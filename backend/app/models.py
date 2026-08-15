import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


class StrategyStatus(str, enum.Enum):
    DRAFT = "DRAFT"
    READY = "READY"
    DISABLED = "DISABLED"


class RunStatus(str, enum.Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    ANALYZING = "ANALYZING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELED = "CANCELED"


class AgentSessionStatus(str, enum.Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    IDLE = "IDLE"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELED = "CANCELED"


class ResearchStatus(str, enum.Enum):
    DISCUSSING = "DISCUSSING"
    SPEC_REVIEW = "SPEC_REVIEW"
    IMPLEMENTING = "IMPLEMENTING"
    CODE_REVIEW = "CODE_REVIEW"
    READY_FOR_BACKTEST = "READY_FOR_BACKTEST"
    BACKTESTING = "BACKTESTING"
    READY_FOR_ANALYSIS = "READY_FOR_ANALYSIS"
    ANALYZING = "ANALYZING"
    RESULT_REVIEW = "RESULT_REVIEW"
    ARCHIVED = "ARCHIVED"


class SpecificationStatus(str, enum.Enum):
    DRAFT = "DRAFT"
    APPROVED = "APPROVED"
    SUPERSEDED = "SUPERSEDED"


class DecisionStatus(str, enum.Enum):
    PENDING = "PENDING"
    RESOLVED = "RESOLVED"
    DISMISSED = "DISMISSED"


class Strategy(Base):
    __tablename__ = "strategies"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(120))
    slug: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    category: Mapped[str] = mapped_column(String(50), default="趋势")
    status: Mapped[StrategyStatus] = mapped_column(Enum(StrategyStatus), default=StrategyStatus.READY)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    versions: Mapped[list["StrategyVersion"]] = relationship(back_populates="strategy", cascade="all, delete-orphan")


class StrategyVersion(Base):
    __tablename__ = "strategy_versions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    strategy_id: Mapped[str] = mapped_column(ForeignKey("strategies.id"))
    version: Mapped[str] = mapped_column(String(30))
    entrypoint: Mapped[str] = mapped_column(String(255))
    code: Mapped[str] = mapped_column(Text, default="")
    code_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    parameter_schema: Mapped[dict] = mapped_column(JSON)
    data_requirements: Mapped[dict] = mapped_column(JSON)
    description: Mapped[str] = mapped_column(Text, default="")
    git_commit: Mapped[str | None] = mapped_column(String(40), nullable=True)
    git_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    git_repo: Mapped[str | None] = mapped_column(String(500), nullable=True)
    manifest_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    strategy: Mapped[Strategy] = relationship(back_populates="versions")


class BacktestRun(Base):
    __tablename__ = "backtest_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(160))
    strategy_version_id: Mapped[str] = mapped_column(ForeignKey("strategy_versions.id"))
    status: Mapped[RunStatus] = mapped_column(Enum(RunStatus), default=RunStatus.QUEUED, index=True)
    stage: Mapped[str] = mapped_column(String(80), default="等待执行")
    progress: Mapped[int] = mapped_column(Integer, default=0)
    config: Mapped[dict] = mapped_column(JSON)
    metrics: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    research_project_id: Mapped[str | None] = mapped_column(ForeignKey("research_projects.id", ondelete="SET NULL"), nullable=True, index=True)


class LlmConfiguration(Base):
    __tablename__ = "llm_configuration"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    base_url: Mapped[str] = mapped_column(String(500))
    api_key_encrypted: Mapped[str] = mapped_column(Text)
    auth_type: Mapped[str] = mapped_column(String(20), default="api_key")
    model: Mapped[str] = mapped_column(String(200))
    small_fast_model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=120)
    max_turns: Mapped[int] = mapped_column(Integer, default=30)
    default_permission_mode: Mapped[str] = mapped_column(String(30), default="default")
    last_test_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    last_test_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_tested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    hermes_base_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    hermes_api_key_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    hermes_model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    hermes_timeout_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True, default=600)
    hermes_last_test_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    hermes_last_test_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    hermes_last_tested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class GitConfiguration(Base):
    __tablename__ = "git_configuration"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    remote_url: Mapped[str] = mapped_column(String(1000), default="")
    username: Mapped[str] = mapped_column(String(300), default="")
    password_encrypted: Mapped[str] = mapped_column(Text, default="")
    auto_push: Mapped[bool] = mapped_column(Boolean, default=False)
    last_backup_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_backup_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    last_backup_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class AgentSession(Base):
    __tablename__ = "agent_sessions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    client_id: Mapped[str] = mapped_column(String(100), index=True)
    strategy_name: Mapped[str] = mapped_column(String(64), index=True)
    permission_mode: Mapped[str] = mapped_column(String(30), default="default")
    status: Mapped[AgentSessionStatus] = mapped_column(Enum(AgentSessionStatus), default=AgentSessionStatus.IDLE, index=True)
    sdk_session_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    workspace_path: Mapped[str] = mapped_column(String(500))
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    research_project_id: Mapped[str | None] = mapped_column(ForeignKey("research_projects.id", ondelete="SET NULL"), nullable=True, index=True)
    specification_id: Mapped[str | None] = mapped_column(ForeignKey("strategy_specifications.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    messages: Mapped[list["AgentMessage"]] = relationship(back_populates="session", cascade="all, delete-orphan")


class AgentMessage(Base):
    __tablename__ = "agent_messages"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    session_id: Mapped[str] = mapped_column(ForeignKey("agent_sessions.id"), index=True)
    role: Mapped[str] = mapped_column(String(30))
    event_type: Mapped[str] = mapped_column(String(50), default="message")
    content: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    session: Mapped[AgentSession] = relationship(back_populates="messages")


class ResearchProject(Base):
    __tablename__ = "research_projects"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    client_id: Mapped[str] = mapped_column(String(100), index=True)
    title: Mapped[str] = mapped_column(String(200))
    original_idea: Mapped[str] = mapped_column(Text)
    status: Mapped[ResearchStatus] = mapped_column(Enum(ResearchStatus), default=ResearchStatus.DISCUSSING, index=True)
    hermes_conversation: Mapped[str] = mapped_column(String(200), unique=True)
    strategy_id: Mapped[str | None] = mapped_column(ForeignKey("strategies.id", ondelete="SET NULL"), nullable=True)
    implementation_session_id: Mapped[str | None] = mapped_column(ForeignKey("agent_sessions.id", ondelete="SET NULL"), nullable=True)
    latest_backtest_id: Mapped[str | None] = mapped_column(ForeignKey("backtest_runs.id", ondelete="SET NULL"), nullable=True)
    conclusion_verdict: Mapped[str | None] = mapped_column(String(30), nullable=True)
    conclusion_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    conclusion_next_step: Mapped[str | None] = mapped_column(Text, nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class ResearchMessage(Base):
    __tablename__ = "research_messages"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id: Mapped[str] = mapped_column(ForeignKey("research_projects.id"), index=True)
    role: Mapped[str] = mapped_column(String(30))
    content: Mapped[str] = mapped_column(Text)
    message_type: Mapped[str] = mapped_column(String(40), default="message")
    metadata_json: Mapped[dict] = mapped_column("metadata", JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class StrategySpecification(Base):
    __tablename__ = "strategy_specifications"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id: Mapped[str] = mapped_column(ForeignKey("research_projects.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[SpecificationStatus] = mapped_column(Enum(SpecificationStatus), default=SpecificationStatus.DRAFT)
    content: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ResearchDecision(Base):
    """A strategy design choice Hermes raised that only the user can settle."""

    __tablename__ = "research_decisions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id: Mapped[str] = mapped_column(ForeignKey("research_projects.id"), index=True)
    question: Mapped[str] = mapped_column(Text)
    options: Mapped[list] = mapped_column(JSON, default=list)
    recommendation: Mapped[str | None] = mapped_column(Text, nullable=True)
    impact: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[DecisionStatus] = mapped_column(Enum(DecisionStatus), default=DecisionStatus.PENDING, index=True)
    answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    origin: Mapped[str] = mapped_column(String(20), default="DISCUSSION")
    source_message_id: Mapped[str | None] = mapped_column(ForeignKey("research_messages.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
