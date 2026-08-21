from __future__ import annotations

from dataclasses import dataclass

from ..models import WorkerType


@dataclass(frozen=True)
class WorkerProfile:
    worker: WorkerType
    tool_budget: int
    retry_budget: int
    cordis_name: str


PROFILES = {
    WorkerType.RESEARCH: WorkerProfile(WorkerType.RESEARCH, 8, 1, "cordis-research.yml"),
    WorkerType.CODING: WorkerProfile(WorkerType.CODING, 15, 3, "cordis-coding.yml"),
    WorkerType.BACKTEST: WorkerProfile(WorkerType.BACKTEST, 8, 2, "cordis-backtest.yml"),
    WorkerType.ANALYSIS: WorkerProfile(WorkerType.ANALYSIS, 6, 1, "cordis-analysis.yml"),
}


def worker_for_phase(phase: str) -> WorkerType:
    normalized = (phase or "").upper()
    if normalized == "RESEARCH":
        return WorkerType.RESEARCH
    if normalized in {"IMPLEMENTATION", "IMPLEMENTED", "REPAIR", "FIX_ERROR"}:
        return WorkerType.CODING
    if normalized in {"BACKTEST", "BACKTEST_RETRY"}:
        return WorkerType.BACKTEST
    if normalized == "RESULT_REVIEW":
        return WorkerType.ANALYSIS
    return WorkerType.CODING
