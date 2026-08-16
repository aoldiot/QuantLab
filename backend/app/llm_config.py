from __future__ import annotations

import base64
import hashlib
import os
import uuid
from datetime import UTC, datetime

import httpx
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
    if not value:
        return ""
    try:
        return _fernet().decrypt(value.encode()).decode()
    except InvalidToken:
        # Fallback to default key if encryption key changed
        default_digest = hashlib.sha256(b"change-me-in-production").digest()
        try:
            return Fernet(base64.urlsafe_b64encode(default_digest)).decrypt(value.encode()).decode()
        except Exception:
            return ""
    except Exception:
        return ""


def mask_api_key(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "••••••••"
    return f"{value[:4]}••••••••{value[-4:]}"


def config_out(config: LlmConfiguration | None) -> dict:
    if config is None:
        return {
            "configured": False,
            "hermes_configured": False,
        }
    key = decrypt_api_key(config.api_key_encrypted)
    hermes_key = decrypt_api_key(config.hermes_api_key_encrypted) if config.hermes_api_key_encrypted else ""
    return {
        "configured": bool(key and config.base_url and config.model),
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
        "hermes_configured": bool(config.hermes_base_url and config.hermes_model),
        "hermes_base_url": config.hermes_base_url,
        "hermes_api_key_masked": mask_api_key(hermes_key) if hermes_key else "",
        "hermes_model": config.hermes_model,
        "hermes_timeout_seconds": config.hermes_timeout_seconds,
        "hermes_last_test_ok": config.hermes_last_test_ok,
        "hermes_last_test_message": config.hermes_last_test_message,
        "hermes_last_tested_at": config.hermes_last_tested_at,
        "updated_at": config.updated_at,
    }


async def get_config(db: AsyncSession) -> LlmConfiguration:
    config = await db.get(LlmConfiguration, 1)
    if not config:
        raise HTTPException(503, "尚未配置 LLM")
    return config


async def get_hermes_config(db: AsyncSession | None = None) -> tuple[str, str, str, int]:
    """Returns (base_url, api_key, model, timeout_seconds) for Hermes from DB."""
    if db is None:
        raise HTTPException(503, "未提供数据库会话，无法获取 Hermes 配置")
    config = await db.get(LlmConfiguration, 1)
    if not config or not config.hermes_base_url or not config.hermes_model:
        raise HTTPException(503, "尚未配置 Hermes，请先前往系统设置配置")
    api_key = decrypt_api_key(config.hermes_api_key_encrypted) if config.hermes_api_key_encrypted else ""
    return (
        config.hermes_base_url,
        api_key,
        config.hermes_model,
        config.hermes_timeout_seconds or 600,
    )


MAX_API_RETRIES = 5


def sdk_env(config: LlmConfiguration) -> dict[str, str]:
    env = {
        "ANTHROPIC_BASE_URL": config.base_url.rstrip("/"),
        "API_TIMEOUT_MS": str((config.timeout_seconds or 120) * 1000),
        "CLAUDE_CODE_MAX_RETRIES": str(MAX_API_RETRIES),
    }
    key_name = "ANTHROPIC_API_KEY" if config.auth_type == "api_key" else "ANTHROPIC_AUTH_TOKEN"
    env[key_name] = decrypt_api_key(config.api_key_encrypted)
    if config.small_fast_model:
        env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = config.small_fast_model
    # Forward proxy envs to the Claude SDK subprocess if present
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy", "NO_PROXY", "no_proxy"):
        val = os.environ.get(var)
        if val:
            env[var] = val
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
        config = LlmConfiguration(
            id=1,
            base_url=data.base_url,
            model=data.model,
            api_key_encrypted=encrypt_api_key(data.api_key),
            hermes_base_url=data.hermes_base_url,
            hermes_model=data.hermes_model,
            hermes_timeout_seconds=data.hermes_timeout_seconds,
            hermes_api_key_encrypted=encrypt_api_key(data.hermes_api_key) if data.hermes_api_key else None,
        )
        db.add(config)
    else:
        if data.api_key:
            config.api_key_encrypted = encrypt_api_key(data.api_key)
        if data.hermes_api_key:
            config.hermes_api_key_encrypted = encrypt_api_key(data.hermes_api_key)
    for field in (
        "base_url", "auth_type", "model", "small_fast_model", "timeout_seconds",
        "max_turns", "default_permission_mode", "hermes_base_url", "hermes_model",
        "hermes_timeout_seconds",
    ):
        setattr(config, field, getattr(data, field))
    config.last_test_ok = None
    config.last_test_message = None
    config.hermes_last_test_ok = None
    config.hermes_last_test_message = None
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


@router.post("/test-hermes")
async def test_hermes_configuration(db: AsyncSession = Depends(get_db)):
    base_url, api_key, model, timeout_seconds = await get_hermes_config(db)
    config = await db.get(LlmConfiguration, 1)

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    body = {
        "model": model,
        "conversation": f"quantlab-test-{uuid.uuid4()}",
        "input": "测试连接。请只回复 quantlab-hermes-ok",
        "instructions": "严格只回复 quantlab-hermes-ok",
        "store": False,
    }
    timeout = httpx.Timeout(min(timeout_seconds, 30))
    result_text = ""
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(f"{base_url.rstrip('/')}/responses", headers=headers, json=body)
            resp.raise_for_status()
            data = resp.json()
            texts = []
            for item in data.get("output", []):
                for part in item.get("content", []):
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        texts.append(part["text"])
            if not texts and isinstance(data.get("output_text"), str):
                texts.append(data["output_text"])
            result_text = "\n".join(texts).strip() or resp.text
        if config:
            config.hermes_last_test_ok = True
            config.hermes_last_test_message = result_text[:500] or "Hermes 连接成功"
            config.hermes_last_tested_at = datetime.now(UTC)
            await db.commit()
    except Exception as exc:
        if config:
            config.hermes_last_test_ok = False
            config.hermes_last_test_message = str(exc)[:1000]
            config.hermes_last_tested_at = datetime.now(UTC)
            await db.commit()
        raise HTTPException(502, f"Hermes 连接失败：{exc}") from exc

    return {"ok": True, "message": result_text[:500] or "Hermes 连接成功"}
