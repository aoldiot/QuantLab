from __future__ import annotations

from .models import ResearchProject, ResearchStatus


PHASE_STATUS: dict[str, ResearchStatus] = {
    "RESEARCH": ResearchStatus.DISCUSSING,
    "AWAITING_IMPLEMENTATION_APPROVAL": ResearchStatus.SPEC_REVIEW,
    "IMPLEMENTATION": ResearchStatus.IMPLEMENTING,
    "IMPLEMENTED": ResearchStatus.READY_FOR_BACKTEST,
    "REPAIR": ResearchStatus.CODE_REVIEW,
    "FIX_ERROR": ResearchStatus.CODE_REVIEW,
    "AWAITING_BACKTEST_APPROVAL": ResearchStatus.READY_FOR_BACKTEST,
    "BACKTEST": ResearchStatus.BACKTESTING,
    "BACKTEST_RETRY": ResearchStatus.READY_FOR_BACKTEST,
    "RESULT_REVIEW": ResearchStatus.RESULT_REVIEW,
}


def apply_research_phase(project: ResearchProject, phase: str) -> None:
    """Keep the durable workflow phase and lifecycle status in one transition."""
    normalized = (phase or "").upper()
    if normalized not in PHASE_STATUS:
        raise ValueError(f"未知研究阶段: {phase}")
    project.research_phase = normalized
    if project.status != ResearchStatus.ARCHIVED:
        project.status = PHASE_STATUS[normalized]
