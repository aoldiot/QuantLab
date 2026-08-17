import secrets
from datetime import UTC, datetime, timedelta
from typing import Optional

import jwt
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware

from .config import settings

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    username: str


class UserOut(BaseModel):
    username: str
    authenticated: bool = True


class LogoutResponse(BaseModel):
    ok: bool = True
    message: str = "已成功登出"


def create_access_token(username: str, expires_delta: Optional[timedelta] = None) -> str:
    if expires_delta is None:
        expires_delta = timedelta(hours=settings.auth_token_expire_hours)
    expire = datetime.now(UTC) + expires_delta
    payload = {
        "sub": username,
        "exp": expire,
        "iat": datetime.now(UTC),
    }
    return jwt.encode(payload, settings.auth_jwt_secret, algorithm="HS256")


def verify_token(token: str) -> Optional[str]:
    if not token:
        return None
    try:
        payload = jwt.decode(token, settings.auth_jwt_secret, algorithms=["HS256"])
        username: str = payload.get("sub")
        if not username:
            return None
        return username
    except jwt.PyJWTError:
        return None


def extract_token(
    authorization: Optional[str] = Header(default=None),
    token: Optional[str] = Query(default=None),
) -> Optional[str]:
    if authorization:
        parts = authorization.strip().split()
        if len(parts) == 2 and parts[0].lower() == "bearer":
            return parts[1]
        elif len(parts) == 1:
            return parts[0]
    if token:
        return token
    return None


async def get_current_user(
    authorization: Optional[str] = Header(default=None),
    token: Optional[str] = Query(default=None),
) -> str:
    raw_token = extract_token(authorization=authorization, token=token)
    if not raw_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="未提供身份认证凭据，请先登录",
            headers={"WWW-Authenticate": "Bearer"},
        )
    username = verify_token(raw_token)
    if not username:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="登录凭证无效或已过期，请重新登录",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return username


@router.post("/login", response_model=LoginResponse)
async def login(data: LoginRequest):
    valid_username = secrets.compare_digest(data.username, settings.auth_username)
    valid_password = secrets.compare_digest(data.password, settings.auth_password)
    if not (valid_username and valid_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户名或密码错误",
            headers={"WWW-Authenticate": "Bearer"},
        )
    access_token = create_access_token(data.username)
    return LoginResponse(
        access_token=access_token,
        token_type="bearer",
        username=data.username,
    )


@router.get("/me", response_model=UserOut)
async def me(current_user: str = Depends(get_current_user)):
    return UserOut(username=current_user, authenticated=True)


@router.post("/logout", response_model=LogoutResponse)
async def logout():
    return LogoutResponse(ok=True, message="已成功登出")


class AuthMiddleware(BaseHTTPMiddleware):
    PUBLIC_EXACT_PATHS = {
        "/api/auth/login",
        "/api/auth/logout",
        "/api/health",
        "/docs",
        "/redoc",
        "/openapi.json",
        "/api/docs",
        "/api/redoc",
        "/api/openapi.json",
    }
    PUBLIC_PREFIX_PATHS = (
        "/api/research/tools/",
        "/api/health",
    )

    async def dispatch(self, request: Request, call_next):
        # Allow CORS preflight requests
        if request.method == "OPTIONS":
            return await call_next(request)

        path = request.url.path

        # Only protect /api/* endpoints
        if not path.startswith("/api/"):
            return await call_next(request)

        # Allow public whitelist paths
        if path in self.PUBLIC_EXACT_PATHS or any(path.startswith(prefix) for prefix in self.PUBLIC_PREFIX_PATHS):
            return await call_next(request)


        # Extract token from Header or Query Param
        auth_header = request.headers.get("Authorization")
        raw_token = None
        if auth_header:
            parts = auth_header.strip().split()
            if len(parts) == 2 and parts[0].lower() == "bearer":
                raw_token = parts[1]
            elif len(parts) == 1:
                raw_token = parts[0]
        if not raw_token:
            raw_token = request.query_params.get("token")

        if not raw_token or not verify_token(raw_token):
            response = JSONResponse(
                status_code=401,
                content={"detail": "未登录或登录已过期，请重新登录"},
                headers={"WWW-Authenticate": "Bearer"},
            )
            # Add CORS headers so frontend doesn't get blocked by browser
            origin = request.headers.get("origin")
            if origin:
                response.headers["Access-Control-Allow-Origin"] = origin
                response.headers["Access-Control-Allow-Credentials"] = "true"
                response.headers["Access-Control-Allow-Methods"] = "*"
                response.headers["Access-Control-Allow-Headers"] = "*"
            return response

        return await call_next(request)
