from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import math
import re
import shutil
import ssl
import threading
import time
import uuid
import zipfile
from collections import defaultdict
from calendar import monthrange
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pyarrow.parquet as pq
from fastapi import APIRouter, HTTPException, Query
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.identifiers import InstrumentId, Symbol
from nautilus_trader.model.instruments import CryptoPerpetual, CurrencyPair
from nautilus_trader.model.objects import Currency, Price, Quantity
from nautilus_trader.persistence.catalog.parquet import ParquetDataCatalog
from pydantic import BaseModel, Field, field_validator

from .config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/data", tags=["data"])
VISION_ROOT = "https://data.binance.vision/data"
EXCHANGE_INFO = {
    "spot": "https://api.binance.com/api/v3/exchangeInfo",
    "um": "https://fapi.binance.com/fapi/v1/exchangeInfo",
}
TIMEFRAMES = {"1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d", "3d", "1w", "1M"}
CATALOG_FORMAT_VERSION = 2

_HTTP_CLIENT = httpx.Client(
    timeout=httpx.Timeout(30.0, connect=10.0),
    follow_redirects=True,
    headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "Accept-Encoding": "gzip, deflate",
    },
    limits=httpx.Limits(max_keepalive_connections=128, max_connections=256, keepalive_expiry=60.0),
)


class DownloadCreate(BaseModel):
    market_type: str = "um"
    symbols: list[str] = Field(min_length=1)
    intervals: list[str] = Field(min_length=1)
    start_date: date
    end_date: date
    catalog_path: str | None = None
    mode: str = "incremental"

    @field_validator("market_type")
    @classmethod
    def valid_market(cls, value: str) -> str:
        if value not in {"spot", "um"}:
            raise ValueError("目前仅支持现货和 U 本位永续合约")
        return value

    @field_validator("intervals")
    @classmethod
    def valid_intervals(cls, values: list[str]) -> list[str]:
        invalid = set(values) - TIMEFRAMES
        if invalid:
            raise ValueError(f"不支持的 K 线周期: {', '.join(sorted(invalid))}")
        return list(dict.fromkeys(values))

    def model_post_init(self, __context: Any, /) -> None:
        if self.end_date < self.start_date:
            raise ValueError("结束日期不能早于开始日期")
        if self.end_date >= datetime.now(UTC).date():
            raise ValueError("Binance 日归档次日发布，结束日期最晚为昨天（UTC）")


@dataclass(frozen=True)
class Archive:
    key: str
    url: str
    fallbacks: tuple[Archive, ...] = ()


_tasks: dict[str, dict[str, Any]] = {}
_lock = threading.Lock()
_last_task_save: dict[str, float] = {}


def _tasks_dir() -> Path:
    path = settings.data_root / "downloads"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _save_task_to_disk(task: dict[str, Any], force: bool = False) -> None:
    try:
        task_id = task.get("id")
        if not task_id:
            return
        now_ts = time.time()
        # Debounce disk write to max once per 250ms unless force=True
        if not force and (now_ts - _last_task_save.get(task_id, 0)) < 0.25:
            return
        _last_task_save[task_id] = now_ts
        file_path = _tasks_dir() / f"{task_id}.json"
        tmp_path = _tasks_dir() / f"{task_id}.json.tmp"
        tmp_path.write_text(json.dumps(task, ensure_ascii=False, indent=2))
        tmp_path.replace(file_path)
    except OSError as err:
        logger.warning("保存下载任务到磁盘失败: %s", err)


def _init_load_tasks() -> None:
    """Load persisted tasks from disk."""
    dir_path = _tasks_dir()
    for file in dir_path.glob("*.json"):
        try:
            task = json.loads(file.read_text())
            task_id = task.get("id")
            if not task_id:
                continue
            if task.get("status") in {"queued", "running"}:
                task["status"] = "failed"
                task["stage"] = "已中断"
                task["error"] = "服务重启导致任务中断（可使用增量模式继续下载）"
                task["updated_at"] = _now()
                _save_task_to_disk(task, force=True)
            _tasks[task_id] = task
        except (OSError, json.JSONDecodeError) as err:
            logger.warning("读取历史下载任务失败 (%s): %s", file, err)


_init_load_tasks()


def _publish(task_id: str, force_save: bool = False, **changes: Any) -> None:
    with _lock:
        task = _tasks.get(task_id)
        if not task:
            return
        task.update(changes)
        task["updated_at"] = _now()
        _save_task_to_disk(task, force=force_save)


