from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import ssl
import threading
import time
import uuid
import zipfile
from calendar import monthrange
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException
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
    timeout=httpx.Timeout(60.0, connect=20.0),
    follow_redirects=True,
    headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "Accept-Encoding": "gzip, deflate",
    },
    limits=httpx.Limits(max_keepalive_connections=20, max_connections=50, keepalive_expiry=30.0),
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


def _tasks_dir() -> Path:
    path = settings.data_root / "downloads"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _save_task_to_disk(task: dict[str, Any]) -> None:
    try:
        task_id = task.get("id")
        if not task_id:
            return
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
                _save_task_to_disk(task)
            _tasks[task_id] = task
        except (OSError, json.JSONDecodeError) as err:
            logger.warning("读取历史下载任务失败 (%s): %s", file, err)


_init_load_tasks()


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _publish(task_id: str, **changes: Any) -> None:
    with _lock:
        task = _tasks.get(task_id)
        if not task:
            return
        task.update(changes)
        task["updated_at"] = _now()
        _save_task_to_disk(task)


def _log(task_id: str, message: str, level: str = "info") -> None:
    with _lock:
        task = _tasks.get(task_id)
        if not task:
            return
        task["logs"].append({"time": datetime.now(UTC).strftime("%H:%M:%S"), "level": level, "message": message})
        task["logs"] = task["logs"][-1000:]
        task["updated_at"] = _now()
        _save_task_to_disk(task)


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


def _fetch_archive(archive: Archive) -> tuple[bytes | None, bytes | None]:
    try:
        content = _download(archive.url)
        checksum = _download(f"{archive.url}.CHECKSUM") if content is not None else None
        return content, checksum
    except (httpx.HTTPError, TimeoutError, OSError, ssl.SSLError):
        return None, None


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
        concurrency = min(max(settings.data_download_concurrency, 1), 16)
        total_files = len(plan)
        completed_files = 0
        rows = 0
        downloaded = 0
        skipped = 0
        missing_files = 0

        _publish(task_id, total_files=total_files, stage="下载并转换 K 线", progress=0)
        _log(task_id, f"计划处理 {total_files} 个归档候选，并发数 {concurrency}")

        catalog_lock = threading.Lock()
        state_lock = threading.Lock()

        def update_progress(item_log: str | None = None, log_level: str = "info") -> None:
            nonlocal completed_files, total_files, rows, downloaded, skipped, missing_files
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
                _log(task_id, item_log, log_level)

        def process_archive(symbol: str, interval: str, archive: Archive) -> bool:
            nonlocal completed_files, total_files, rows, downloaded, skipped, missing_files
            if payload.mode == "incremental" and archive.key in manifest.get("archives", {}):
                with state_lock:
                    skipped += 1
                    completed_files += 1
                update_progress(f"已存在，跳过 {archive.key}", "muted")
                return True

            content, checksum = _fetch_archive(archive)
            if content is None:
                return False

            try:
                digest = hashlib.sha256(content).hexdigest()
                if checksum:
                    expected = checksum.decode("utf-8").strip().split()[0].lower()
                    if expected != digest:
                        with state_lock:
                            missing_files += 1
                            completed_files += 1
                        update_progress(f"CHECKSUM 校验不一致，跳过归档: {archive.url}", "warning")
                        return True
                else:
                    _log(task_id, f"远端未提供 CHECKSUM ({archive.key})，继续处理 ZIP", "warning")

                bars = parse_archive(content, instruments[symbol], interval, payload.start_date, payload.end_date)
                with catalog_lock:
                    if bars:
                        catalog.write_data(bars)
                    manifest["archives"][archive.key] = {"sha256": digest, "rows": len(bars), "completed_at": _now()}
                    _save_manifest(manifest_path, manifest)

                with state_lock:
                    rows += len(bars)
                    downloaded += 1
                    completed_files += 1
                update_progress(f"转换并写入 {len(bars):,} 根 K 线 ({symbol} {interval} {archive.key.split('/')[-1]})", "success")
                return True
            except Exception as e:  # noqa: BLE001
                with state_lock:
                    missing_files += 1
                    completed_files += 1
                update_progress(f"解析写入归档失败 ({archive.url}): {e}", "warning")
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
                        _log(task_id, f"月归档不存在，拆分为 {len(archive.fallbacks)} 个日归档 ({symbol} {interval})", "warning")
                        fallback_daily_items.extend((symbol, interval, daily) for daily in archive.fallbacks)

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

        _publish(task_id, status="completed", stage="完成", progress=100, finished_at=_now())
        _log(task_id, f"任务完成：新增 {rows:,} 根 K 线，下载 {downloaded} 个文件，跳过 {skipped} 个，缺失/跳过 {missing_files} 个", "success")
    except Exception as exc:  # noqa: BLE001 - task failures must be reflected in task state
        _publish(task_id, status="failed", stage="失败", error=str(exc), finished_at=_now())
        _log(task_id, str(exc), "error")


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
