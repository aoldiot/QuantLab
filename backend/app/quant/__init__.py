"""QuantLab Quantitative Domain Capabilities Package.

Encapsulates core deterministic quantification, factor analysis, vectorized experiment,
backtest execution, parameter sweep, robustness evaluation, and strategy management.
"""

from ..strategy_base import QuantLabStrategy
from .backtest import run_nautilus_backtest
from .experiment import StrategyCandidate, run_vectorized_experiment
from .factor_analysis import compute_technical_factor, evaluate_factor
from .indicators import (
    ATRTrailingStopTracker,
    IncWilderADX,
    SqueezeStateTracker,
    calc_standard_indicators,
)
from .market_data import compute_market_stats, get_catalog_instruments, load_market_bars
from .parameter_sweep import run_parameter_sweep
from .robustness import (
    calculate_deflated_sharpe_ratio,
    run_monte_carlo_stress_test,
    run_walk_forward_analysis,
)
from .strategy_manager import (
    ensure_strategy_db_record,
    get_strategy_code,
    save_strategy_file,
)

__all__ = [
    "ATRTrailingStopTracker",
    "IncWilderADX",
    "QuantLabStrategy",
    "SqueezeStateTracker",
    "StrategyCandidate",
    "calc_standard_indicators",
    "calculate_deflated_sharpe_ratio",
    "compute_market_stats",
    "compute_technical_factor",
    "ensure_strategy_db_record",
    "evaluate_factor",
    "get_catalog_instruments",
    "get_strategy_code",
    "load_market_bars",
    "run_monte_carlo_stress_test",
    "run_nautilus_backtest",
    "run_parameter_sweep",
    "run_vectorized_experiment",
    "run_walk_forward_analysis",
    "save_strategy_file",
]


