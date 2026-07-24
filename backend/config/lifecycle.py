"""配置生命周期公共接口与可替换存储接缝。"""

from __future__ import annotations

import base64
import asyncio
from copy import deepcopy
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import shutil
from threading import RLock
import time
from typing import Any, Awaitable, Callable, Literal, Protocol, Union

from pydantic import BaseModel, Field, model_validator
import yaml

from backend.config.config import (
    CURRENT_CONFIG_VERSION,
    _merge_dicts,
    _migrate_config_tree,
    _normalize_config_tree,
)
from backend.config.workflow_catalog import WORKFLOW_STEPS


class ConfigIssue(BaseModel):
    path: str
    code: str
    message: str
    severity: str = "warning"


class ConfigView(BaseModel):
    editable_data: dict[str, Any]
    resolutions: dict[str, Any] = Field(default_factory=dict)
    revision: str
    issues: list[ConfigIssue] = Field(default_factory=list)


class ProviderReferenceChange(BaseModel):
    path: str
    before: Any = None
    after: Any = None


class ConfigChangePreview(BaseModel):
    reference_changes: list[ProviderReferenceChange] = Field(default_factory=list)
    issues: list[ConfigIssue] = Field(default_factory=list)
    confirmation_token: str | None = None


class SecretPatch(BaseModel):
    mode: Literal["keep", "replace", "clear"]
    value: str | None = None

    @model_validator(mode="after")
    def validate_value(self) -> "SecretPatch":
        if self.mode == "replace" and not (self.value or "").strip():
            raise ValueError("SecretPatch replace requires a non-empty value")
        if self.mode != "replace" and self.value is not None:
            raise ValueError("SecretPatch keep/clear must not include a value")
        return self


class RenameProviderCommand(BaseModel):
    kind: Literal["rename"] = "rename"
    from_alias: str
    to_alias: str


class DeleteProviderCommand(BaseModel):
    kind: Literal["delete"] = "delete"
    alias: str
    replacement_default_alias: str | None = None


ProviderCommand = Union[RenameProviderCommand, DeleteProviderCommand]


class ConfigPatch(BaseModel):
    changes: dict[str, Any] = Field(default_factory=dict)
    provider_commands: list[ProviderCommand] = Field(default_factory=list)
    provider_secrets: dict[str, SecretPatch] = Field(default_factory=dict)
    expected_revision: str
    confirmation_token: str | None = None


class ConfigConflictError(ValueError):
    """客户端基于过期 revision 保存配置。"""


class ConfigStore(Protocol):
    def read(self) -> dict[str, Any]: ...

    def write_atomic(self, data: dict[str, Any]) -> None: ...


class MemoryConfigStore:
    """测试使用的进程内配置存储。"""

    def __init__(self, initial: dict[str, Any]) -> None:
        self._data = deepcopy(initial)

    def read(self) -> dict[str, Any]:
        return deepcopy(self._data)

    def write_atomic(self, data: dict[str, Any]) -> None:
        self._data = deepcopy(data)