def _log(task_id: str, message: str, level: str = "info", force_save: bool = False, **_kwargs: Any) -> None:
    with _lock:
        task = _tasks.get(task_id)
        if not task:
            return
        task["logs"].append({"time": datetime.now(UTC).strftime("%H:%M:%S"), "level": level, "message": message})
        task["logs"] = task["logs"][-1000:]
        task["updated_at"] = _now()
        _save_task_to_disk(task, force=force_save or bool(_kwargs.get("force_log")))


def _json_get(url: str) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(5):
        try:
            response = _HTTP_CLIENT.get(url)
            if response.status_code == 429 or response.status_code >= 500:
                retry_after = response.headers.get("Retry-After")
                delay = float(retry_after) if retry_after and retry_after.isdigit() else (2**attempt) * 0.5
                time.sleep(min(delay, 15))
                continue
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, TimeoutError, OSError, ssl.SSLError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt == 4:
                break
            time.sleep((2**attempt) * 0.5)
    raise RuntimeError(f"请求接口失败 ({url}): {last_error}") from last_error


def _download(url: str) -> bytes | None:
    for attempt in range(5):
        try:
            response = _HTTP_CLIENT.get(url)
            if response.status_code == 404:
                return None
            if response.status_code == 429 or response.status_code >= 500:
                if attempt == 4:
                    response.raise_for_status()
                retry_after = response.headers.get("Retry-After")
                delay = float(retry_after) if retry_after and retry_after.isdigit() else (2**attempt) * 0.5
                time.sleep(min(delay, 20))
                continue
            response.raise_for_status()
            return response.content
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            if attempt == 4:
                raise
            time.sleep((2**attempt) * 0.5)
        except (httpx.HTTPError, TimeoutError, OSError, ssl.SSLError):
            if attempt == 4:
                raise
            time.sleep((2**attempt) * 0.5)
    return None


def _fetch_archive(archive: Archive) -> bytes | None:
    """Download single archive ZIP directly. ZIP internal CRC32 validates payload integrity."""
    try:
        return _download(archive.url)
    except (httpx.HTTPError, TimeoutError, OSError, ssl.SSLError):
        return None


def _symbol_map(market_type: str) -> dict[str, dict[str, Any]]:
    payload = _json_get(EXCHANGE_INFO[market_type])
    return {item["symbol"].upper(): item for item in payload.get("symbols", []) if item.get("status") == "TRADING"}


def normalize_symbol(value: str) -> str:
    value = value.strip().upper().split(":", 1)[0]
    return "".join(char for char in value if char.isalnum())


def archive_plan(symbol: str, interval: str, start: date, end: date, market_type: str) -> list[Archive]:
    path = "spot" if market_type == "spot" else "futures/um"
    archives: list[Archive] = []
    cursor = start.replace(day=1)
    current_month_first = datetime.now(UTC).date().replace(day=1)
    while cursor <= end:
        last = date(cursor.year, cursor.month, monthrange(cursor.year, cursor.month)[1])
        part_start, part_end = max(start, cursor), min(end, last)
        daily: list[Archive] = []
        day = part_start
        while day <= part_end:
            daily_period = day.isoformat()
            daily_name = f"{symbol}-{interval}-{daily_period}.zip"
            daily.append(
                Archive(
                    f"{market_type}/{symbol}/{interval}/daily/{daily_period}",
                    f"{VISION_ROOT}/{path}/daily/klines/{symbol}/{interval}/{daily_name}",
                )
            )
            day += timedelta(days=1)

        # Current ongoing month does not have monthly archives on Binance Vision yet
        if cursor >= current_month_first:
            archives.extend(daily)
        else:
            month_period = cursor.strftime("%Y-%m")
            month_name = f"{symbol}-{interval}-{month_period}.zip"
            slice_key = f"{part_start.isoformat()}_{part_end.isoformat()}"
            archives.append(
                Archive(
                    f"{market_type}/{symbol}/{interval}/monthly/{month_period}/{slice_key}",
                    f"{VISION_ROOT}/{path}/monthly/klines/{symbol}/{interval}/{month_name}",
                    tuple(daily),
                )
            )
        cursor = (last + timedelta(days=1)).replace(day=1)
    return archives


def _precision(value: str) -> int:
    # Binance serializes filter values at a fixed scale (for example
    # ``0.00100000``).  Trailing zeroes are presentation padding, not price
    # precision.  Keeping them made SOL's instrument precision 8 while its
    # bars naturally used 3-4 decimals, which Nautilus rejects at matching.
    normalized = Decimal(value).normalize()
    return max(0, -normalized.as_tuple().exponent)


def _fixed_price(value: str, precision: int) -> Price:
    """Create every OHLC value with the instrument's exact precision."""
    return Price(Decimal(value), precision)


def _fixed_quantity(value: str, precision: int) -> Quantity:
    """Create bar volume with a stable precision across the data set."""
    return Quantity(Decimal(value), precision)


