from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.strategy_files as strategy_files
from app.strategy_files import _template


def test_generated_strategy_templates_are_valid_python():
    for mode in ("SINGLE_INSTRUMENT", "PORTFOLIO"):
        source = _template("online_example", mode)
        compile(source, "online_example.py", "exec")
        assert f"mode=StrategyMode.{mode}" in source


def test_git_commit_endpoint_accepts_one_character_message(monkeypatch):
    calls = []

    def fake_git(*args):
        calls.append(args)
        if args[:3] == ("diff", "--cached", "--name-only"):
            return "backend/app/strategies/example.py"
        if args == ("rev-parse", "HEAD"):
            return "a" * 40
        return ""

    monkeypatch.setattr(strategy_files, "_git", fake_git)
    test_app = FastAPI()
    test_app.include_router(strategy_files.router)

    response = TestClient(test_app).post("/api/strategy-files/git/commit", json={"message": "修"})

    assert response.status_code == 200
    assert response.json()["commit"] == "a" * 40
    assert ("commit", "-m", "修", "--", "backend/app/strategies") in calls


def test_updates_draft_manifest_metadata(tmp_path, monkeypatch):
    monkeypatch.setattr(strategy_files, "STRATEGY_DIR", tmp_path)
    monkeypatch.setattr(strategy_files.settings, "strategy_repo_path", tmp_path)
    monkeypatch.setattr(strategy_files, "_git", lambda *args: "")
    path = tmp_path / "online_example.py"
    path.write_text(_template("online_example", "PORTFOLIO", "旧说明", "趋势"), encoding="utf-8")
    test_app = FastAPI()
    test_app.include_router(strategy_files.router)

    response = TestClient(test_app).patch(
        "/api/strategy-files/online_example/metadata",
        json={"description": "归档说明", "category": "归档"},
    )

    assert response.status_code == 200
    source = path.read_text(encoding="utf-8")
    assert "description='归档说明'" in source
    assert "category='归档'" in source
    compile(source, path.name, "exec")
