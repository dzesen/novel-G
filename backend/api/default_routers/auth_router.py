from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from backend.services.auth.identity_service import (
    CSRF_COOKIE_NAME,
    SESSION_COOKIE_NAME,
    SESSION_TTL,
    Actor,
    AuthenticationError,
    AuthResult,
    AuthorizationError,
    IdentityConflictError,
    LoginRateLimitError,
    IdentityService,
    IdentityValidationError,
    get_identity_service,
)
from backend.db.errors import InvalidIdError, NotFoundError
from backend.network_mode import is_lan_access_enabled, is_loopback_host
from backend.services.auth.novel_access_service import (
    NovelAccessService,
    get_novel_access_service,
)


router = APIRouter(prefix="/api/auth", tags=["auth"])


class SetupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=3, max_length=64)
    display_name: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=12, max_length=256)


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class CreateUserRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=3, max_length=64)
    display_name: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=12, max_length=256)


class ResetPasswordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    password: str = Field(min_length=12, max_length=256)


def _user_view(actor: Actor) -> dict[str, str]:
    return {
        "id": actor.id,
        "username": actor.username,
        "display_name": actor.display_name,
        "role": actor.role,
        "status": actor.status,
    }


def _set_auth_cookies(response: Response, result: AuthResult) -> None:
    max_age = int(SESSION_TTL.total_seconds())
    response.set_cookie(
        SESSION_COOKIE_NAME,
        result.session_token,
        max_age=max_age,
        httponly=True,
        secure=False,
        samesite="lax",
        path="/",
    )
    response.set_cookie(
        CSRF_COOKIE_NAME,
        result.csrf_token,
        max_age=max_age,
        httponly=False,
        secure=False,
        samesite="lax",
        path="/",
    )


async def require_actor(
    session_token: Annotated[str | None, Cookie(alias=SESSION_COOKIE_NAME)] = None,
    service: IdentityService = Depends(get_identity_service),
) -> Actor:
    try:
        return await service.authenticate(session_token)
    except AuthenticationError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


async def require_csrf_actor(
    request: Request,
    actor: Actor = Depends(require_actor),
    service: IdentityService = Depends(get_identity_service),
) -> Actor:
    if not service.verify_csrf(
        actor,
        cookie_token=request.cookies.get(CSRF_COOKIE_NAME),
        header_token=request.headers.get("X-CSRF-Token"),
    ):
        raise HTTPException(status_code=403, detail="CSRF 校验失败")
    return actor


async def require_admin_actor(
    actor: Actor = Depends(require_actor),
) -> Actor:
    if not actor.is_admin:
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return actor


async def require_admin_csrf_actor(
    actor: Actor = Depends(require_csrf_actor),
) -> Actor:
    if not actor.is_admin:
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return actor


async def require_authenticated_request(
    request: Request,
    actor: Actor = Depends(require_actor),
    service: IdentityService = Depends(get_identity_service),
) -> Actor:
    """统一保护业务路由，并对所有非安全方法执行 Session 绑定的 CSRF 校验。"""
    if request.method.upper() not in {"GET", "HEAD", "OPTIONS"}:
        if not service.verify_csrf(
            actor,
            cookie_token=request.cookies.get(CSRF_COOKIE_NAME),
            header_token=request.headers.get("X-CSRF-Token"),
        ):
            raise HTTPException(status_code=403, detail="CSRF 校验失败")
    request.state.actor = actor
    return actor


async def require_admin_request(
    actor: Actor = Depends(require_authenticated_request),
) -> Actor:
    if not actor.is_admin:
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return actor


async def require_owned_path_resource(
    request: Request,
    actor: Actor = Depends(require_authenticated_request),
    access: NovelAccessService = Depends(get_novel_access_service),
) -> Actor:
    """按路由路径里的根或子资源 ID 解析所属小说并校验 owner。"""
    path = request.path_params
    try:
        if path.get("novel_id"):
            await access.require_owned_novel(
                actor,
                path["novel_id"],
                include_deleted=True,
            )
        elif path.get("volume_id"):
            await access.resolve_owned_resource(
                actor,
                resource_kind="volume",
                resource_id=path["volume_id"],
            )
        elif path.get("chapter_id"):
            await access.resolve_owned_resource(
                actor,
                resource_kind="chapter",
                resource_id=path["chapter_id"],
            )
        elif path.get("job_id"):
            await access.resolve_owned_resource(
                actor,
                resource_kind="job",
                resource_id=path["job_id"],
            )
        elif path.get("proposal_id"):
            await access.resolve_owned_resource(
                actor,
                resource_kind="state_proposal",
                resource_id=path["proposal_id"],
            )
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InvalidIdError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return actor