def _filter(info: dict[str, Any], kind: str) -> dict[str, Any]:
    return next((item for item in info.get("filters", []) if item.get("filterType") == kind), {})


def make_instrument(info: dict[str, Any], market_type: str):
    raw = info["symbol"]
    price_filter, lot_filter = _filter(info, "PRICE_FILTER"), _filter(info, "LOT_SIZE")
    tick, step = price_filter.get("tickSize", "0.00000001"), lot_filter.get("stepSize", "0.00000001")
    price_precision, size_precision = _precision(tick), _precision(step)
    base, quote = Currency.from_str(info["baseAsset"]), Currency.from_str(info["quoteAsset"])
    instrument_id = InstrumentId.from_str(f"{raw}{'-PERP' if market_type == 'um' else ''}.BINANCE")
    common = {
        "instrument_id": instrument_id,
        "raw_symbol": Symbol(raw),
        "base_currency": base,
        "quote_currency": quote,
        "price_precision": price_precision,
        "size_precision": size_precision,
        "price_increment": _fixed_price(tick, price_precision),
        "size_increment": _fixed_quantity(step, size_precision),
        "ts_event": 0,
        "ts_init": 0,
        "min_quantity": _fixed_quantity(lot_filter["minQty"], size_precision) if lot_filter.get("minQty") and Decimal(lot_filter["minQty"]) > 0 else None,
        "max_quantity": _fixed_quantity(lot_filter["maxQty"], size_precision) if lot_filter.get("maxQty") and Decimal(lot_filter["maxQty"]) > 0 else None,
        "info": {"source": "data.binance.vision", "market_type": market_type},
    }
    if market_type == "spot":
        return CurrencyPair(**common)
    return CryptoPerpetual(**common, settlement_currency=Currency.from_str(info.get("marginAsset", info["quoteAsset"])), is_inverse=False)


def _timestamp_ns(raw: str) -> int:
    value = int(raw)
    return value * (1_000 if value >= 100_000_000_000_000 else 1_000_000)


def parse_archive(content: bytes, instrument: Any, interval: str, start: date, end: date) -> list[Bar]:
    bar_type = BarType.from_str(f"{instrument.id}-{interval_to_spec(interval)}-EXTERNAL")
    lower = int(datetime.combine(start, datetime.min.time(), UTC).timestamp() * 1_000_000_000)
    upper = int(datetime.combine(end + timedelta(days=1), datetime.min.time(), UTC).timestamp() * 1_000_000_000)
    bars: list[Bar] = []
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        names = [name for name in archive.namelist() if name.lower().endswith(".csv")]
        if not names:
            raise ValueError("ZIP 中没有 CSV 文件")
        with archive.open(names[0]) as raw:
            reader = csv.reader(io.TextIOWrapper(raw, encoding="utf-8-sig"))
            for row in reader:
                if len(row) < 7 or not row[0].isdigit():
                    continue
                open_ns = _timestamp_ns(row[0])
                if not lower <= open_ns < upper:
                    continue
                close_ns = _timestamp_ns(row[6])
                bars.append(Bar(
                    bar_type,
                    _fixed_price(row[1], instrument.price_precision),
                    _fixed_price(row[2], instrument.price_precision),
                    _fixed_price(row[3], instrument.price_precision),
                    _fixed_price(row[4], instrument.price_precision),
                    _fixed_quantity(row[5], instrument.size_precision),
                    close_ns,
                    close_ns,
                ))
    return bars


def interval_to_spec(interval: str) -> str:
    unit = {"m": "MINUTE", "h": "HOUR", "d": "DAY", "w": "WEEK", "M": "MONTH"}[interval[-1]]
    return f"{int(interval[:-1])}-{unit}-LAST"


def _manifest(path: Path) -> tuple[Path, dict[str, Any]]:
    target = path / ".quantlab-downloads.json"
    try:
        manifest = json.loads(target.read_text())
        if manifest.get("version") == CATALOG_FORMAT_VERSION:
            return target, manifest
        # Version 1 stored Binance padding as precision and wrote OHLC values
        # with variable precision. Do not trust its "already downloaded"
        # markers: the next incremental request must rebuild the selected data.
        return target, {"version": CATALOG_FORMAT_VERSION, "archives": {}}
    except (FileNotFoundError, json.JSONDecodeError):
        return target, {"version": CATALOG_FORMAT_VERSION, "archives": {}}


def _save_manifest(target: Path, manifest: dict[str, Any]) -> None:
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    temporary.replace(target)


