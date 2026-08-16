from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://quantlab:quantlab@localhost:5432/quantlab"
    redis_url: str = "redis://localhost:6380/0"
    cors_origins: list[str] = ["http://localhost:5173"]
    data_root: Path = BACKEND_DIR / "data"
    artifact_root: Path = BACKEND_DIR / "data" / "backtests"
    catalog_path: Path = BACKEND_DIR / "catalog"
    backtest_timeout_seconds: int = 3600
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
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

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


settings = Settings()
