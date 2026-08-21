from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..models import WorkerType


@dataclass(frozen=True)
class ErrorRoute:
    code: str
    worker: WorkerType | None
    auto_fix: bool = False
    retryable: bool = False


def classify_error(value: Any) -> ErrorRoute:
    """Deterministically route failures instead of sending all of them to REPAIR."""
    text = str(value or "").lower()
    if any(word in text for word in ("parameterspec", "strategy_manifest", "plot_config", "contract")):
        return ErrorRoute("CONTRACT_ERROR", WorkerType.CODING, auto_fix=True)
    if any(word in text for word in ("syntaxerror", "importerror", "modulenotfounderror", "traceback")):
        return ErrorRoute("STRATEGY_RUNTIME_ERROR", WorkerType.CODING)
    if any(word in text for word in ("catalog", "missing data", "no bars", "数据缺失")):
        return ErrorRoute("DATA_MISSING", WorkerType.BACKTEST, retryable=True)
    if any(word in text for word in ("timeout", "connection", "temporarily unavailable")):
        return ErrorRoute("INFRASTRUCTURE_ERROR", None, retryable=True)
    if any(word in text for word in ("drawdown", "sharpe", "poor performance", "收益")):
        return ErrorRoute("PERFORMANCE_RESULT", WorkerType.ANALYSIS)
    return ErrorRoute("UNKNOWN_ERROR", WorkerType.CODING)
