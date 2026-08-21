from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class StrategyCreate(BaseModel):
    module: str
    version_description: str = Field(min_length=1, max_length=500)


class StrategyUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = None
    category: str | None = Field(default=None, min_length=1, max_length=50)
    status: Literal["DRAFT", "READY", "DISABLED"] | None = None


class StrategyVersionCreate(BaseModel):
    module: str
    description: str = Field(min_length=1, max_length=500)


class StrategyVersionOut(BaseModel):
    id: str
    strategy_id: str
    version: str
    entrypoint: str
    code: str = ""
    code_hash: str | None = None
    parameter_schema: dict[str, Any]
    data_requirements: dict[str, Any]
    is_latest: bool = False
    git_commit: str | None = None
    git_ref: str | None = None
    manifest_hash: str | None = None
    created_at: datetime | None = None
    description: str = ""


class StrategyFileCreate(BaseModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    mode: Literal["SINGLE_INSTRUMENT", "PORTFOLIO"] = "PORTFOLIO"
    description: str = Field(min_length=1, max_length=500)
    category: str = Field(min_length=1, max_length=50)


class StrategyFileUpdate(BaseModel):
    content: str = Field(max_length=1_000_000)


class StrategyFileMetadataUpdate(BaseModel):
    description: str = Field(min_length=1, max_length=500)
    category: str = Field(min_length=1, max_length=50)


class StrategyAiRequest(BaseModel):
    instruction: str = Field(min_length=1, max_length=4000)
    content: str = Field(max_length=1_000_000)


class GitCommitCreate(BaseModel):
    message: str = Field(min_length=1, max_length=200)


class GitConfigurationUpdate(BaseModel):
    remote_url: str = Field(min_length=1, max_length=1000, pattern=r"^https?://")
    username: str = Field(min_length=1, max_length=300)
    password: str | None = Field(default=None, max_length=2000)
    auto_push: bool = True


class StrategyOut(BaseModel):
    id: str
    name: str
    slug: str
    description: str
    category: str
    status: str
    latest_version_id: str
    version: str
    parameter_schema: dict[str, Any]
    data_requirements: dict[str, Any]
    version_count: int
    module: str
    created_at: datetime | None = None
    updated_at: datetime | None = None


class CatalogCheckRequest(BaseModel):
    symbols: list[str] = Field(min_length=1)
    timeframes: list[str] = Field(min_length=1)
    start_date: date
    end_date: date
    venue: str = "BINANCE"
    market_type: Literal["spot", "um"] = "um"
    catalog_path: str | None = None


class CatalogMissingDetail(BaseModel):
    symbol: str
    instrument_id: str
    timeframe: str
    status: Literal["MISSING_INSTRUMENT", "MISSING_DATA", "PARTIAL_RANGE", "OK"]
    message: str


class CatalogCheckResponse(BaseModel):
    ok: bool
    has_missing: bool
    catalog_exists: bool
    catalog_path: str
    missing_symbols: list[str]
    details: list[CatalogMissingDetail]
    summary_text: str


class BacktestCreate(BaseModel):
    name: str
    strategy_version_id: str
    strategy_parameters: dict[str, Any]
    venue: str = "BINANCE"
    market_type: Literal["spot", "um"] = "um"
    symbols: list[str] = Field(min_length=1)
    timeframes: list[str] = Field(min_length=1)
    start_date: date
    end_date: date
    initial_balance: float = Field(gt=0)
    leverage: float = Field(gt=0, le=125)
    execution_model: Literal["FAST", "STANDARD", "CONSERVATIVE"] = "CONSERVATIVE"
    catalog_path: str | None = None
    chunk_size: int | None = Field(default=None, gt=0)
    ignore_missing_data: bool = True
    check_data_integrity: bool = True
    research_project_id: str | None = None

    def model_post_init(self, __context, /):
        if self.end_date <= self.start_date:
            raise ValueError("结束日期必须晚于开始日期")


class BacktestConfirmRequest(BaseModel):
    ignore_missing_data: bool = True


class BacktestLogsOut(BaseModel):
    id: str
    status: str
    stage: str
    progress: int
    logs: str
    error_message: str | None = None


class BacktestOut(BaseModel):
    id: str
    name: str
    status: str
    stage: str
    progress: int
    config: dict[str, Any]
    metrics: dict[str, Any] | None
    result: dict[str, Any] | None
    error_message: str | None
    research_project_id: str | None = None
    created_at: datetime


PermissionMode = Literal["plan", "default", "acceptEdits", "bypassPermissions"]


class LlmConfigurationUpdate(BaseModel):
    base_url: str = Field(min_length=1, max_length=500)
    api_key: str | None = Field(default=None, max_length=2000)
    auth_type: Literal["api_key", "auth_token"] = "api_key"
    model: str = Field(min_length=1, max_length=200)
    small_fast_model: str | None = Field(default=None, max_length=200)
    timeout_seconds: int = Field(default=120, ge=10, le=1800)
    max_turns: int = Field(default=30, ge=1, le=200)
    default_permission_mode: PermissionMode = "default"


class ResearchProjectCreate(BaseModel):
    client_id: str = Field(default="default_client", max_length=100)
    title: str = Field(min_length=1, max_length=200)
    original_idea: str = Field(default="", max_length=20_000)
    source_project_id: str | None = Field(default=None, max_length=36)


class ResearchMessageCreate(BaseModel):
    content: str = Field(min_length=1, max_length=30_000)


class DshActionRequest(BaseModel):
    action: Literal["WRITE_STRATEGY", "RUN_BACKTEST", "FIX_ERROR", "ANALYZE_BACKTEST"]
    content: str = Field(default="", max_length=4000)
    run_id: str | None = Field(default=None, max_length=64)
    arguments: dict[str, Any] = Field(default_factory=dict)


class DshApproveRequest(BaseModel):
    request_id: str = Field(min_length=1, max_length=64)
    approved: bool
    feedback: str = Field(default="", max_length=2000)


class StrategySpecificationUpdate(BaseModel):
    content: dict[str, Any]


class ResearchDecisionResolve(BaseModel):
    answer: str = Field(min_length=1, max_length=2000)


class ResearchImplementationCreate(BaseModel):
    client_id: str = Field(default="default_client", max_length=100)
    permission_mode: PermissionMode = "acceptEdits"
    force: bool = False


class ResearchConclusionUpdate(BaseModel):
    verdict: Literal["SUPPORTED", "REJECTED", "INCONCLUSIVE"]
    summary: str = Field(min_length=1, max_length=20_000)
    next_step: str = Field(default="", max_length=20_000)


class ResearchIterationCreate(BaseModel):
    target: Literal["DISCUSSING", "SPEC_REVIEW", "READY_FOR_BACKTEST"]
    reason: str = Field(min_length=1, max_length=5_000)


class DashboardRecentRun(BaseModel):
    id: str
    name: str
    status: str
    stage: str
    progress: int
    strategy_name: str | None = None
    venue: str | None = None
    timeframes: list[str] = []
    symbols: list[str] = []
    total_return: float | None = None
    sharpe: float | None = None
    created_at: datetime


class StrategyStats(BaseModel):
    total_strategies: int
    registered_strategies: int
    draft_strategies: int
    categories: dict[str, int]


class BacktestStats(BaseModel):
    total_runs: int
    running_runs: int
    completed_runs: int
    failed_runs: int
    canceled_runs: int
    win_rate: float | None = None
    avg_return: float | None = None
    avg_sharpe: float | None = None
    recent_runs: list[DashboardRecentRun]


class ResearchStats(BaseModel):
    total_projects: int
    active_projects: int
    archived_projects: int


class CatalogStats(BaseModel):
    total_symbols: int
    total_bars: int
    total_size_bytes: int
    available_timeframes: list[str]


class SystemHealthStats(BaseModel):
    llm_configured: bool
    db_ok: bool
    engine_status: str


class DashboardStatsOut(BaseModel):
    strategies: StrategyStats
    backtests: BacktestStats
    research: ResearchStats
    catalog: CatalogStats
    system: SystemHealthStats
