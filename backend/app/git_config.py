from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from .db import get_db
from .git_versions import GitVersionError, push_revision, strategy_repo
from .llm_config import decrypt_api_key, encrypt_api_key, mask_api_key
from .models import GitConfiguration
from .schemas import GitConfigurationUpdate

router = APIRouter(prefix="/api/settings/git", tags=["git-settings"])


def config_out(config: GitConfiguration | None) -> dict:
    if config is None:
        return {"configured": False, "repository_path": str(strategy_repo())}
    return {"configured": True, "repository_path": str(strategy_repo()), "remote_url": config.remote_url,
            "username": config.username, "password_masked": mask_api_key(decrypt_api_key(config.password_encrypted)),
            "auto_push": config.auto_push, "updated_at": config.updated_at}


async def push_credentials(db: AsyncSession) -> tuple[str, str, str] | None:
    config = await db.get(GitConfiguration, 1)
    if not config or not config.auto_push:
        return None
    return config.remote_url, config.username, decrypt_api_key(config.password_encrypted)


@router.get("")
async def read_git_configuration(db: AsyncSession = Depends(get_db)):
    return config_out(await db.get(GitConfiguration, 1))


@router.put("")
async def save_git_configuration(data: GitConfigurationUpdate, db: AsyncSession = Depends(get_db)):
    config = await db.get(GitConfiguration, 1)
    if config is None:
        if not data.password:
            raise HTTPException(422, "首次配置必须填写 Git 密码或访问令牌")
        config = GitConfiguration(id=1, remote_url=data.remote_url, username=data.username,
                                  password_encrypted=encrypt_api_key(data.password), auto_push=data.auto_push)
        db.add(config)
    else:
        config.remote_url, config.username, config.auto_push = data.remote_url, data.username, data.auto_push
        if data.password:
            config.password_encrypted = encrypt_api_key(data.password)
    await db.commit()
    await db.refresh(config)
    return config_out(config)


@router.post("/test")
async def test_git_push(db: AsyncSession = Depends(get_db)):
    config = await db.get(GitConfiguration, 1)
    if not config:
        raise HTTPException(503, "尚未配置远程 Git")
    try:
        push_revision(strategy_repo(), config.remote_url, config.username, decrypt_api_key(config.password_encrypted))
    except GitVersionError as exc:
        raise HTTPException(502, str(exc)) from exc
    return {"ok": True, "message": "策略仓库已成功推送到远程"}
