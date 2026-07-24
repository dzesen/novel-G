from __future__ import annotations

import hashlib
import hmac
import secrets
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import anyio
from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from bson import ObjectId
from pymongo import ASCENDING, IndexModel
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import DuplicateKeyError

from backend.db import collections
from backend.db.mongo import get_database
from backend.db.utils import to_object_id


SESSION_COOKIE_NAME = "novel_g_session"
CSRF_COOKIE_NAME = "novel_g_csrf"
SESSION_TTL = timedelta(days=7)
PASSWORD_MIN_LENGTH = 12
LOGIN_FAILURE_WINDOW = timedelta(minutes=10)
LOGIN_FAILURE_LIMIT = 5
LOGIN_LOCKOUT = timedelta(minutes=15)


class AuthenticationError(Exception):
    """凭据或会话无效。"""


class LoginRateLimitError(AuthenticationError):
    """同一登录标识在短时间内连续失败过多。"""

    def __init__(self, retry_after_seconds: int):
        super().__init__("登录尝试过多，请稍后重试")
        self.retry_after_seconds = max(1, retry_after_seconds)


class AuthorizationError(Exception):
    """当前用户没有执行操作的权限。"""


class IdentityConflictError(Exception):
    """用户名或首次初始化槽位冲突。"""


class IdentityValidationError(Exception):
    """身份输入不满足本地账户约束。"""


@dataclass(frozen=True)
class Actor:
    id: str
    username: str
    display_name: str
    role: str
    status: str
    session_id: str
    csrf_digest: str

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


@dataclass(frozen=True)
class AuthResult:
    actor: Actor
    session_token: str
    csrf_token: str


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _normalize_username(value: str) -> tuple[str, str]:
    username = unicodedata.normalize("NFKC", value).strip()
    normalized = username.casefold()
    if not 3 <= len(username) <= 64:
        raise IdentityValidationError("用户名长度必须为 3 到 64 个字符")
    if any(char.isspace() for char in username):
        raise IdentityValidationError("用户名不能包含空白字符")
    if any(not (char.isalnum() or char in "._-@") for char in username):
        raise IdentityValidationError("用户名只能包含字母、数字、汉字及 . _ - @")
    return username, normalized


def _validate_password(password: str) -> None:
    if len(password) < PASSWORD_MIN_LENGTH:
        raise IdentityValidationError(
            f"密码至少需要 {PASSWORD_MIN_LENGTH} 个字符"
        )
    if len(password) > 256:
        raise IdentityValidationError("密码不能超过 256 个字符")


