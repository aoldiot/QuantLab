import pytest
from pathlib import Path

from app.git_versions import GitVersionError, export_revision, manifest_hash, publish_revision, resolve_export_repo, resolve_revision
from app.main import next_patch_version
from app.strategy_contract import load_manifest


def test_resolves_and_exports_committed_strategy(tmp_path, monkeypatch):
    monkeypatch.setattr("app.git_versions.settings.strategy_git_repo_path", tmp_path / "repo")
    manifest = load_manifest("app.strategies.macd_btc")
    revision = resolve_revision(manifest, require_clean=False)

    assert len(revision.commit) == 40
    assert revision.manifest_hash == manifest_hash(manifest)

    export_revision(revision.repo, revision.commit, tmp_path / "export")
    assert (tmp_path / "export/backend/app/strategies/macd_btc.py").exists()


def test_export_repo_falls_back_when_recorded_path_moved(tmp_path, monkeypatch):
    monkeypatch.setattr("app.git_versions.settings.strategy_git_repo_path", tmp_path / "repo")
    manifest = load_manifest("app.strategies.macd_btc")
    revision = resolve_revision(manifest, require_clean=False)
    assert resolve_export_repo(tmp_path / "old-location", revision.commit) == revision.repo


def test_publish_revision_commits_dirty_strategy(monkeypatch):
    manifest = load_manifest("app.strategies.macd_btc")
    calls = []

    def fake_git(repo, *args):
        calls.append(args)
        if args[:2] == ("status", "--porcelain"):
            return " M backend/app/strategies/macd_btc.py" if len([c for c in calls if c[:2] == ("status", "--porcelain")]) == 1 else ""
        if args == ("rev-parse", "HEAD"):
            return "a" * 40
        if args[:3] == ("diff", "--cached", "--name-only"):
            return "backend/app/strategies/atr_trend.py"
        if args[:3] == ("symbolic-ref", "--short", "-q"):
            return "main"
        return ""

    monkeypatch.setattr("app.git_versions._git", fake_git)
    monkeypatch.setattr("app.git_versions._git_optional", fake_git)
    monkeypatch.setattr("app.git_versions.strategy_repo", lambda: Path("/tmp/strategy-repo"))
    monkeypatch.setattr("app.git_versions.sync_strategy_file", lambda name: f"backend/app/strategies/{name}.py")
    revision = publish_revision(manifest)

    assert revision.commit == "a" * 40
    path = "backend/app/strategies/macd_btc.py"
    assert ("add", "--", path) in calls
    assert ("commit", "-m", f"strategy: publish {manifest.slug} v{manifest.version}", "--", path) in calls


def test_publish_revision_rejects_unchanged_strategy(monkeypatch):
    manifest = load_manifest("app.strategies.macd_btc")

    def fake_git(repo, *args):
        if args == ("rev-parse", "--show-toplevel"):
            return str(repo)
        if args[:2] == ("status", "--porcelain"):
            return ""
        return ""

    monkeypatch.setattr("app.git_versions._git", fake_git)
    monkeypatch.setattr("app.git_versions.strategy_repo", lambda: Path("/tmp/strategy-repo"))
    monkeypatch.setattr("app.git_versions.sync_strategy_file", lambda name: f"backend/app/strategies/{name}.py")
    with pytest.raises(GitVersionError, match="没有发生改变"):
        publish_revision(manifest, "test release")


def test_next_patch_version():
    assert next_patch_version("1.0.0") == "1.0.1"
    assert next_patch_version("2.7.19") == "2.7.20"