async def require_owned_body_resource(
    request: Request,
    actor: Actor = Depends(require_authenticated_request),
    access: NovelAccessService = Depends(get_novel_access_service),
) -> Actor:
    """解析 JSON 顶层的 novel/chapter/proposal ID，保护以请求体定位资源的路由。"""
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        return actor
    try:
        if payload.get("novel_id"):
            await access.require_owned_novel(
                actor,
                str(payload["novel_id"]),
                include_deleted=True,
            )
        elif payload.get("chapter_id"):
            await access.resolve_owned_resource(
                actor,
                resource_kind="chapter",
                resource_id=str(payload["chapter_id"]),
            )
        elif payload.get("proposal_id"):
            await access.resolve_owned_resource(
                actor,
                resource_kind="state_proposal",
                resource_id=str(payload["proposal_id"]),
            )
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InvalidIdError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return actor


@router.get("/setup-status")
async def setup_status(
    service: IdentityService = Depends(get_identity_service),
) -> dict[str, bool]:
    return {"setup_required": await service.setup_status()}


@router.post("/setup", status_code=status.HTTP_201_CREATED)
async def setup_initial_admin(
    request: SetupRequest,
    response: Response,
    http_request: Request,
    service: IdentityService = Depends(get_identity_service),
) -> dict[str, object]:
    client_host = http_request.client.host if http_request.client else None
    if is_lan_access_enabled() and not is_loopback_host(client_host):
        raise HTTPException(
            status_code=403,
            detail="局域网模式下必须从运行服务的本机完成初始管理员设置",
        )
    try:
        result = await service.setup_initial_admin(**request.model_dump())
    except IdentityConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except IdentityValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    _set_auth_cookies(response, result)
    return {"user": _user_view(result.actor), "csrf_token": result.csrf_token}


@router.post("/login")
async def login(
    request: LoginRequest,
    response: Response,
    service: IdentityService = Depends(get_identity_service),
) -> dict[str, object]:
    try:
        result = await service.login(**request.model_dump())
    except LoginRateLimitError as exc:
        raise HTTPException(
            status_code=429,
            detail=str(exc),
            headers={"Retry-After": str(exc.retry_after_seconds)},
        ) from exc
    except (AuthenticationError, IdentityValidationError) as exc:
        raise HTTPException(status_code=401, detail="用户名或密码错误") from exc
    _set_auth_cookies(response, result)
    return {"user": _user_view(result.actor), "csrf_token": result.csrf_token}


@router.get("/me")
async def me(
    request: Request,
    actor: Actor = Depends(require_actor),
) -> dict[str, object]:
    csrf_token = request.cookies.get(CSRF_COOKIE_NAME)
    if not csrf_token:
        raise HTTPException(status_code=401, detail="会话的 CSRF 凭据缺失")
    return {"user": _user_view(actor), "csrf_token": csrf_token}


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    response: Response,
    actor: Actor = Depends(require_csrf_actor),
    service: IdentityService = Depends(get_identity_service),
) -> Response:
    await service.logout(actor)
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    response.delete_cookie(CSRF_COOKIE_NAME, path="/")
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@router.get("/users")
async def list_users(
    actor: Actor = Depends(require_admin_actor),
    service: IdentityService = Depends(get_identity_service),
) -> dict[str, object]:
    return {"data": await service.list_users(actor)}


@router.post("/users", status_code=status.HTTP_201_CREATED)
async def create_user(
    request: CreateUserRequest,
    actor: Actor = Depends(require_admin_csrf_actor),
    service: IdentityService = Depends(get_identity_service),
) -> dict[str, object]:
    try:
        user = await service.create_user(actor, **request.model_dump())
    except IdentityConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (IdentityValidationError, AuthorizationError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"user": user}


async def _change_status(
    user_id: str,
    next_status: str,
    actor: Actor,
    service: IdentityService,
) -> dict[str, object]:
    try:
        user = await service.set_user_status(
            actor,
            user_id,
            next_status=next_status,
        )
    except AuthorizationError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except IdentityValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"user": user}


@router.post("/users/{user_id}/disable")
async def disable_user(
    user_id: str,
    actor: Actor = Depends(require_admin_csrf_actor),
    service: IdentityService = Depends(get_identity_service),
) -> dict[str, object]:
    return await _change_status(user_id, "disabled", actor, service)


@router.post("/users/{user_id}/enable")
async def enable_user(
    user_id: str,
    actor: Actor = Depends(require_admin_csrf_actor),
    service: IdentityService = Depends(get_identity_service),
) -> dict[str, object]:
    return await _change_status(user_id, "active", actor, service)


@router.post("/users/{user_id}/reset-password")
async def reset_password(
    user_id: str,
    request: ResetPasswordRequest,
    actor: Actor = Depends(require_admin_csrf_actor),
    service: IdentityService = Depends(get_identity_service),
) -> dict[str, object]:
    try:
        user = await service.reset_password(
            actor,
            user_id,
            password=request.password,
        )
    except AuthorizationError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except IdentityValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"user": user}