class IdentityService:
    """本地用户、Argon2id 密码与不透明服务端会话的深模块。"""

    _hasher = PasswordHasher(
        time_cost=2,
        memory_cost=19_456,
        parallelism=1,
        hash_len=32,
        salt_len=16,
        type=Type.ID,
    )
    _dummy_hash: str | None = None

    def __init__(self, database: AsyncDatabase | None = None):
        self._database = database

    @property
    def db(self) -> AsyncDatabase:
        return self._database if self._database is not None else get_database()

    async def ensure_indexes(self) -> None:
        await self.db[collections.USERS].create_indexes([
            IndexModel(
                [("normalized_username", ASCENDING)],
                unique=True,
                partialFilterExpression={"is_deleted": False},
                name="users_active_normalized_username_unique",
            ),
            IndexModel(
                [("bootstrap_slot", ASCENDING)],
                unique=True,
                partialFilterExpression={
                    "bootstrap_slot": "initial",
                    "is_deleted": False,
                },
                name="users_initial_admin_unique",
            ),
        ])
        await self.db[collections.AUTH_SESSIONS].create_indexes([
            IndexModel(
                [("token_digest", ASCENDING)],
                unique=True,
                name="auth_sessions_token_digest_unique",
            ),
            IndexModel(
                [("expires_at", ASCENDING)],
                expireAfterSeconds=0,
                name="auth_sessions_expiry_ttl",
            ),
        ])
        await self.db[collections.AUTH_LOGIN_ATTEMPTS].create_indexes([
            IndexModel(
                [("expires_at", ASCENDING)],
                expireAfterSeconds=0,
                name="auth_login_attempts_expiry_ttl",
            ),
        ])

    async def setup_status(self) -> bool:
        initial = await self.db[collections.USERS].find_one(
            {"bootstrap_slot": "initial", "is_deleted": False},
            projection={"_id": 1},
        )
        return initial is None

    async def setup_initial_admin(
        self,
        *,
        username: str,
        display_name: str,
        password: str,
    ) -> AuthResult:
        await self.ensure_indexes()
        username, normalized_username = _normalize_username(username)
        display_name = unicodedata.normalize("NFKC", display_name).strip()
        if not 1 <= len(display_name) <= 64:
            raise IdentityValidationError("显示名称长度必须为 1 到 64 个字符")
        _validate_password(password)
        password_hash = await anyio.to_thread.run_sync(self._hasher.hash, password)
        now = _utc_now()
        user = {
            "username": username,
            "normalized_username": normalized_username,
            "display_name": display_name,
            "password_hash": password_hash,
            "role": "admin",
            "status": "active",
            "session_version": 1,
            "bootstrap_slot": "initial",
            "created_at": now,
            "updated_at": now,
            "is_deleted": False,
            "deleted_at": None,
        }
        try:
            result = await self.db[collections.USERS].insert_one(user)
        except DuplicateKeyError as exc:
            raise IdentityConflictError("初始管理员已经存在") from exc

        user["_id"] = result.inserted_id
        try:
            await self.db[collections.NOVELS].update_many(
                {
                    "$or": [
                        {"owner_id": {"$exists": False}},
                        {"owner_id": None},
                    ]
                },
                {
                    "$set": {
                        "owner_id": result.inserted_id,
                        "created_by": result.inserted_id,
                        "creation_source": "manual",
                        "updated_at": now,
                    }
                },
            )
        except Exception:
            await self.db[collections.USERS].delete_one({"_id": result.inserted_id})
            raise

        return await self._create_session(user)

    async def login(self, *, username: str, password: str) -> AuthResult:
        _, normalized_username = _normalize_username(username)
        await self._check_login_rate_limit(normalized_username)
        try:
            _validate_password(password)
        except IdentityValidationError as exc:
            await self._record_login_failure(normalized_username)
            raise AuthenticationError("用户名或密码错误") from exc
        user = await self.db[collections.USERS].find_one(
            {
                "normalized_username": normalized_username,
                "is_deleted": False,
            }
        )
        candidate_hash = (
            user.get("password_hash")
            if user
            else await self._get_dummy_hash()
        )
        try:
            verified = await anyio.to_thread.run_sync(
                self._hasher.verify,
                candidate_hash,
                password,
            )
        except (VerifyMismatchError, InvalidHashError):
            verified = False
        if not user or not verified or user.get("status") != "active":
            await self._record_login_failure(normalized_username)
            raise AuthenticationError("用户名或密码错误")

        await self.db[collections.AUTH_LOGIN_ATTEMPTS].delete_one(
            {"_id": _digest(normalized_username)}
        )
        if self._hasher.check_needs_rehash(candidate_hash):
            upgraded = await anyio.to_thread.run_sync(self._hasher.hash, password)
            await self.db[collections.USERS].update_one(
                {"_id": user["_id"]},
                {"$set": {"password_hash": upgraded, "updated_at": _utc_now()}},
            )
            user["password_hash"] = upgraded
        return await self._create_session(user)

    async def _check_login_rate_limit(self, normalized_username: str) -> None:
        now = _utc_now()
        record = await self.db[collections.AUTH_LOGIN_ATTEMPTS].find_one(
            {"_id": _digest(normalized_username)}
        )
        if not record:
            return
        locked_until = record.get("locked_until")
        if isinstance(locked_until, datetime):
            if locked_until.tzinfo is None:
                locked_until = locked_until.replace(tzinfo=timezone.utc)
            if locked_until > now:
                retry_after = int((locked_until - now).total_seconds() + 0.999)
                raise LoginRateLimitError(retry_after)

    async def _record_login_failure(self, normalized_username: str) -> None:
        now = _utc_now()
        key = _digest(normalized_username)
        record = await self.db[collections.AUTH_LOGIN_ATTEMPTS].find_one({"_id": key})
        window_started_at = record.get("window_started_at") if record else None
        if isinstance(window_started_at, datetime) and window_started_at.tzinfo is None:
            window_started_at = window_started_at.replace(tzinfo=timezone.utc)
        if (
            not isinstance(window_started_at, datetime)
            or now - window_started_at >= LOGIN_FAILURE_WINDOW
        ):
            window_started_at = now
            failure_count = 1
        else:
            failure_count = int(record.get("failure_count", 0)) + 1

        locked_until = (
            now + LOGIN_LOCKOUT
            if failure_count >= LOGIN_FAILURE_LIMIT
            else None
        )
        await self.db[collections.AUTH_LOGIN_ATTEMPTS].update_one(
            {"_id": key},
            {
                "$set": {
                    "failure_count": failure_count,
                    "window_started_at": window_started_at,
                    "locked_until": locked_until,
                    "expires_at": now + LOGIN_FAILURE_WINDOW + LOGIN_LOCKOUT,
                    "updated_at": now,
                },
                "$setOnInsert": {"created_at": now},
            },
            upsert=True,
        )
        if locked_until is not None:
            raise LoginRateLimitError(int(LOGIN_LOCKOUT.total_seconds()))

    async def _get_dummy_hash(self) -> str:
        if self.__class__._dummy_hash is None:
            self.__class__._dummy_hash = await anyio.to_thread.run_sync(
                self._hasher.hash,
                secrets.token_urlsafe(24),
            )
        return self.__class__._dummy_hash

    async def _create_session(self, user: dict[str, Any]) -> AuthResult:
        session_token = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(32)
        now = _utc_now()
        session = {
            "user_id": user["_id"],
            "token_digest": _digest(session_token),
            "csrf_digest": _digest(csrf_token),
            "session_version": int(user.get("session_version", 1)),
            "expires_at": now + SESSION_TTL,
            "last_seen_at": now,
            "revoked_at": None,
            "created_at": now,
            "updated_at": now,
            "is_deleted": False,
            "deleted_at": None,
        }
        result = await self.db[collections.AUTH_SESSIONS].insert_one(session)
        actor = self._actor_from_documents(user, session, result.inserted_id)
        return AuthResult(
            actor=actor,
            session_token=session_token,
            csrf_token=csrf_token,
        )

    async def authenticate(self, session_token: str | None) -> Actor:
        if not session_token:
            raise AuthenticationError("需要登录")
        now = _utc_now()
        session = await self.db[collections.AUTH_SESSIONS].find_one(
            {
                "token_digest": _digest(session_token),
                "revoked_at": None,
                "expires_at": {"$gt": now},
                "is_deleted": False,
            }
        )
        if not session:
            raise AuthenticationError("会话无效或已过期")
        user = await self.db[collections.USERS].find_one(
            {
                "_id": session["user_id"],
                "status": "active",
                "is_deleted": False,
            }
        )
        if (
            not user
            or int(user.get("session_version", 0))
            != int(session.get("session_version", -1))
        ):
            raise AuthenticationError("会话已失效")

        last_seen = session.get("last_seen_at")
        if isinstance(last_seen, datetime) and last_seen.tzinfo is None:
            last_seen = last_seen.replace(tzinfo=timezone.utc)
        if not isinstance(last_seen, datetime) or now - last_seen >= timedelta(minutes=15):
            await self.db[collections.AUTH_SESSIONS].update_one(
                {"_id": session["_id"]},
                {"$set": {"last_seen_at": now, "updated_at": now}},
            )
        return self._actor_from_documents(user, session, session["_id"])

    async def logout(self, actor: Actor) -> None:
        now = _utc_now()
        await self.db[collections.AUTH_SESSIONS].update_one(
            {"_id": to_object_id(actor.session_id), "revoked_at": None},
            {"$set": {"revoked_at": now, "updated_at": now}},
        )

    async def list_users(self, actor: Actor) -> list[dict[str, str]]:
        self._require_admin(actor)
        users = await self.db[collections.USERS].find(
            {"is_deleted": False}
        ).sort("created_at", ASCENDING).to_list(length=None)
        return [self._public_user(user) for user in users]

    async def create_user(
        self,
        actor: Actor,
        *,
        username: str,
        display_name: str,
        password: str,
    ) -> dict[str, str]:
        self._require_admin(actor)
        username, normalized_username = _normalize_username(username)
        display_name = unicodedata.normalize("NFKC", display_name).strip()
        if not 1 <= len(display_name) <= 64:
            raise IdentityValidationError("显示名称长度必须为 1 到 64 个字符")
        _validate_password(password)
        password_hash = await anyio.to_thread.run_sync(self._hasher.hash, password)
        now = _utc_now()
        user = {
            "username": username,
            "normalized_username": normalized_username,
            "display_name": display_name,
            "password_hash": password_hash,
            "role": "user",
            "status": "active",
            "session_version": 1,
            "bootstrap_slot": None,
            "created_at": now,
            "updated_at": now,
            "is_deleted": False,
            "deleted_at": None,
        }
        try:
            result = await self.db[collections.USERS].insert_one(user)
        except DuplicateKeyError as exc:
            raise IdentityConflictError("用户名已经存在") from exc
        user["_id"] = result.inserted_id
        return self._public_user(user)

    async def set_user_status(
        self,
        actor: Actor,
        user_id: str,
        *,
        next_status: str,
    ) -> dict[str, str]:
        self._require_admin(actor)
        if next_status not in {"active", "disabled"}:
            raise IdentityValidationError("用户状态无效")
        try:
            object_id = to_object_id(user_id)
        except Exception as exc:
            raise IdentityValidationError("用户 ID 无效") from exc
        user = await self.db[collections.USERS].find_one(
            {"_id": object_id, "is_deleted": False}
        )
        if not user:
            raise IdentityValidationError("用户不存在")
        if actor.id == user_id and next_status == "disabled":
            raise IdentityValidationError("不能禁用当前登录的管理员")
        if user.get("bootstrap_slot") == "initial" and next_status == "disabled":
            raise IdentityValidationError("不能禁用初始管理员")

        if user.get("status") != next_status:
            now = _utc_now()
            await self.db[collections.USERS].update_one(
                {"_id": object_id},
                {
                    "$set": {"status": next_status, "updated_at": now},
                    "$inc": {"session_version": 1},
                },
            )
            await self.db[collections.AUTH_SESSIONS].update_many(
                {"user_id": object_id, "revoked_at": None},
                {"$set": {"revoked_at": now, "updated_at": now}},
            )
            user["status"] = next_status
        return self._public_user(user)

    async def reset_password(
        self,
        actor: Actor,
        user_id: str,
        *,
        password: str,
    ) -> dict[str, str]:
        self._require_admin(actor)
        _validate_password(password)
        try:
            object_id = to_object_id(user_id)
        except Exception as exc:
            raise IdentityValidationError("用户 ID 无效") from exc
        user = await self.db[collections.USERS].find_one(
            {"_id": object_id, "is_deleted": False}
        )
        if not user:
            raise IdentityValidationError("用户不存在")
        password_hash = await anyio.to_thread.run_sync(self._hasher.hash, password)
        now = _utc_now()
        await self.db[collections.USERS].update_one(
            {"_id": object_id},
            {
                "$set": {"password_hash": password_hash, "updated_at": now},
                "$inc": {"session_version": 1},
            },
        )
        await self.db[collections.AUTH_SESSIONS].update_many(
            {"user_id": object_id, "revoked_at": None},
            {"$set": {"revoked_at": now, "updated_at": now}},
        )
        return self._public_user(user)

    def verify_csrf(
        self,
        actor: Actor,
        *,
        cookie_token: str | None,
        header_token: str | None,
    ) -> bool:
        if not cookie_token or not header_token:
            return False
        if not hmac.compare_digest(cookie_token, header_token):
            return False
        return hmac.compare_digest(_digest(header_token), actor.csrf_digest)

    @staticmethod
    def _actor_from_documents(
        user: dict[str, Any],
        session: dict[str, Any],
        session_id: ObjectId,
    ) -> Actor:
        return Actor(
            id=str(user["_id"]),
            username=str(user["username"]),
            display_name=str(user["display_name"]),
            role=str(user["role"]),
            status=str(user["status"]),
            session_id=str(session_id),
            csrf_digest=str(session["csrf_digest"]),
        )

    @staticmethod
    def _public_user(user: dict[str, Any]) -> dict[str, str]:
        return {
            "id": str(user["_id"]),
            "username": str(user["username"]),
            "display_name": str(user["display_name"]),
            "role": str(user["role"]),
            "status": str(user["status"]),
        }

    @staticmethod
    def _require_admin(actor: Actor) -> None:
        if not actor.is_admin:
            raise AuthorizationError("需要管理员权限")


_identity_service = IdentityService()


def get_identity_service() -> IdentityService:
    return _identity_service