def run_download(task_id: str, payload: DownloadCreate) -> None:
    try:
        catalog_path = Path(payload.catalog_path or settings.catalog_path).expanduser().resolve()
        catalog_path.mkdir(parents=True, exist_ok=True)
        _publish(task_id, status="running", stage="读取 Binance 交易品种", catalog_path=str(catalog_path))
        _log(task_id, f"Catalog: {catalog_path}")
        markets = _symbol_map(payload.market_type)
        symbols = list(dict.fromkeys(normalize_symbol(item) for item in payload.symbols))
        missing = [item for item in symbols if item not in markets]
        if missing:
            _log(task_id, f"跳过未在交易中的品种: {', '.join(missing)}", "warning")
            symbols = [item for item in symbols if item in markets]
        if not symbols:
            raise ValueError("没有可用的有效交易品种")
        instruments = {symbol: make_instrument(markets[symbol], payload.market_type) for symbol in symbols}
        catalog = ParquetDataCatalog(str(catalog_path))
        # Instrument metadata is tiny and uses a deterministic catalog path.
        # Always rewrite the requested definitions so corrected exchange
        # filters replace stale precision metadata from earlier imports.
        catalog.write_data(list(instruments.values()))
        _log(task_id, f"已校准 {len(instruments)} 个 Instrument")
        manifest_path, manifest = _manifest(catalog_path)
        plan = [
            (symbol, interval, archive)
            for symbol in symbols
            for interval in payload.intervals
            for archive in archive_plan(symbol, interval, payload.start_date, payload.end_date, payload.market_type)
        ]
        concurrency = min(max(settings.data_download_concurrency, 1), 64)
        total_files = len(plan)
        completed_files = 0
        rows = 0
        downloaded = 0
        skipped = 0
        missing_files = 0

        _publish(task_id, force_save=True, total_files=total_files, stage="下载并转换 K 线", progress=0)
        _log(task_id, f"计划处理 {total_files} 个归档候选，高并发度 {concurrency}", force_save=True)

        catalog_lock = threading.Lock()
        state_lock = threading.Lock()
        pending_manifest: dict[str, dict[str, Any]] = {}
        last_manifest_save = [time.time()]
        last_log_time = 0.0

        def flush_manifest(force: bool = False) -> None:
            with catalog_lock:
                now = time.time()
                if pending_manifest and (force or (now - last_manifest_save[0]) >= 0.8 or len(pending_manifest) >= 50):
                    manifest["archives"].update(pending_manifest)
                    pending_manifest.clear()
                    _save_manifest(manifest_path, manifest)
                    last_manifest_save[0] = now

        def update_progress(item_log: str | None = None, log_level: str = "info", force_log: bool = False) -> None:
            nonlocal completed_files, total_files, rows, downloaded, skipped, missing_files, last_log_time
            with state_lock:
                pct = min(100, round(completed_files / max(1, total_files) * 100))
                _publish(
                    task_id,
                    completed_files=completed_files,
                    total_files=total_files,
                    progress=pct,
                    rows=rows,
                    downloaded_files=downloaded,
                    skipped_files=skipped,
                    missing_files=missing_files,
                )
            if item_log:
                now = time.time()
                if force_log or log_level in {"warning", "error"} or (now - last_log_time) >= 0.15:
                    last_log_time = now
                    _log(task_id, item_log, log_level)

        def process_archive(symbol: str, interval: str, archive: Archive) -> bool:
            nonlocal completed_files, total_files, rows, downloaded, skipped, missing_files
            if payload.mode == "incremental" and archive.key in manifest.get("archives", {}):
                with state_lock:
                    skipped += 1
                    completed_files += 1
                update_progress(f"已存在，跳过 {archive.key}", "muted")
                return True

            content = _fetch_archive(archive)
            if content is None:
                return False

            try:
                digest = hashlib.sha256(content).hexdigest()
                bars = parse_archive(content, instruments[symbol], interval, payload.start_date, payload.end_date)
                
                with catalog_lock:
                    if bars:
                        try:
                            catalog.write_data(bars)
                        except Exception as write_err:
                            if "non-disjoint intervals" in str(write_err):
                                logger.info("Parquet 区间已存在或重叠 (%s)，自动跳过写入: %s", archive.key, write_err)
                            else:
                                raise
                    pending_manifest[archive.key] = {"sha256": digest, "rows": len(bars), "completed_at": _now()}
                    if (time.time() - last_manifest_save[0]) >= 0.8 or len(pending_manifest) >= 50:
                        manifest["archives"].update(pending_manifest)
                        pending_manifest.clear()
                        _save_manifest(manifest_path, manifest)
                        last_manifest_save[0] = time.time()

                with state_lock:
                    rows += len(bars)
                    downloaded += 1
                    completed_files += 1

                update_progress(f"写入完成 {len(bars):,} 根 K 线 ({symbol} {interval} {archive.key.split('/')[-1]})", "success")
                return True
            except Exception as e:  # noqa: BLE001
                with state_lock:
                    missing_files += 1
                    completed_files += 1
                update_progress(f"解析归档失败 ({archive.url}): {e}", "warning")
                return True

        monthly_items = [item for item in plan if item[2].fallbacks]
        direct_daily_items = [item for item in plan if not item[2].fallbacks]
        fallback_daily_items: list[tuple[str, str, Archive]] = []

        if monthly_items:
            with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="binance-monthly") as pool:
                futures = {
                    pool.submit(process_archive, symbol, interval, archive): (symbol, interval, archive)
                    for symbol, interval, archive in monthly_items
                }
                for future in as_completed(futures):
                    symbol, interval, archive = futures[future]
                    try:
                        success = future.result()
                    except Exception:  # noqa: BLE001
                        success = False
                    if not success:
                        with state_lock:
                            total_files += len(archive.fallbacks) - 1
                            _publish(task_id, total_files=total_files)
                        _log(task_id, f"月归档不存在，拆分为 {len(archive.fallbacks)} 个日归档 ({symbol} {interval})", "warning", force_log=True)
                        fallback_daily_items.extend((symbol, interval, daily) for daily in archive.fallbacks)

        flush_manifest(force=True)

        all_daily_items = direct_daily_items + fallback_daily_items
        if all_daily_items:
            with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="binance-daily") as pool:
                futures = {
                    pool.submit(process_archive, symbol, interval, archive): (symbol, interval, archive)
                    for symbol, interval, archive in all_daily_items
                }
                for future in as_completed(futures):
                    symbol, interval, archive = futures[future]
                    try:
                        success = future.result()
                    except Exception:  # noqa: BLE001
                        success = False
                    if not success:
                        with state_lock:
                            missing_files += 1
                            completed_files += 1
                        update_progress(f"远端归档不存在或下载失败，已跳过 {archive.url}", "warning")

        flush_manifest(force=True)

        _publish(task_id, force_save=True, status="completed", stage="完成", progress=100, finished_at=_now())
        _log(task_id, f"任务完成：新增 {rows:,} 根 K 线，下载 {downloaded} 个文件，跳过 {skipped} 个，缺失/跳过 {missing_files} 个", "success", force_save=True)
    except Exception as exc:  # noqa: BLE001 - task failures must be reflected in task state
        _publish(task_id, force_save=True, status="failed", stage="失败", error=str(exc), finished_at=_now())
        _log(task_id, str(exc), "error", force_save=True)


