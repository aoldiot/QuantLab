from nautilus_trader.config import StrategyConfig
from app.strategy_contract import StrategyManifest
STRATEGY_MANIFEST = StrategyManifest(slug='btc_trend_test', name='BTC', description='', category='trend', strategy_path='', config_path='', parameters={}, timeframes=('1h',), primary_timeframe='1h')