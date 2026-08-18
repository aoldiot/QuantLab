import pytest
from httpx import ASGITransport, AsyncClient

from app.auth import create_access_token
from app.db import engine
from app.main import app


@pytest.mark.anyio
async def test_dashboard_stats_endpoint():
    await engine.dispose()
    token = create_access_token("admin")
    headers = {"Authorization": f"Bearer {token}"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        res = await ac.get("/api/dashboard/stats", headers=headers)
        assert res.status_code == 200
        data = res.json()
        assert "strategies" in data
        assert "backtests" in data
        assert "research" in data
        assert "catalog" in data
        assert "system" in data
        assert isinstance(data["strategies"]["total_strategies"], int)
        assert isinstance(data["backtests"]["total_runs"], int)
        assert isinstance(data["backtests"]["recent_runs"], list)
        assert isinstance(data["research"]["total_projects"], int)
