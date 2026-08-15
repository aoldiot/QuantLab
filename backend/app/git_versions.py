from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .config import settings
from .strategy_contract import StrategyManifest


class GitVersionError(ValueError):
    pass


@dataclass(frozen=True)
class GitRevision:
    repo: Path
    commit: str
    ref: str
    manifest_hash: str


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


def strategy_repo() -> Path:
    """Return the dedicated repository used only for strategy source history."""
    repo = settings.strategy_git_repo_path.expanduser().resolve()
    repo.mkdir(parents=True, exist_ok=True)
    if not (repo / ".git").exists():
        result = subprocess.run(["git", "init", "-b", "main", str(repo)], capture_output=True, text=True, check=False)
        if result.returncode:
            _git(repo, "init")
        _git(repo, "config", "user.name", "QuantLab")
        _git(repo, "config", "user.email", "quantlab@local")
        target = repo / "backend" / "app" / "strategies"
        target.mkdir(parents=True, exist_ok=True)
        source = Path(__file__).resolve().parent / "strategies"
        for path in source.glob("*.py"):
            shutil.copy2(path, target / path.name)
        _git(repo, "add", "--", "backend/app/strategies")
        if _git_optional(repo, "diff", "--cached", "--name-only"):
            _git(repo, "commit", "-m", "strategy: initialize dedicated repository")
    return repo


def sync_strategy_file(name: str, source: Path | None = None) -> str:
    repo = strategy_repo()
    relative = f"backend/app/strategies/{name}.py"
    source = source or Path(__file__).resolve().parent / "strategies" / f"{name}.py"
    target = repo / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.exists():
        shutil.copy2(source, target)
    elif target.exists():
        target.unlink()
    return relative


def push_revision(repo: Path, remote_url: str, username: str, password: str) -> None:
    current = _git_optional(repo, "remote", "get-url", "origin")
    _git(repo, "remote", "set-url" if current else "add", "origin", remote_url)
    script = "#!/bin/sh\ncase \"$1\" in *Username*) printf '%s' \"$QUANTLAB_GIT_USERNAME\";; *) printf '%s' \"$QUANTLAB_GIT_PASSWORD\";; esac\n"
    fd, askpass = tempfile.mkstemp(prefix="quantlab-git-askpass-")
    try:
        os.write(fd, script.encode())
        os.close(fd)
        os.chmod(askpass, 0o700)
        env = {**os.environ, "GIT_ASKPASS": askpass, "GIT_TERMINAL_PROMPT": "0", "QUANTLAB_GIT_USERNAME": username, "QUANTLAB_GIT_PASSWORD": password}
        result = subprocess.run(["git", "-C", str(repo), "push", "-u", "origin", "HEAD"], capture_output=True, text=True, check=False, env=env)
        if result.returncode:
            raise GitVersionError(result.stderr.strip() or "推送策略仓库失败")
    finally:
        try:
            os.unlink(askpass)
        except FileNotFoundError:
            pass


def manifest_hash(manifest: StrategyManifest) -> str:
    payload = {
        "slug": manifest.slug,
        "version": manifest.version,
        "strategy_path": manifest.strategy_path,
        "config_path": manifest.config_path,
        "parameters": manifest.parameter_schema(),
        "data_requirements": manifest.data_requirements(),
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def resolve_revision(manifest: StrategyManifest, require_clean: bool = True) -> GitRevision:
    repo = strategy_repo()
    relative = sync_strategy_file(manifest.strategy_path.partition(":")[0].rsplit(".", 1)[-1])
    if require_clean and _git(repo, "status", "--porcelain", "--", relative):
        raise GitVersionError("策略代码存在未提交修改，请先 commit 再发布策略版本")
    commit = _git(repo, "rev-parse", "HEAD")
    ref = _git_optional(repo, "symbolic-ref", "--short", "-q", "HEAD") or "DETACHED"
    return GitRevision(repo=repo, commit=commit, ref=ref, manifest_hash=manifest_hash(manifest))


def publish_revision(manifest: StrategyManifest, description: str | None = None, remote: tuple[str, str, str] | None = None) -> GitRevision:
    """Return a clean revision, committing strategy workspace changes when needed."""
    repo = strategy_repo()
    module = manifest.strategy_path.partition(":")[0]
    strategy_path = sync_strategy_file(module.rsplit(".", 1)[-1])
    changed = bool(_git(repo, "status", "--porcelain", "--", strategy_path))
    if not changed and not remote:
        raise GitVersionError("策略代码没有发生改变，不能发布新版本")
    if changed:
        _git(repo, "add", "--", strategy_path)
        message = description or f"strategy: publish {manifest.slug} v{manifest.version}"
        _git(repo, "commit", "-m", message, "--", strategy_path)
    if remote:
        push_revision(repo, *remote)
    # Other strategy drafts may remain dirty; this commit already contains the
    # exact source file represented by this published version.
    return resolve_revision(manifest, require_clean=False)


def resolve_export_repo(recorded_repo: Path, commit: str) -> Path:
    """Resolve a movable repository path while preserving commit pinning."""
    candidates = [recorded_repo.expanduser()]
    configured = settings.strategy_git_repo_path.expanduser()
    if configured not in candidates:
        candidates.append(configured)

    errors: list[str] = []
    for candidate in candidates:
        try:
            repo = Path(_git(candidate, "rev-parse", "--show-toplevel")).resolve()
            _git(repo, "cat-file", "-e", f"{commit}^{{commit}}")
            return repo
        except GitVersionError as exc:
            errors.append(f"{candidate}: {exc}")
    raise GitVersionError(f"找不到包含 Git commit {commit} 的仓库；" + "；".join(errors))


def export_revision(repo: Path, commit: str, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    archive = subprocess.Popen(["git", "-C", str(repo), "archive", commit], stdout=subprocess.PIPE)
    extract = subprocess.run(["tar", "-x", "-C", str(target)], stdin=archive.stdout, capture_output=True, text=True)
    if archive.stdout:
        archive.stdout.close()
    archive_code = archive.wait()
    if archive_code or extract.returncode:
        raise GitVersionError(extract.stderr.strip() or f"无法导出 Git commit {commit}")
