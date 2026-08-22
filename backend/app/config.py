from pathlib import Path

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    deployment_mode: str = "development"
    database_url: str = "postgresql+asyncpg://quantlab:quantlab@localhost:5432/quantlab"
    redis_url: str = "redis://localhost:6380/0"
    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173", "http://0.0.0.0:5173"]
    data_root: Path = BACKEND_DIR / "data"
    artifact_root: Path = BACKEND_DIR / "data" / "backtests"
    catalog_path: Path = BACKEND_DIR / "catalog"
    backtest_timeout_seconds: int = 3600
    backtest_sandbox: bool = True
    instrument_id_template: str = "{symbol}-PERP.{venue}"
    strategy_repo_path: Path = PROJECT_ROOT
    strategy_git_repo_path: Path = BACKEND_DIR / "data" / "strategy-repository"
    llm_secret_encryption_key: str = "change-me-in-production"
    agent_max_concurrency: int = 5
    agent_workspace_retention_days: int = 7
    data_download_concurrency: int = 32
    auth_username: str = "admin"
    auth_password: str = "admin123"
    auth_jwt_secret: str = "quantlab-secure-jwt-secret-key-2026-production"
    auth_token_expire_hours: int = 168
    dsh_bridge_token: str = ""
    dsh_max_tokens: int = 32768
    dsh_bridge_url: str = "http://127.0.0.1:8000/api"
    dsh_research_timeout_seconds: int = 180
    dsh_research_max_tool_calls: int = 5
    dsh_tool_result_max_chars: int = 8000
    dsh_candidate_code_max_chars: int = 200000
    dsh_auto_repair_max_attempts: int = 2
    dsh_require_action_approvals: bool = False
    dsh_quiet_poll_access_logs: bool = True
    dsh_web_search_timeout_seconds: int = 12
    # Keep local execution and Docker Compose on the repository-root .env.
    # Resolving this path avoids changing configuration merely because uvicorn
    # was started from backend/ rather than the repository root.
    model_config = SettingsConfigDict(env_file=PROJECT_ROOT / ".env", env_file_encoding="utf-8", extra="ignore")

    @field_validator("data_root", "artifact_root", "catalog_path", "strategy_git_repo_path", mode="after")
    @classmethod
    def resolve_backend_relative_path(cls, v: Path) -> Path:
        if not v.is_absolute():
            return (BACKEND_DIR / v).resolve()
        return v.resolve()

    @field_validator("strategy_repo_path", mode="after")
    @classmethod
    def resolve_repo_relative_path(cls, v: Path) -> Path:
        if not v.is_absolute():
            return (PROJECT_ROOT / v).resolve()
        return v.resolve()

    @model_validator(mode="after")
    def reject_insecure_production_defaults(self):
        if self.deployment_mode.lower() == "production":
            insecure = []
            if self.auth_password == "admin123":
                insecure.append("AUTH_PASSWORD")
            if self.auth_jwt_secret == "quantlab-secure-jwt-secret-key-2026-production":
                insecure.append("AUTH_JWT_SECRET")
            if not self.dsh_bridge_token:
                insecure.append("DSH_BRIDGE_TOKEN")
            if "*" in self.cors_origins:
                insecure.append("CORS_ORIGINS")
            if self.llm_secret_encryption_key == "change-me-in-production":
                insecure.append("LLM_SECRET_ENCRYPTION_KEY")
            if insecure:
                raise ValueError("生产模式拒绝不安全默认配置，请设置：" + ", ".join(insecure))
        return self


settings = Settings()