@router.get("/symbols")
def symbols(market_type: str = "um"):
    if market_type not in EXCHANGE_INFO:
        raise HTTPException(400, "不支持的交易类型")
    try:
        markets = _symbol_map(market_type)
    except Exception as exc:
        raise HTTPException(502, f"读取 Binance 品种失败: {exc}") from exc
    return [{"symbol": key, "base": value["baseAsset"], "quote": value["quoteAsset"]} for key, value in sorted(markets.items())]


@router.get("/downloads")
def list_downloads():
    with _lock:
        return sorted(_tasks.values(), key=lambda x: str(x.get("created_at", "")), reverse=True)


@router.get("/downloads/latest")
def get_latest_download():
    with _lock:
        tasks = sorted(_tasks.values(), key=lambda x: str(x.get("created_at", "")), reverse=True)
        if not tasks:
            return None
        return dict(tasks[0])


@router.post("/downloads", status_code=202)
def create_download(payload: DownloadCreate):
    task_id = str(uuid.uuid4())
    task = {
        "id": task_id,
        "status": "queued",
        "stage": "等待执行",
        "progress": 0,
        "logs": [],
        "rows": 0,
        "completed_files": 0,
        "total_files": 0,
        "downloaded_files": 0,
        "skipped_files": 0,
        "missing_files": 0,
        "error": None,
        "created_at": _now(),
        "updated_at": _now(),
        "request": payload.model_dump(mode="json"),
    }
    with _lock:
        _tasks[task_id] = task
        _save_task_to_disk(task)
    threading.Thread(target=run_download, args=(task_id, payload), daemon=True, name=f"binance-download-{task_id[:8]}").start()
    return task


@router.get("/downloads/{task_id}")
def get_download(task_id: str):
    with _lock:
        task = _tasks.get(task_id)
        if not task:
            file_path = _tasks_dir() / f"{task_id}.json"
            if file_path.exists():
                try:
                    task = json.loads(file_path.read_text())
                    _tasks[task_id] = task
                except (OSError, json.JSONDecodeError):
                    logger.warning("读取任务文件失败: %s", file_path)
        if not task:
            raise HTTPException(404, "下载任务不存在")
        return dict(task)


