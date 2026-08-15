from datetime import datetime, UTC
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from .db import get_db
from .git_versions import GitVersionError, backup_repo, backup_strategies_to_git
from .llm_config import decrypt_api_key, encrypt_api_key, mask_api_key
from .models import GitConfiguration
from .schemas import GitConfigurationUpdate

router = APIRouter(prefix="/api/settings/git", tags=["git-settings"])


def config_out(config: GitConfiguration | None) -> dict:
    if config is None:
        return {
            "configured": False,
            "repository_path": str(backup_repo()),
            "last_backup_at": None,
            "last_backup_ok": None,
            "last_backup_message": None,
        }
    return {
        "configured": bool(config.remote_url and config.username),
        "repository_path": str(backup_repo()),
        "remote_url": config.remote_url,
        "username": config.username,
        "password_masked": mask_api_key(decrypt_api_key(config.password_encrypted)) if config.password_encrypted else None,
        "auto_push": config.auto_push,
        "last_backup_at": config.last_backup_at,
        "last_backup_ok": config.last_backup_ok,
        "last_backup_message": config.last_backup_message,
        "updated_at": config.updated_at,
    }


@router.get("")
async def read_git_configuration(db: AsyncSession = Depends(get_db)):
    return config_out(await db.get(GitConfiguration, 1))


@router.put("")
async def save_git_configuration(data: GitConfigurationUpdate, db: AsyncSession = Depends(get_db)):
    config = await db.get(GitConfiguration, 1)
    if config is None:
        if not data.password:
            raise HTTPException(422, "首次配置必须填写 Git 密码或访问令牌")
        config = GitConfiguration(
            id=1,
            remote_url=data.remote_url,
            username=data.username,
            password_encrypted=encrypt_api_key(data.password),
            auto_push=data.auto_push,
        )
        db.add(config)
    else:
        config.remote_url = data.remote_url
        config.username = data.username
        config.auto_push = data.auto_push
        if data.password:
            config.password_encrypted = encrypt_api_key(data.password)
    await db.commit()
    await db.refresh(config)
    return config_out(config)


@router.post("/backup")
@router.post("/test")
async def manual_backup_to_git(db: AsyncSession = Depends(get_db)):
    config = await db.get(GitConfiguration, 1)
    if not config or not config.remote_url or not config.username:
        raise HTTPException(400, "尚未配置远程 Git 仓库信息，请先填写仓库 URL、账号及访问密码/令牌")

    password = decrypt_api_key(config.password_encrypted)
    now = datetime.now(UTC)
    try:
        result = backup_strategies_to_git(
            remote_url=config.remote_url,
            username=config.username,
            password=password,
        )
        config.last_backup_at = now
        config.last_backup_ok = True
        config.last_backup_message = result.get("message", "备份成功")
        await db.commit()
        return result
    except GitVersionError as exc:
        config.last_backup_at = now
        config.last_backup_ok = False
        config.last_backup_message = str(exc)
        await db.commit()
        raise HTTPException(502, f"远程 Git 备份失败：{exc}") from exc
