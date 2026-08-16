import pytest
from httpx import ASGITransport, AsyncClient

from app.auth import create_access_token, verify_token
from app.config import settings
from app.main import app


def test_token_creation_and_verification():
    token = create_access_token("admin")
    assert token is not None
    username = verify_token(token)
    assert username == "admin"

    assert verify_token("invalid-token") is None
    assert verify_token("") is None


@pytest.mark.anyio
async def test_auth_login_success():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        response = await ac.post("/api/auth/login", json={
            "username": settings.auth_username,
            "password": settings.auth_password,
        })
        assert response.status_code == 200
        data = response.json()
        assert "access_token" in data
        assert data["token_type"] == "bearer"
        assert data["username"] == settings.auth_username

        token = data["access_token"]
        assert verify_token(token) == settings.auth_username


@pytest.mark.anyio
async def test_auth_login_failure():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        response = await ac.post("/api/auth/login", json={
            "username": "wrong_user",
            "password": "wrong_password",
        })
        assert response.status_code == 401
        assert "用户名或密码错误" in response.json()["detail"]


@pytest.mark.anyio
async def test_auth_me_endpoint():
    token = create_access_token("admin")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        # Without token
        res_unauth = await ac.get("/api/auth/me")
        assert res_unauth.status_code == 401

        # With valid token
        res_auth = await ac.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert res_auth.status_code == 200
        assert res_auth.json() == {"username": "admin", "authenticated": True}


@pytest.mark.anyio
async def test_public_and_protected_routes():
    token = create_access_token("admin")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        # Health check is public
        res_health = await ac.get("/api/health")
        assert res_health.status_code == 200

        # Protected route without token -> 401
        res_no_token = await ac.get("/api/strategies")
        assert res_no_token.status_code == 401

        # Protected route with invalid token -> 401
        res_bad_token = await ac.get("/api/strategies", headers={"Authorization": "Bearer bad-token"})
        assert res_bad_token.status_code == 401

        # Protected route with valid token -> not 401
        res_ok = await ac.get("/api/strategy-files", headers={"Authorization": f"Bearer {token}"})
        assert res_ok.status_code == 200


@pytest.mark.anyio
async def test_auth_logout():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        res = await ac.post("/api/auth/logout")
        assert res.status_code == 200
        assert res.json()["ok"] is True
