from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query
from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .db import get_db
from .models import LlmConfiguration
from .schemas import LlmConfigurationUpdate

router = APIRouter(prefix="/api/settings/llm", tags=["llm-settings"])


def _fernet() -> Fernet:
    digest = hashlib.sha256(settings.llm_secret_encryption_key.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_api_key(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt_api_key(value: str) -> str:
    try:
        return _fernet().decrypt(value.encode()).decode()
    except InvalidToken as exc:
        raise RuntimeError("LLM API Key 无法解密，请检查 LLM_SECRET_ENCRYPTION_KEY") from exc


def mask_api_key(value: str) -> str:
    if len(value) <= 8:
        return "••••••••"
    return f"{value[:4]}••••••••{value[-4:]}"


def config_out(config: LlmConfiguration | None) -> dict:
    if config is None:
        return {"configured": False}
    key = decrypt_api_key(config.api_key_encrypted)
    return {
        "configured": True,
        "base_url": config.base_url,
        "api_key_masked": mask_api_key(key),
        "auth_type": config.auth_type,
        "model": config.model,
        "small_fast_model": config.small_fast_model,
        "timeout_seconds": config.timeout_seconds,
        "max_turns": config.max_turns,
        "default_permission_mode": config.default_permission_mode,
        "last_test_ok": config.last_test_ok,
        "last_test_message": config.last_test_message,
        "last_tested_at": config.last_tested_at,
        "updated_at": config.updated_at,
    }


async def get_config(db: AsyncSession) -> LlmConfiguration:
    config = await db.get(LlmConfiguration, 1)
    if not config:
        raise HTTPException(503, "尚未配置 LLM")
    return config


def sdk_env(config: LlmConfiguration) -> dict[str, str]:
    env = {
        "ANTHROPIC_BASE_URL": config.base_url.rstrip("/"),
        "API_TIMEOUT_MS": str(config.timeout_seconds * 1000),
        "CLAUDE_CODE_MAX_RETRIES": "2",
    }
    key_name = "ANTHROPIC_API_KEY" if config.auth_type == "api_key" else "ANTHROPIC_AUTH_TOKEN"
    env[key_name] = decrypt_api_key(config.api_key_encrypted)
    if config.small_fast_model:
        env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = config.small_fast_model
    return env


@router.get("")
async def read_llm_configuration(db: AsyncSession = Depends(get_db)):
    return config_out(await db.get(LlmConfiguration, 1))


@router.put("")
async def save_llm_configuration(data: LlmConfigurationUpdate, db: AsyncSession = Depends(get_db)):
    config = await db.get(LlmConfiguration, 1)
    if config is None:
        if not data.api_key:
            raise HTTPException(422, "首次配置必须填写 API Key")
        config = LlmConfiguration(id=1, base_url=data.base_url, model=data.model, api_key_encrypted=encrypt_api_key(data.api_key))
        db.add(config)
    elif data.api_key:
        config.api_key_encrypted = encrypt_api_key(data.api_key)
    for field in ("base_url", "auth_type", "model", "small_fast_model", "timeout_seconds", "max_turns", "default_permission_mode"):
        setattr(config, field, getattr(data, field))
    config.last_test_ok = None
    config.last_test_message = None
    await db.commit()
    await db.refresh(config)
    return config_out(config)


@router.post("/test")
async def test_llm_configuration(deep: bool = False, db: AsyncSession = Depends(get_db)):
    config = await get_config(db)
    prompt = "使用 Bash 工具执行 printf quantlab-agent-ok，并只回复命令输出。" if deep else "只回复 quantlab-ok"
    options = ClaudeAgentOptions(
        model=config.model,
        env=sdk_env(config),
        tools=["Bash"] if deep else [],
        allowed_tools=["Bash(printf quantlab-agent-ok)"] if deep else [],
        permission_mode="dontAsk",
        setting_sources=[],
        max_turns=2,
    )
    result_text = ""
    try:
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, ResultMessage):
                result_text = message.result or ""
                if message.is_error:
                    raise RuntimeError(result_text or message.subtype)
        config.last_test_ok = True
        config.last_test_message = result_text[:500] or "连接成功"
    except Exception as exc:
        config.last_test_ok = False
        config.last_test_message = str(exc)[:1000]
    config.last_tested_at = datetime.now(UTC)
    await db.commit()
    if not config.last_test_ok:
        raise HTTPException(502, config.last_test_message)
    return {"ok": True, "deep": deep, "message": config.last_test_message}