@router.delete("/downloads/{task_id}")
def delete_download(task_id: str):
    with _lock:
        _tasks.pop(task_id, None)
        file_path = _tasks_dir() / f"{task_id}.json"
        if file_path.exists():
            file_path.unlink(missing_ok=True)
    return {"ok": True}


def parse_spec_to_interval(spec: str) -> str:
    """Convert Nautilus interval spec like '1-MINUTE-LAST-EXTERNAL' to '1m', '4-HOUR-LAST-EXTERNAL' to '4h'."""
    m = re.match(r"^(\d+)-([A-Z]+)-LAST-EXTERNAL$", spec)
    if not m:
        return spec
    num, unit = m.group(1), m.group(2)
    u_map = {"MINUTE": "m", "HOUR": "h", "DAY": "d", "WEEK": "w", "MONTH": "M"}
    return f"{num}{u_map.get(unit, unit.lower())}"


# In-memory file cache: {file_path: (st_mtime_ns, st_size, num_rows, min_ts, max_ts)}
_PARQUET_FILE_CACHE: dict[str, tuple[int, int, int, int | None, int | None]] = {}
_CATALOG_INSTRUMENTS_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


def _get_parquet_file_stats(file_path: str, st_mtime_ns: int, st_size: int) -> tuple[int, int | None, int | None]:
    """Get row count, min timestamp (ns), max timestamp (ns) for a Parquet file using stat cache & footer column stats."""
    cached = _PARQUET_FILE_CACHE.get(file_path)
    if cached and cached[0] == st_mtime_ns and cached[1] == st_size:
        return cached[2], cached[3], cached[4]

    num_rows = 0
    min_ts = None
    max_ts = None
    try:
        meta = pq.read_metadata(file_path)
        num_rows = meta.num_rows
        # Extract timestamp min/max directly from footer column statistics without reading table data
        for rg_idx in range(meta.num_row_groups):
            rg = meta.row_group(rg_idx)
            for col_idx in range(rg.num_columns):
                col = rg.column(col_idx)
                if col.path_in_schema in ("ts_init", "ts_event") and col.is_stats_set:
                    c_min, c_max = col.statistics.min, col.statistics.max
                    if c_min is not None:
                        min_ts = c_min if min_ts is None else min(min_ts, c_min)
                    if c_max is not None:
                        max_ts = c_max if max_ts is None else max(max_ts, c_max)
                    break
    except Exception:
        pass

    # Fallback to reading ts_init column only if footer stats are missing
    if (min_ts is None or max_ts is None) and num_rows > 0:
        try:
            t = pq.read_table(file_path, columns=["ts_init"])
            ts_list = t["ts_init"].to_pylist()
            if ts_list:
                min_ts, max_ts = min(ts_list), max(ts_list)
        except Exception:
            pass

    _PARQUET_FILE_CACHE[file_path] = (st_mtime_ns, st_size, num_rows, min_ts, max_ts)
    return num_rows, min_ts, max_ts


def _get_registered_instruments(catalog_path: Path) -> dict[str, Any]:
    cat_key = str(catalog_path)
    now = time.monotonic()
    cached = _CATALOG_INSTRUMENTS_CACHE.get(cat_key)
    if cached and (now - cached[0] < 60.0):
        return cached[1]

    registered_instruments: dict[str, Any] = {}
    try:
        if catalog_path.exists():
            catalog = ParquetDataCatalog(str(catalog_path))
            for inst in catalog.instruments():
                registered_instruments[inst.id.value] = inst
    except Exception as err:
        logger.warning("读取 Catalog Instruments 失败: %s", err)

    _CATALOG_INSTRUMENTS_CACHE[cat_key] = (now, registered_instruments)
    return registered_instruments


