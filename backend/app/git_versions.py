from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

from .config import settings
from .strategy_contract import StrategyManifest


class GitVersionError(ValueError):
    pass


def code_hash(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def manifest_hash(manifest: StrategyManifest) -> str:
    payload = {
        "slug": manifest.slug,
        "version": manifest.version,
        "strategy_path": manifest.strategy_path,
        "config_path": manifest.config_path,
        "parameters": manifest.parameter_schema(),
        "data_requirements": manifest.data_requirements(),
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise GitVersionError(result.stderr.strip() or "Git 命令执行失败")
    return result.stdout.strip()


def _git_optional(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=False)
    return result.stdout.strip() if result.returncode == 0 else ""


def backup_repo() -> Path:
    """Return the dedicated repository used for manual strategy backups."""
    repo = settings.strategy_git_repo_path.expanduser().resolve()
    repo.mkdir(parents=True, exist_ok=True)
    if not (repo / ".git").exists():
        result = subprocess.run(["git", "init", "-b", "main", str(repo)], capture_output=True, text=True, check=False)
        if result.returncode:
            _git(repo, "init")
        _git(repo, "config", "user.name", "QuantLab Backup")
        _git(repo, "config", "user.email", "backup@quantlab.local")
    return repo


def sync_all_strategy_files_to_backup(repo: Path) -> list[str]:
    """Copy all active strategy files from backend/app/strategies into backup repo."""
    target_dir = repo / "strategies"
    target_dir.mkdir(parents=True, exist_ok=True)
    source_dir = Path(__file__).resolve().parent / "strategies"

    source_files = {p.name: p for p in source_dir.glob("*.py") if p.name != "__init__.py"}
    existing_files = {p.name: p for p in target_dir.glob("*.py") if p.name != "__init__.py"}

    # Copy / update source files
    for name, src_path in source_files.items():
        shutil.copy2(src_path, target_dir / name)

    # Remove deleted files from backup repo
    for name, tgt_path in existing_files.items():
        if name not in source_files:
            tgt_path.unlink(missing_ok=True)

    # Ensure __init__.py exists
    (target_dir / "__init__.py").write_text("", encoding="utf-8")

    return list(source_files.keys())


def push_backup(repo: Path, remote_url: str, username: str, password: str) -> None:
    current = _git_optional(repo, "remote", "get-url", "origin")
    _git(repo, "remote", "set-url" if current else "add", "origin", remote_url)
    script = '#!/bin/sh\ncase "$1" in *Username*) printf \'%s\' "$QUANTLAB_GIT_USERNAME";; *) printf \'%s\' "$QUANTLAB_GIT_PASSWORD";; esac\n'
    fd, askpass = tempfile.mkstemp(prefix="quantlab-git-askpass-")
    try:
        os.write(fd, script.encode())
        os.close(fd)
        os.chmod(askpass, 0o700)
        env = {
            **os.environ,
            "GIT_ASKPASS": askpass,
            "GIT_TERMINAL_PROMPT": "0",
            "QUANTLAB_GIT_USERNAME": username,
            "QUANTLAB_GIT_PASSWORD": password,
        }
        result = subprocess.run(
            ["git", "-C", str(repo), "push", "-u", "origin", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
        if result.returncode:
            raise GitVersionError(result.stderr.strip() or "推送备份到远程 Git 仓库失败")
    finally:
        try:
            os.unlink(askpass)
        except FileNotFoundError:
            pass


def backup_strategies_to_git(
    remote_url: str,
    username: str,
    password: str,
    message: str | None = None,
) -> dict:
    """Commit all local strategies and push to user-specified remote repository."""
    if not remote_url or not username or not password:
        raise GitVersionError("请先完整填写远程 Git 仓库 URL、账号和访问密码/令牌")

    repo = backup_repo()
    backed_up_names = sync_all_strategy_files_to_backup(repo)
    _git(repo, "add", "--", "strategies")

    commit_msg = message or f"strategies backup: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ({len(backed_up_names)} strategies)"
    if _git_optional(repo, "diff", "--cached", "--name-only"):
        _git(repo, "commit", "-m", commit_msg)

    commit_hash = _git_optional(repo, "rev-parse", "HEAD")
    push_backup(repo, remote_url, username, password)

    return {
        "ok": True,
        "files_count": len(backed_up_names),
        "files": backed_up_names,
        "commit": commit_hash[:8] if commit_hash else "",
        "message": f"成功备份 {len(backed_up_names)} 个策略文件到远程 Git 仓库",
    }
