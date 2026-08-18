from __future__ import annotations

import base64
import hashlib
import os
import uuid
from datetime import UTC, datetime

import httpx
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
        }
    key = decrypt_api_key(config.api_key_encrypted)
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
        "updated_at": config.updated_at,
    }


async def get_config(db: AsyncSession) -> LlmConfiguration:
    config = await db.get(LlmConfiguration, 1)
    if not config:
        raise HTTPException(503, "尚未配置 LLM")
    return config


MAX_API_RETRIES = 5


def format_llm_error(exc: Exception) -> str:
    err_str = str(exc).strip()
    hint = ""
    if any(k in err_str for k in ("401", "authentication_failed", "Invalid API key", "unauthorized")):
        hint = "API Key 认证失败（401 Unauthorized）。请前往「系统设置 - LLM 配置」检查 API Key 是否正确有效。"
    elif any(k in err_str for k in ("404", "not_found", "model_not_found", "does not exist")):
        hint = "模型不存在或无权访问（404 Not Found）。请检查配置的模型名称与 Base URL 服务是否匹配。"
    elif any(k in err_str for k in ("429", "rate_limit", "overloaded", "insufficient_quota")):
        hint = "上游接口请求超限或额度不足（429 Too Many Requests / Overloaded）。请稍后重试或检查账户额度/并发配置。"
    elif any(k in err_str for k in ("ECONNREFUSED", "Connection refused", "Failed to connect", "getaddrinfo", "ETIMEDOUT")):
        hint = "无法连接到 LLM Base URL 服务。请检查 Base URL 地址及本地网络/代理连接。"

    if hint:
        return f"{hint}\n【底层错误】{err_str}"
    return err_str


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
        )
        db.add(config)
    else:
        if data.api_key:
            config.api_key_encrypted = encrypt_api_key(data.api_key)
    for field in (
        "base_url", "auth_type", "model", "small_fast_model", "timeout_seconds",
        "max_turns", "default_permission_mode",
    ):
        setattr(config, field, getattr(data, field))
    config.last_test_ok = None
    config.last_test_message = None
    await db.commit()
    await db.refresh(config)
    return config_out(config)


@router.post("/test")
async def test_llm_configuration(deep: bool = False, db: AsyncSession = Depends(get_db)):
    config = await get_config(db)
    from app.dsh.runtime import dsh_runtime
    
    test_prompt = "请测试工具调用能力，回复 quantlab-agent-ok" if deep else "请只回复 quantlab-ok"
    try:
        res_text, tool_calls, reasoning = await dsh_runtime.call_llm(
            messages=[{"role": "user", "content": test_prompt}],
            system_prompt="你是 QuantLab 测试助手。请简洁确认连接状态。",
            db_config=config,
        )
        if "[API Error" in res_text or "[LLM Exception]" in res_text:
            raise RuntimeError(res_text)
            
        config.last_test_ok = True
        config.last_test_message = res_text[:500] or "quantlab-ok"
    except Exception as exc:
        config.last_test_ok = False
        config.last_test_message = format_llm_error(exc)[:1000]

    config.last_tested_at = datetime.now(UTC)
    await db.commit()
    if not config.last_test_ok:
        raise HTTPException(502, config.last_test_message)
    return {"ok": True, "deep": deep, "message": config.last_test_message}


@router.post("/test-dsh")
async def test_dsh_configuration(db: AsyncSession = Depends(get_db)):
    """Test DeepSeek Harness LLM connectivity and Tool Calling capability."""
    return await test_llm_configuration(deep=True, db=db)