def scan_catalog_summary(
    catalog_path: Path,
    query: str | None = None,
    market_type: str | None = None,
    interval: str | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    page: int = 1,
    page_size: int = 20,
    sort_by: str = "symbol",
    sort_order: str = "asc",
) -> dict[str, Any]:
    """Scan and aggregate statistics for all instruments in the Parquet Data Catalog with high performance caching & pagination."""
    catalog_path = catalog_path.expanduser().resolve()
    bar_dir = catalog_path / "data" / "bar"

    registered_instruments = _get_registered_instruments(catalog_path)

    symbols_map: dict[str, dict[str, Any]] = {}
    total_catalog_bars = 0
    total_catalog_size = 0

    if bar_dir.exists():
        for d in sorted(bar_dir.iterdir()):
            if not d.is_dir():
                continue
            m = re.match(r"^(.+?\.[A-Z0-9]+)-(\d+-[A-Z]+-LAST-EXTERNAL)$", d.name)
            if not m:
                continue
            inst_id, spec = m.group(1), m.group(2)
            tf_interval = parse_spec_to_interval(spec)
            is_perp = "-PERP." in inst_id or inst_id.endswith("-PERP")
            inst_market_type = "um" if is_perp else "spot"
            raw_symbol = inst_id.split(".")[0].replace("-PERP", "")

            tf_bars = 0
            tf_size = 0
            tf_start: int | None = None
            tf_end: int | None = None
            parquet_files = sorted(d.glob("*.parquet"))

            for f in parquet_files:
                try:
                    st = f.stat()
                    tf_size += st.st_size
                    f_rows, f_min, f_max = _get_parquet_file_stats(str(f), st.st_mtime_ns, st.st_size)
                    tf_bars += f_rows
                    if f_min is not None:
                        tf_start = f_min if tf_start is None else min(tf_start, f_min)
                    if f_max is not None:
                        tf_end = f_max if tf_end is None else max(tf_end, f_max)
                except Exception:
                    pass

            if tf_bars == 0 and not parquet_files:
                continue

            total_catalog_bars += tf_bars
            total_catalog_size += tf_size

            if inst_id not in symbols_map:
                inst_obj = registered_instruments.get(inst_id)
                base = inst_obj.base_currency.code if inst_obj else raw_symbol.replace("USDT", "").replace("USDC", "").replace("BUSD", "")
                quote = inst_obj.quote_currency.code if inst_obj else ("USDT" if "USDT" in raw_symbol else "USD")
                symbols_map[inst_id] = {
                    "symbol": raw_symbol,
                    "instrument_id": inst_id,
                    "market_type": inst_market_type,
                    "market_type_label": "U本位永续" if inst_market_type == "um" else "现货",
                    "base_currency": base,
                    "quote_currency": quote,
                    "total_bars": 0,
                    "total_size_bytes": 0,
                    "file_count": 0,
                    "start_time": None,
                    "end_time": None,
                    "start_date": None,
                    "end_date": None,
                    "timeframes": [],
                }

            entry = symbols_map[inst_id]
            entry["total_bars"] += tf_bars
            entry["total_size_bytes"] += tf_size
            entry["file_count"] += len(parquet_files)

            if tf_start is not None:
                entry["_min_ns"] = min(entry.get("_min_ns", tf_start), tf_start)
            if tf_end is not None:
                entry["_max_ns"] = max(entry.get("_max_ns", tf_end), tf_end)

            start_dt_str = datetime.fromtimestamp(tf_start / 1e9, UTC).strftime("%Y-%m-%d %H:%M:%S") if tf_start else None
            end_dt_str = datetime.fromtimestamp(tf_end / 1e9, UTC).strftime("%Y-%m-%d %H:%M:%S") if tf_end else None

            entry["timeframes"].append({
                "interval": tf_interval,
                "spec": spec,
                "bar_type": d.name,
                "bars": tf_bars,
                "size_bytes": tf_size,
                "file_count": len(parquet_files),
                "start_time": start_dt_str,
                "end_time": end_dt_str,
                "start_date": start_dt_str[:10] if start_dt_str else None,
                "end_date": end_dt_str[:10] if end_dt_str else None,
            })

    all_timeframes_set: set[str] = set()
    filtered_items: list[dict[str, Any]] = []

    for inst_id, entry in symbols_map.items():
        min_ns = entry.pop("_min_ns", None)
        max_ns = entry.pop("_max_ns", None)
        if min_ns:
            dt = datetime.fromtimestamp(min_ns / 1e9, UTC)
            entry["start_time"] = dt.strftime("%Y-%m-%d %H:%M:%S")
            entry["start_date"] = dt.strftime("%Y-%m-%d")
        if max_ns:
            dt = datetime.fromtimestamp(max_ns / 1e9, UTC)
            entry["end_time"] = dt.strftime("%Y-%m-%d %H:%M:%S")
            entry["end_date"] = dt.strftime("%Y-%m-%d")

        # Sort timeframes
        entry["timeframes"].sort(key=lambda x: (x["interval"] not in TIMEFRAMES, x["interval"]))
        for tf in entry["timeframes"]:
            all_timeframes_set.add(tf["interval"])

        if query:
            q = query.strip().upper()
            if q not in entry["symbol"].upper() and q not in inst_id.upper() and q not in entry["base_currency"].upper():
                continue

        if market_type and market_type != "all":
            if entry["market_type"] != market_type:
                continue

        if interval and interval != "all":
            if not any(tf["interval"] == interval for tf in entry["timeframes"]):
                continue

        if start_date and entry["end_date"]:
            if entry["end_date"] < start_date.isoformat():
                continue

        if end_date and entry["start_date"]:
            if entry["start_date"] > end_date.isoformat():
                continue

        filtered_items.append(entry)

    # Sort filtered items
    reverse = (sort_order == "desc")
    if sort_by == "bars":
        filtered_items.sort(key=lambda x: x["total_bars"], reverse=reverse)
    elif sort_by == "size":
        filtered_items.sort(key=lambda x: x["total_size_bytes"], reverse=reverse)
    elif sort_by == "start":
        filtered_items.sort(key=lambda x: x["start_date"] or "", reverse=reverse)
    elif sort_by == "end":
        filtered_items.sort(key=lambda x: x["end_date"] or "", reverse=reverse)
    else:
        filtered_items.sort(key=lambda x: x["symbol"], reverse=reverse)

    total_filtered_symbols = len(filtered_items)
    total_filtered_bars = sum(x["total_bars"] for x in filtered_items)
    total_filtered_size = sum(x["total_size_bytes"] for x in filtered_items)

    # Pagination
    if page_size > 0:
        total_pages = max(1, math.ceil(total_filtered_symbols / page_size)) if total_filtered_symbols > 0 else 1
        page = min(max(1, page), total_pages)
        start_idx = (page - 1) * page_size
        end_idx = start_idx + page_size
        paged_items = filtered_items[start_idx:end_idx]
    else:
        page = 1
        page_size = total_filtered_symbols
        total_pages = 1
        paged_items = filtered_items

    return {
        "catalog_path": str(catalog_path),
        "total_symbols": total_filtered_symbols,
        "total_bars": total_filtered_bars,
        "total_size_bytes": total_filtered_size,
        "all_symbols_count": len(symbols_map),
        "all_bars_count": total_catalog_bars,
        "all_size_bytes": total_catalog_size,
        "available_timeframes": sorted(all_timeframes_set),
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
        "items": paged_items,
    }


