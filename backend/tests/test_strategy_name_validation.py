import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.agent.strategy_verifier import verify_strategy_source
from app.schemas import StrategyFileCreate
from app.strategy_contract import StrategyManifest, StrategyMode
from app.strategy_files import _path


def test_filename_strict_regex_validation():
    # Valid names should pass
    valid_path = _path("volatility_squeeze_breakout")
    assert valid_path.name == "volatility_squeeze_breakout.py"

    # Invalid names with hyphens, uppercase, spaces, or leading digits should raise HTTPException(400)
    with pytest.raises(HTTPException):
        _path("volatility-squeeze-breakout")

    with pytest.raises(HTTPException):
        _path("VolatilitySqueezeBreakout")

    with pytest.raises(HTTPException):
        _path("15m_ema_cross")

    with pytest.raises(HTTPException):
        _path("__init__")


def test_schema_strict_regex_validation():
    # Valid
    m1 = StrategyFileCreate(name="ema_cross_trend", description="test", category="trend")
    assert m1.name == "ema_cross_trend"

    # Invalid hyphen
    with pytest.raises(ValidationError):
        StrategyFileCreate(name="ema-cross-trend", description="test", category="trend")


def test_strategy_manifest_supported_modes_tolerance():
    # Test that passing supported_modes does not crash and auto-maps mode
    manifest = StrategyManifest(
        slug="volatility_squeeze_breakout",
        supported_modes=[StrategyMode.SINGLE_INSTRUMENT],
    )
    assert manifest.slug == "volatility_squeeze_breakout"
    assert manifest.mode == StrategyMode.SINGLE_INSTRUMENT
