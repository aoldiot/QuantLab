from __future__ import annotations

import csv
import hashlib
import io
import json
import threading
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from calendar import monthrange
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.identifiers import InstrumentId, Symbol
from nautilus_trader.model.instruments import CryptoPerpetual, CurrencyPair
from nautilus_trader.model.objects import Currency, Price, Quantity
from nautilus_trader.persistence.catalog.parquet import ParquetDataCatalog
from pydantic import BaseModel, Field, field_validator

from .config import settings

router = APIRouter(prefix="/api/data", tags=["data"])
VISION_ROOT = "https://data.binance.vision/data"
EXCHANGE_INFO = {
    "spot": "https://api.binance.com/api/v3/exchangeInfo",
    "um": "https://fapi.binance.com/fapi/v1/exchangeInfo",
}
TIMEFRAMES = {"1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d", "3d", "1w", "1M"}
CATALOG_FORMAT_VERSION = 2


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


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _publish(task_id: str, **changes: Any) -> None:
    with _lock:
        task = _tasks[task_id]
        task.update(changes)
        task["updated_at"] = _now()


def _log(task_id: str, message: str, level: str = "info") -> None:
    with _lock:
        task = _tasks[task_id]
        task["logs"].append({"time": datetime.now(UTC).strftime("%H:%M:%S"), "level": level, "message": message})
        task["logs"] = task["logs"][-1000:]
        task["updated_at"] = _now()


