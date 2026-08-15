from datetime import date
from io import BytesIO
from zipfile import ZipFile

from app.data_downloads import _manifest, archive_plan, make_instrument, parse_archive

INFO = {
    "symbol": "BTCUSDT",
    "baseAsset": "BTC",
    "quoteAsset": "USDT",
    "marginAsset": "USDT",
    "filters": [
        {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
        {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001", "maxQty": "1000.000"},
    ],
}


def test_archive_plan_uses_monthly_for_finished_historical_months():
    plan = archive_plan("BTCUSDT", "1m", date(2024, 1, 30), date(2024, 3, 2), "um")
    assert [item.key for item in plan] == [
        "um/BTCUSDT/1m/monthly/2024-01/2024-01-30_2024-01-31",
        "um/BTCUSDT/1m/monthly/2024-02/2024-02-01_2024-02-29",
        "um/BTCUSDT/1m/monthly/2024-03/2024-03-01_2024-03-02",
    ]
    assert [len(item.fallbacks) for item in plan] == [2, 29, 2]


def test_partial_historical_month_is_one_archive_per_symbol():
    plan = archive_plan("BTCUSDT", "1d", date(2026, 7, 1), date(2026, 7, 30), "um")
    assert len(plan) == 1
    assert plan[0].key == "um/BTCUSDT/1d/monthly/2026-07/2026-07-01_2026-07-30"
    assert len(plan[0].fallbacks) == 30


def test_parses_header_and_binance_millisecond_timestamp():
    content = BytesIO()
    with ZipFile(content, "w") as archive:
        archive.writestr(
            "BTCUSDT-1m.csv",
            "open_time,open,high,low,close,volume,close_time,q,n,tb,tq,i\n"
            "1722470400000,64000.10,64100.20,63900.00,64050.20,12.345,1722470459999,0,0,0,0,0\n",
        )
    bars = parse_archive(content.getvalue(), make_instrument(INFO, "um"), "1m", date(2024, 8, 1), date(2024, 8, 1))
    assert len(bars) == 1
    assert str(bars[0].bar_type) == "BTCUSDT-PERP.BINANCE-1-MINUTE-LAST-EXTERNAL"
    assert bars[0].ts_event == 1_722_470_459_999_000_000


def test_parses_binance_spot_microsecond_timestamp():
    content = BytesIO()
    with ZipFile(content, "w") as archive:
        archive.writestr(
            "BTCUSDT-1m.csv",
            "1735689600000000,90000.10,90100.20,89900.00,90050.20,1.234,1735689659999999,0,0,0,0,0\n",
        )
    bars = parse_archive(content.getvalue(), make_instrument(INFO, "spot"), "1m", date(2025, 1, 1), date(2025, 1, 1))
    assert len(bars) == 1
    assert bars[0].ts_event == 1_735_689_659_999_999_000


def test_binance_filter_padding_does_not_inflate_price_precision():
    info = {
        **INFO,
        "symbol": "SOLUSDT",
        "baseAsset": "SOL",
        "filters": [
            {"filterType": "PRICE_FILTER", "tickSize": "0.00100000"},
            {"filterType": "LOT_SIZE", "stepSize": "0.01000000", "minQty": "0.01", "maxQty": "100000"},
        ],
    }
    instrument = make_instrument(info, "um")
    assert instrument.price_precision == 3
    assert instrument.size_precision == 2


def test_bar_prices_are_coerced_to_instrument_precision():
    info = {
        **INFO,
        "symbol": "SOLUSDT",
        "baseAsset": "SOL",
        "filters": [
            {"filterType": "PRICE_FILTER", "tickSize": "0.00100000"},
            {"filterType": "LOT_SIZE", "stepSize": "0.01000000", "minQty": "0.01", "maxQty": "100000"},
        ],
    }
    content = BytesIO()
    with ZipFile(content, "w") as archive:
        archive.writestr(
            "SOLUSDT-1m.csv",
            "1722470400000,123.4,124.1200,122.98,123.4567,12.3456,1722470459999,0,0,0,0,0\n",
        )
    instrument = make_instrument(info, "um")
    bars = parse_archive(content.getvalue(), instrument, "1m", date(2024, 8, 1), date(2024, 8, 1))
    assert len(bars) == 1
    assert {bars[0].open.precision, bars[0].high.precision, bars[0].low.precision, bars[0].close.precision} == {3}
    assert bars[0].volume.precision == 2


def test_old_precision_manifest_forces_selected_data_to_rebuild(tmp_path):
    (tmp_path / ".quantlab-downloads.json").write_text(
        '{"version":1,"archives":{"um/SOLUSDT/1d/monthly/2026-01/x":{}}}'
    )
    _, manifest = _manifest(tmp_path)
    assert manifest == {"version": 2, "archives": {}}