class YamlConfigStore:
    """带备份和同目录原子替换的 YAML 配置存储。"""

    def __init__(self, path: Path, *, default_path: Path | None = None) -> None:
        self.path = Path(path)
        self.default_path = Path(default_path) if default_path is not None else None

    @staticmethod
    def _read_path(path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        with path.open("r", encoding="utf-8") as file:
            data = yaml.safe_load(file) or {}
        if not isinstance(data, dict):
            raise ValueError(f"Config file must contain a mapping: {path}")
        return data

    def read(self) -> dict[str, Any]:
        user_config = _migrate_config_tree(self._read_path(self.path))
        if self.default_path is None:
            return user_config
        defaults = _migrate_config_tree(self._read_path(self.default_path))
        return _normalize_config_tree(_merge_dicts(defaults, user_config))

    def write_atomic(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{secrets.token_hex(6)}.tmp")
        backup = self.path.with_suffix(f"{self.path.suffix}.bak")
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as file:
                yaml.safe_dump(data, file, allow_unicode=True, sort_keys=False)
                file.flush()
                os.fsync(file.fileno())
            if self.path.exists():
                shutil.copy2(self.path, backup)
            os.replace(temporary, self.path)
        finally:
            if temporary.exists():
                temporary.unlink()


class SecretVersionStore(Protocol):
    def sync(self, raw_config: dict[str, Any]) -> dict[str, int]: ...

    def snapshot_state(self) -> Any: ...

    def restore_state(self, snapshot: Any) -> None: ...

    def revision_state(self) -> dict[str, Any]: ...


class MemorySecretVersionStore:
    """用 keyed digest 检测密钥变化，不保存或暴露密钥本身。"""

    def __init__(self, *, seed: bytes) -> None:
        self._seed = seed
        self._records: dict[str, tuple[str, int]] = {}
        self._store_id = hashlib.sha256(seed).hexdigest()

    def sync(self, raw_config: dict[str, Any]) -> dict[str, int]:
        providers = raw_config.get("llm", {}).get("providers", {})
        if not isinstance(providers, dict):
            providers = {}

        current_aliases: set[str] = set()
        for alias, provider in providers.items():
            if not isinstance(provider, dict):
                continue
            current_aliases.add(alias)
            secret = str(provider.get("api_key") or "")
            digest = hmac.new(self._seed, secret.encode("utf-8"), hashlib.sha256).hexdigest()
            old = self._records.get(alias)
            generation = 1 if old is None else old[1] + int(old[0] != digest)
            self._records[alias] = (digest, generation)

        for alias in set(self._records) - current_aliases:
            del self._records[alias]
        return {alias: record[1] for alias, record in self._records.items()}

    def rename(self, from_alias: str, to_alias: str) -> None:
        if from_alias in self._records:
            self._records[to_alias] = self._records.pop(from_alias)

    def snapshot_state(self) -> Any:
        return deepcopy(self._records)

    def restore_state(self, snapshot: Any) -> None:
        self._records = deepcopy(snapshot)

    def revision_state(self) -> dict[str, Any]:
        return {
            "store_id": self._store_id,
            "generations": {alias: value[1] for alias, value in self._records.items()},
        }


class FileSecretVersionStore:
    """持久化 keyed digest 和密钥世代；文件内不保存 API Key。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = RLock()
        self._state = self._load_or_initialize()

    def _new_state(self) -> dict[str, Any]:
        return {
            "version": 1,
            "store_id": secrets.token_hex(16),
            "seed": base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii"),
            "records": {},
        }

    def _load_or_initialize(self) -> dict[str, Any]:
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(state, dict) or not isinstance(state.get("records"), dict):
                raise ValueError("invalid secret version state")
            base64.urlsafe_b64decode(str(state["seed"]).encode("ascii"))
            return state
        except (FileNotFoundError, KeyError, ValueError, TypeError, json.JSONDecodeError):
            state = self._new_state()
            self._write_atomic(state)
            return state

    def _write_atomic(self, state: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{secrets.token_hex(6)}.tmp")
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as file:
                json.dump(state, file, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary.exists():
                temporary.unlink()

    def sync(self, raw_config: dict[str, Any]) -> dict[str, int]:
        with self._lock:
            seed = base64.urlsafe_b64decode(self._state["seed"].encode("ascii"))
            records = self._state["records"]
            providers = raw_config.get("llm", {}).get("providers", {})
            if not isinstance(providers, dict):
                providers = {}
            changed = False
            current_aliases: set[str] = set()
            for alias, provider in providers.items():
                if not isinstance(provider, dict):
                    continue
                current_aliases.add(alias)
                secret = str(provider.get("api_key") or "")
                digest = hmac.new(seed, secret.encode("utf-8"), hashlib.sha256).hexdigest()
                old = records.get(alias)
                generation = 1 if not isinstance(old, dict) else int(old["generation"]) + int(old["digest"] != digest)
                next_record = {"digest": digest, "generation": generation}
                if old != next_record:
                    records[alias] = next_record
                    changed = True
            for alias in set(records) - current_aliases:
                del records[alias]
                changed = True
            if changed:
                self._write_atomic(self._state)
            return {alias: int(record["generation"]) for alias, record in records.items()}

    def rename(self, from_alias: str, to_alias: str) -> None:
        with self._lock:
            records = self._state["records"]
            if from_alias in records:
                records[to_alias] = records.pop(from_alias)
                self._write_atomic(self._state)

    def snapshot_state(self) -> Any:
        with self._lock:
            return deepcopy(self._state)

    def restore_state(self, snapshot: Any) -> None:
        with self._lock:
            self._state = deepcopy(snapshot)
            self._write_atomic(self._state)

    def revision_state(self) -> dict[str, Any]:
        with self._lock:
            return {
                "store_id": self._state["store_id"],
                "generations": {
                    alias: int(record["generation"])
                    for alias, record in self._state["records"].items()
                },
            }

    def derive_key(self, purpose: str) -> bytes:
        """从私有持久化 seed 派生用途隔离的签名密钥。"""
        with self._lock:
            seed = base64.urlsafe_b64decode(self._state["seed"].encode("ascii"))
            return hmac.new(seed, purpose.encode("utf-8"), hashlib.sha256).digest()


def _provider_issues(raw_config: dict[str, Any]) -> list[ConfigIssue]:
    llm_config = raw_config.get("llm")
    if not isinstance(llm_config, dict):
        return []
    providers = llm_config.get("providers")
    provider_aliases = set(providers) if isinstance(providers, dict) else set()
    issues: list[ConfigIssue] = []

    def check(path: str, value: Any) -> None:
        alias = str(value or "").strip()
        if alias and alias not in provider_aliases:
            issues.append(
                ConfigIssue(
                    path=path,
                    code="provider_not_found",
                    message=f"Provider does not exist: {alias}",
                )
            )
        elif alias:
            provider = providers.get(alias)
            if (
                not isinstance(provider, dict)
                or provider.get("enabled", False) is not True
            ):
                issues.append(
                    ConfigIssue(
                        path=path,
                        code="provider_disabled",
                        message=f"Provider is disabled: {alias}",
                    )
                )

    check("llm.default_provider", llm_config.get("default_provider"))
    review = llm_config.get("format_review")
    if isinstance(review, dict) and review.get("mode") == "provider":
        check("llm.format_review.provider_alias", review.get("provider_alias"))

    workflows = llm_config.get("workflows")
    if isinstance(workflows, dict):
        for workflow_name, workflow in workflows.items():
            if not isinstance(workflow, dict):
                continue
            check(
                f"llm.workflows.{workflow_name}.default_provider",
                workflow.get("default_provider"),
            )
            steps = workflow.get("steps")
            if not isinstance(steps, dict):
                continue
            for step_name, step in steps.items():
                if isinstance(step, dict):
                    check(
                        f"llm.workflows.{workflow_name}.steps.{step_name}.provider",
                        step.get("provider"),
                    )
    return issues


def _redact_config(raw_config: dict[str, Any]) -> dict[str, Any]:
    redacted = deepcopy(raw_config)
    providers = redacted.get("llm", {}).get("providers", {})
    if not isinstance(providers, dict):
        return redacted
    for provider in providers.values():
        if not isinstance(provider, dict):
            continue
        secret = str(provider.pop("api_key", "") or "")
        provider["has_api_key"] = bool(secret)
    return redacted


def _merge_patch(base: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in changes.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_patch(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


def _contains_forbidden_secret_field(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            key in {"api_key", "has_api_key"} or _contains_forbidden_secret_field(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_contains_forbidden_secret_field(item) for item in value)
    return False


def _rename_provider_references(llm_config: dict[str, Any], from_alias: str, to_alias: str) -> None:
    if llm_config.get("default_provider") == from_alias:
        llm_config["default_provider"] = to_alias
    review = llm_config.get("format_review")
    if isinstance(review, dict) and review.get("provider_alias") == from_alias:
        review["provider_alias"] = to_alias

    workflows = llm_config.get("workflows")
    if not isinstance(workflows, dict):
        return
    for workflow in workflows.values():
        if not isinstance(workflow, dict):
            continue
        if workflow.get("default_provider") == from_alias:
            workflow["default_provider"] = to_alias
        steps = workflow.get("steps")
        if not isinstance(steps, dict):
            continue
        for step in steps.values():
            if isinstance(step, dict) and step.get("provider") == from_alias:
                    step["provider"] = to_alias


def _clear_provider_references(llm_config: dict[str, Any], alias: str) -> None:
    review = llm_config.get("format_review")
    if isinstance(review, dict) and review.get("provider_alias") == alias:
        llm_config["format_review"] = {"mode": "disabled", "provider_alias": None}

    workflows = llm_config.get("workflows")
    if not isinstance(workflows, dict):
        return
    for workflow in workflows.values():
        if not isinstance(workflow, dict):
            continue
        if workflow.get("default_provider") == alias:
            workflow["default_provider"] = ""
        steps = workflow.get("steps")
        if not isinstance(steps, dict):
            continue
        for step in steps.values():
            if isinstance(step, dict) and step.get("provider") == alias:
                step["provider"] = ""


def _provider_reference_map(raw_config: dict[str, Any]) -> dict[str, Any]:
    llm_config = raw_config.get("llm")
    if not isinstance(llm_config, dict):
        return {}
    references: dict[str, Any] = {
        "llm.default_provider": llm_config.get("default_provider"),
    }
    review = llm_config.get("format_review")
    if isinstance(review, dict):
        references["llm.format_review"] = deepcopy(review)

    workflows = llm_config.get("workflows")
    if not isinstance(workflows, dict):
        return references
    for workflow_name, workflow in workflows.items():
        if not isinstance(workflow, dict):
            continue
        references[f"llm.workflows.{workflow_name}.default_provider"] = workflow.get(
            "default_provider"
        )
        steps = workflow.get("steps")
        if not isinstance(steps, dict):
            continue
        for step_name, step in steps.items():
            if isinstance(step, dict):
                references[
                    f"llm.workflows.{workflow_name}.steps.{step_name}.provider"
                ] = step.get("provider")
    return references


def _provider_resolutions(raw_config: dict[str, Any]) -> dict[str, Any]:
    """返回 raw 覆盖之外的逐步骤有效 Provider、来源与可用性。"""
    llm = raw_config.get("llm") if isinstance(raw_config.get("llm"), dict) else {}
    providers = llm.get("providers") if isinstance(llm.get("providers"), dict) else {}
    workflows = llm.get("workflows") if isinstance(llm.get("workflows"), dict) else {}
    global_alias = str(llm.get("default_provider") or "").strip()
    resolutions: dict[str, Any] = {}
    for workflow_name, step_names in WORKFLOW_STEPS.items():
        workflow = workflows.get(workflow_name)
        workflow = workflow if isinstance(workflow, dict) else {}
        workflow_alias = str(workflow.get("default_provider") or "").strip()
        steps = workflow.get("steps") if isinstance(workflow.get("steps"), dict) else {}
        for step_name in step_names:
            step = steps.get(step_name)
            step = step if isinstance(step, dict) else {}
            step_alias = str(step.get("provider") or "").strip()
            if step_alias:
                alias, source = step_alias, "step"
            elif workflow_alias:
                alias, source = workflow_alias, "workflow"
            else:
                alias, source = global_alias, "global"
            provider = providers.get(alias) if alias else None
            resolutions[f"llm.workflows.{workflow_name}.steps.{step_name}"] = {
                "provider_alias": alias or None,
                "source": source,
                "exists": isinstance(provider, dict),
                "enabled": bool(isinstance(provider, dict) and provider.get("enabled")),
                "timeout_seconds": step.get("timeout_seconds") or (
                    provider.get("timeout_seconds") if isinstance(provider, dict) else None
                ),
            }
    return resolutions


def _apply_provider_commands(
    raw_config: dict[str, Any],
    commands: list[ProviderCommand],
    secret_store: SecretVersionStore,
) -> dict[str, Any]:
    candidate = deepcopy(raw_config)
    llm_config = candidate.get("llm")
    if not isinstance(llm_config, dict):
        raise ValueError("llm must be a mapping")
    providers = llm_config.get("providers")
    if not isinstance(providers, dict):
        raise ValueError("llm.providers must be a mapping")

    for command in commands:
        if isinstance(command, RenameProviderCommand):
            source = command.from_alias.strip()
            target = command.to_alias.strip()
            if not source or not target:
                raise ValueError("Provider rename aliases must not be empty")
            if source not in providers:
                raise ValueError(f"Provider rename source does not exist: {source}")
            if target in providers and target != source:
                raise ValueError(f"Provider rename target already exists: {target}")
            if source == target:
                continue
            renamed: dict[str, Any] = {}
            for alias, provider in providers.items():
                renamed[target if alias == source else alias] = provider
            providers = renamed
            llm_config["providers"] = providers
            _rename_provider_references(llm_config, source, target)
            continue

        if isinstance(command, DeleteProviderCommand):
            alias = command.alias.strip()
            replacement = (command.replacement_default_alias or "").strip()
            if not alias:
                raise ValueError("Provider delete alias must not be empty")
            if alias not in providers:
                raise ValueError(f"Provider delete target does not exist: {alias}")
            if llm_config.get("default_provider") == alias:
                if not replacement or replacement == alias or replacement not in providers:
                    raise ValueError(
                        "Deleting the default provider requires a valid replacement default alias"
                    )
                replacement_provider = providers[replacement]
                if (
                    not isinstance(replacement_provider, dict)
                    or replacement_provider.get("enabled", False) is not True
                ):
                    raise ValueError(
                        "Deleting the default provider requires an enabled "
                        "replacement default alias"
                    )
                llm_config["default_provider"] = replacement
            del providers[alias]
            _clear_provider_references(llm_config, alias)
            continue

        raise ValueError(f"Unsupported Provider command: {command.kind}")
    return candidate


def _delete_commands_replace_default(
    raw_config: dict[str, Any],
    commands: list[ProviderCommand],
) -> bool:
    llm_config = raw_config.get("llm")
    if not isinstance(llm_config, dict):
        return False
    current_default = str(llm_config.get("default_provider") or "").strip()
    replaced = False
    for command in commands:
        if isinstance(command, RenameProviderCommand):
            if current_default == command.from_alias.strip():
                current_default = command.to_alias.strip()
        elif isinstance(command, DeleteProviderCommand):
            if current_default == command.alias.strip():
                current_default = (
                    command.replacement_default_alias or ""
                ).strip()
                replaced = True
    return replaced


class ConfigLifecycle:
    """统一提供脱敏查看、并发 revision 与后续补丁保存能力。"""

    def __init__(
        self,
        *,
        store: ConfigStore,
        secret_store: SecretVersionStore,
        confirmation_key: bytes | None = None,
    ) -> None:
        self._store = store
        self._secret_store = secret_store
        self._confirmation_key = confirmation_key or hashlib.sha256(
            b"novel-generator-config-confirmation"
        ).digest()
        self._lock = RLock()
        self._apply_lock: asyncio.Lock | None = None

    @staticmethod
    def _request_digest(request: ConfigPatch) -> str:
        request_data = request.model_dump(exclude={"confirmation_token"})
        for secret_patch in request_data.get("provider_secrets", {}).values():
            value = secret_patch.get("value")
            if value is not None:
                secret_patch["value"] = hashlib.sha256(value.encode("utf-8")).hexdigest()
        canonical = json.dumps(
            request_data,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    def _make_confirmation_token(self, request: ConfigPatch) -> str:
        payload = {
            "revision": request.expected_revision,
            "request_digest": self._request_digest(request),
            "expires_at": int(time.time()) + 300,
        }
        encoded = base64.urlsafe_b64encode(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).rstrip(b"=")
        signature = hmac.new(self._confirmation_key, encoded, hashlib.sha256).digest()
        return b".".join(
            (encoded, base64.urlsafe_b64encode(signature).rstrip(b"="))
        ).decode("ascii")

    def _validate_confirmation_token(self, request: ConfigPatch) -> None:
        token = request.confirmation_token or ""
        try:
            encoded_text, signature_text = token.split(".", 1)
            encoded = encoded_text.encode("ascii")
            padding = b"=" * (-len(signature_text) % 4)
            supplied_signature = base64.urlsafe_b64decode(
                signature_text.encode("ascii") + padding
            )
            expected_signature = hmac.new(
                self._confirmation_key, encoded, hashlib.sha256
            ).digest()
            if not hmac.compare_digest(supplied_signature, expected_signature):
                raise ValueError
            payload_padding = b"=" * (-len(encoded) % 4)
            payload = json.loads(base64.urlsafe_b64decode(encoded + payload_padding))
        except (ValueError, UnicodeError, json.JSONDecodeError) as error:
            raise ValueError("A valid confirmation token is required") from error

        if payload.get("expires_at", 0) < int(time.time()):
            raise ValueError("The confirmation token has expired")
        if payload.get("revision") != request.expected_revision:
            raise ValueError("The confirmation token does not match the configuration revision")
        if payload.get("request_digest") != self._request_digest(request):
            raise ValueError("The confirmation token does not match the requested changes")

    def preview(self, request: ConfigPatch) -> ConfigChangePreview:
        with self._lock:
            before = self._store.read()
            current_view = self.get_view()
            if request.expected_revision != current_view.revision:
                raise ConfigConflictError("Configuration revision is stale; reload before saving")
            if _contains_forbidden_secret_field(request.changes):
                raise ValueError("Config changes must not contain api_key or has_api_key")
            command_candidate = _apply_provider_commands(
                before,
                request.provider_commands,
                self._secret_store,
            )
            command_default = command_candidate.get("llm", {}).get(
                "default_provider"
            )
            candidate = _merge_patch(command_candidate, request.changes)
            if (
                _delete_commands_replace_default(
                    before,
                    request.provider_commands,
                )
                and candidate.get("llm", {}).get("default_provider")
                != command_default
            ):
                raise ValueError(
                    "Provider delete changes must preserve the confirmed "
                    "replacement default alias"
                )
            before_references = _provider_reference_map(before)
            after_references = _provider_reference_map(candidate)
            reference_changes = [
                ProviderReferenceChange(
                    path=path,
                    before=before_references.get(path),
                    after=after_references.get(path),
                )
                for path in sorted(set(before_references) | set(after_references))
                if before_references.get(path) != after_references.get(path)
            ]
            destructive = any(
                isinstance(command, DeleteProviderCommand)
                for command in request.provider_commands
            )
            return ConfigChangePreview(
                reference_changes=reference_changes,
                issues=_provider_issues(candidate),
                confirmation_token=self._make_confirmation_token(request) if destructive else None,
            )

    def get_view(self) -> ConfigView:
        with self._lock:
            raw_config = self._store.read()
            generations = self._secret_store.sync(raw_config)
            editable = _redact_config(raw_config)
            revision_payload = {
                "editable": editable,
                "secret_generations": generations,
                "secret_store": self._secret_store.revision_state(),
            }
            revision = hashlib.sha256(
                json.dumps(
                    revision_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            return ConfigView(
                editable_data=editable,
                resolutions=_provider_resolutions(raw_config),
                revision=revision,
                issues=_provider_issues(raw_config),
            )

    def get_raw_config(self) -> dict[str, Any]:
        """仅供受信任的后端服务读取含密钥快照；不得直接返回给 API。"""
        with self._lock:
            return self._store.read()

    async def patch_and_apply(
        self,
        request: ConfigPatch,
        *,
        runtime_apply: Callable[[], Awaitable[None]] | None = None,
        runtime_restore: Callable[[], Awaitable[None]] | None = None,
    ) -> ConfigView:
        """串行化磁盘、密钥世代、运行时应用与失败恢复的完整生命周期。"""
        if self._apply_lock is None:
            self._apply_lock = asyncio.Lock()
        async with self._apply_lock:
            return await self._patch_and_apply_unlocked(
                request,
                runtime_apply=runtime_apply,
                runtime_restore=runtime_restore,
            )

    async def _patch_and_apply_unlocked(
        self,
        request: ConfigPatch,
        *,
        runtime_apply: Callable[[], Awaitable[None]] | None = None,
        runtime_restore: Callable[[], Awaitable[None]] | None = None,
    ) -> ConfigView:
        """按 revision 合并非密钥字段和显式密钥操作，并在失败时恢复。"""
        with self._lock:
            before = self._store.read()
            current_view = self.get_view()
            secret_state_before = self._secret_store.snapshot_state()
            if request.expected_revision != current_view.revision:
                raise ConfigConflictError("Configuration revision is stale; reload before saving")
            if _contains_forbidden_secret_field(request.changes):
                raise ValueError("Config changes must not contain api_key or has_api_key")
            if any(
                isinstance(command, DeleteProviderCommand)
                for command in request.provider_commands
            ):
                self._validate_confirmation_token(request)

            command_candidate = _apply_provider_commands(
                before,
                request.provider_commands,
                self._secret_store,
            )
            command_default = command_candidate.get("llm", {}).get(
                "default_provider"
            )
            candidate = _merge_patch(command_candidate, request.changes)
            if (
                _delete_commands_replace_default(
                    before,
                    request.provider_commands,
                )
                and candidate.get("llm", {}).get("default_provider")
                != command_default
            ):
                raise ValueError(
                    "Provider delete changes must preserve the confirmed "
                    "replacement default alias"
                )
            providers = candidate.get("llm", {}).get("providers", {})
            if not isinstance(providers, dict):
                raise ValueError("llm.providers must be a mapping")

            for alias, secret_patch in request.provider_secrets.items():
                provider = providers.get(alias)
                if not isinstance(provider, dict):
                    raise ValueError(f"Provider does not exist for secret patch: {alias}")
                if secret_patch.mode == "replace":
                    provider["api_key"] = secret_patch.value
                elif secret_patch.mode == "clear":
                    provider["api_key"] = ""

            if candidate.get("config_version") != CURRENT_CONFIG_VERSION:
                raise ValueError(
                    f"Only config_version {CURRENT_CONFIG_VERSION} can be written by this release"
                )
            existing_issues = {
                (issue.path, issue.code, issue.message)
                for issue in _provider_issues(command_candidate)
            }
            for issue in _provider_issues(candidate):
                if (issue.path, issue.code, issue.message) not in existing_issues:
                    raise ValueError(f"Invalid Provider reference at {issue.path}")

            try:
                self._store.write_atomic(candidate)
                for command in request.provider_commands:
                    if isinstance(command, RenameProviderCommand):
                        rename_secret = getattr(self._secret_store, "rename", None)
                        if callable(rename_secret):
                            rename_secret(command.from_alias.strip(), command.to_alias.strip())
                self._secret_store.sync(candidate)
            except Exception:
                self._store.write_atomic(before)
                self._secret_store.restore_state(secret_state_before)
                raise

        try:
            if runtime_apply is not None:
                await runtime_apply()
        except Exception:
            with self._lock:
                self._store.write_atomic(before)
                self._secret_store.restore_state(secret_state_before)
            if runtime_restore is not None:
                await runtime_restore()
            raise

        return self.get_view()