def _json_get(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": "QuantLab/0.1"})
    with urllib.request.urlopen(request, timeout=25) as response:
        return json.load(response)


def _download(url: str) -> bytes | None:
    for attempt in range(4):
        request = urllib.request.Request(url, headers={"User-Agent": "QuantLab/0.1"})
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            if exc.code not in {429, 500, 502, 503, 504} or attempt == 3:
                raise
            retry_after = exc.headers.get("Retry-After")
            delay = float(retry_after) if retry_after and retry_after.isdigit() else 2**attempt
            time.sleep(min(delay, 30))
        except (TimeoutError, urllib.error.URLError):
            if attempt == 3:
                raise
            time.sleep(2**attempt)
    return None


def _fetch_archive(archive: Archive) -> tuple[bytes | None, bytes | None]:
    content = _download(archive.url)
    checksum = _download(f"{archive.url}.CHECKSUM") if content is not None else None
    return content, checksum


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
    while cursor <= end:
        last = date(cursor.year, cursor.month, monthrange(cursor.year, cursor.month)[1])
        part_start, part_end = max(start, cursor), min(end, last)
        daily: list[Archive] = []
        day = part_start
        while day <= part_end:
            daily_period = day.isoformat()
            daily_name = f"{symbol}-{interval}-{daily_period}.zip"
            daily.append(Archive(f"{market_type}/{symbol}/{interval}/daily/{daily_period}", f"{VISION_ROOT}/{path}/daily/klines/{symbol}/{interval}/{daily_name}"))
            day += timedelta(days=1)
        month_period = cursor.strftime("%Y-%m")
        month_name = f"{symbol}-{interval}-{month_period}.zip"
        # Include the selected slice in the manifest key. A partial-month
        # import must not make a later request for other days look complete.
        slice_key = f"{part_start.isoformat()}_{part_end.isoformat()}"
        archives.append(Archive(
            f"{market_type}/{symbol}/{interval}/monthly/{month_period}/{slice_key}",
            f"{VISION_ROOT}/{path}/monthly/klines/{symbol}/{interval}/{month_name}",
            tuple(daily),
        ))
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
            raise ValueError(f"Binance 当前不存在或未交易的品种: {', '.join(missing)}")
        instruments = {symbol: make_instrument(markets[symbol], payload.market_type) for symbol in symbols}
        catalog = ParquetDataCatalog(str(catalog_path))
        # Instrument metadata is tiny and uses a deterministic catalog path.
        # Always rewrite the requested definitions so corrected exchange
        # filters replace stale precision metadata from earlier imports.
        catalog.write_data(list(instruments.values()))
        _log(task_id, f"已校准 {len(instruments)} 个 Instrument")
        manifest_path, manifest = _manifest(catalog_path)
        plan = [(symbol, interval, archive) for symbol in symbols for interval in payload.intervals for archive in archive_plan(symbol, interval, payload.start_date, payload.end_date, payload.market_type)]
        _publish(task_id, total_files=len(plan), stage="下载并转换 K 线")
        concurrency = min(max(settings.data_download_concurrency, 1), 16)
        _log(task_id, f"计划处理 {len(plan)} 个月归档候选，并发数 {concurrency}")
        rows = downloaded = skipped = missing_files = 0
        total = len(plan)

        monthly_results: dict[str, tuple[bytes | None, bytes | None]] = {}
        pending_monthly = [item[2] for item in plan if not (payload.mode == "incremental" and item[2].key in manifest["archives"])]
        with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="binance-archive") as pool:
            futures = {pool.submit(_fetch_archive, archive): archive for archive in pending_monthly}
            for future in as_completed(futures):
                archive = futures[future]
                monthly_results[archive.key] = future.result()

        work: list[tuple[str, str, Archive, bool]] = []
        for symbol, interval, monthly in plan:
            if payload.mode == "incremental" and monthly.key in manifest["archives"]:
                work.append((symbol, interval, monthly, True))
            else:
                monthly_content, _ = monthly_results[monthly.key]
                if monthly_content is not None:
                    _log(task_id, f"月归档可用 {monthly.url}", "success")
                    work.append((symbol, interval, monthly, False))
                else:
                    total += len(monthly.fallbacks) - 1
                    _publish(task_id, total_files=total)
                    _log(task_id, f"月归档不存在，拆分为 {len(monthly.fallbacks)} 个日归档", "warning")
                    work.extend((symbol, interval, daily, False) for daily in monthly.fallbacks)

        daily_results: dict[str, tuple[bytes | None, bytes | None]] = {}
        pending_daily = [archive for _, _, archive, known in work if not archive.fallbacks and not known and not (payload.mode == "incremental" and archive.key in manifest["archives"])]
        with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="binance-archive") as pool:
            futures = {pool.submit(_fetch_archive, archive): archive for archive in pending_daily}
            for future in as_completed(futures):
                archive = futures[future]
                daily_results[archive.key] = future.result()

        for completed, (symbol, interval, archive, known_complete) in enumerate(work, 1):
            if known_complete or (payload.mode == "incremental" and archive.key in manifest["archives"]):
                skipped += 1
                _log(task_id, f"已存在，跳过 {archive.key}", "muted")
            else:
                content, checksum = (monthly_results if archive.fallbacks else daily_results)[archive.key]
                if content is None:
                    missing_files += 1
                    _log(task_id, f"远端归档不存在，已跳过 {archive.url}", "warning")
                else:
                    digest = hashlib.sha256(content).hexdigest()
                    if checksum:
                        expected = checksum.decode("utf-8").strip().split()[0].lower()
                        if expected != digest:
                            raise ValueError(f"Binance CHECKSUM 校验失败: {archive.url}")
                    else:
                        _log(task_id, "远端未提供 CHECKSUM，继续处理 ZIP", "warning")
                    bars = parse_archive(content, instruments[symbol], interval, payload.start_date, payload.end_date)
                    if bars:
                        catalog.write_data(bars)
                        rows += len(bars)
                    manifest["archives"][archive.key] = {"sha256": digest, "rows": len(bars), "completed_at": _now()}
                    _save_manifest(manifest_path, manifest)
                    downloaded += 1
                    _log(task_id, f"转换并写入 {len(bars):,} 根 K 线", "success")
            _publish(task_id, completed_files=completed, progress=round(completed / max(1, total) * 100), rows=rows, downloaded_files=downloaded, skipped_files=skipped, missing_files=missing_files)
        _publish(task_id, status="completed", stage="完成", progress=100, finished_at=_now())
        _log(task_id, f"任务完成：新增 {rows:,} 根 K 线，下载 {downloaded} 个文件，跳过 {skipped} 个", "success")
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


@router.post("/downloads", status_code=202)
def create_download(payload: DownloadCreate):
    task_id = str(uuid.uuid4())
    task = {"id": task_id, "status": "queued", "stage": "等待执行", "progress": 0, "logs": [], "rows": 0, "completed_files": 0, "total_files": 0, "downloaded_files": 0, "skipped_files": 0, "missing_files": 0, "error": None, "created_at": _now(), "updated_at": _now(), "request": payload.model_dump(mode="json")}
    with _lock:
        _tasks[task_id] = task
    threading.Thread(target=run_download, args=(task_id, payload), daemon=True, name=f"binance-download-{task_id[:8]}").start()
    return task


@router.get("/downloads/{task_id}")
def get_download(task_id: str):
    with _lock:
        task = _tasks.get(task_id)
        if not task:
            raise HTTPException(404, "下载任务不存在或服务已重启")
        return dict(task)