def delete_catalog_symbol_data(catalog_path: Path, instrument_id: str, interval: str | None = None) -> bool:
    """Delete a symbol or specific timeframe data from catalog and update manifest."""
    catalog_path = catalog_path.expanduser().resolve()
    bar_dir = catalog_path / "data" / "bar"
    manifest_path, manifest = _manifest(catalog_path)
    deleted_anything = False

    # Invalidate cache for deleted items
    _CATALOG_INSTRUMENTS_CACHE.pop(str(catalog_path), None)
    cached_keys = list(_PARQUET_FILE_CACHE.keys())
    for k in cached_keys:
        if instrument_id in k:
            _PARQUET_FILE_CACHE.pop(k, None)

    if bar_dir.exists():
        for d in list(bar_dir.iterdir()):
            if not d.is_dir():
                continue
            if not d.name.startswith(f"{instrument_id}-"):
                continue
            if interval:
                spec = d.name[len(instrument_id) + 1:]
                if parse_spec_to_interval(spec) != interval:
                    continue
            shutil.rmtree(d, ignore_errors=True)
            deleted_anything = True

    if not interval:
        is_perp = "-PERP." in instrument_id or instrument_id.endswith("-PERP")
        type_dir = catalog_path / "data" / ("crypto_perpetual" if is_perp else "currency_pair")
        inst_folder = type_dir / instrument_id
        if inst_folder.exists():
            shutil.rmtree(inst_folder, ignore_errors=True)
            deleted_anything = True

    if "archives" in manifest:
        raw_symbol = instrument_id.split(".")[0].replace("-PERP", "")
        keys_to_delete = [
            k for k in manifest["archives"]
            if f"/{raw_symbol}/" in k and (not interval or f"/{raw_symbol}/{interval}/" in k)
        ]
        for k in keys_to_delete:
            manifest["archives"].pop(k, None)
        _save_manifest(manifest_path, manifest)

    return deleted_anything


@router.get("/catalog/summary")
def catalog_summary(
    query: str | None = None,
    market_type: str | None = None,
    interval: str | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    catalog_path: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=0, le=500),
    sort_by: str = Query("symbol"),
    sort_order: str = Query("asc"),
):
    path = Path(catalog_path or settings.catalog_path)
    return scan_catalog_summary(
        path,
        query=query,
        market_type=market_type,
        interval=interval,
        start_date=start_date,
        end_date=end_date,
        page=page,
        page_size=page_size,
        sort_by=sort_by,
        sort_order=sort_order,
    )


@router.delete("/catalog/symbols/{instrument_id}")
def delete_catalog_symbol(instrument_id: str, interval: str | None = None, catalog_path: str | None = None):
    path = Path(catalog_path or settings.catalog_path)
    deleted = delete_catalog_symbol_data(path, instrument_id, interval)
    return {"ok": True, "deleted": deleted}

