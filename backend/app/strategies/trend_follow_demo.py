# Test demo strategy
from nautilus_trader.config import StrategyConfig
from nautilus_trader.trading.strategy import Strategy
import pandas as pd
from app.strategy_contract import StrategyManifest

class TrendFollowDemoConfig(StrategyConfig):
    pass

class TrendFollowDemoStrategy(Strategy):
    pass

def calculate_indicators(df: pd.DataFrame, parameters: dict) -> pd.DataFrame:
    return df

STRATEGY_MANIFEST = StrategyManifest(
    name="Trend Follow Demo",
    slug="trend_follow_demo",
    description="Demo for testing",
    category="TREND",
    version="1.0.0",
)
