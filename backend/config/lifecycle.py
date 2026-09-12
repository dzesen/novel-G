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
    _should_replace_dict_path,
)
from backend.config.image_providers import (
    ImageProvidersConfig,
    compute_image_pipeline_quality_fingerprint,
    compute_image_pipeline_revision,
    image_pipeline_reference_paths,
    image_provider_reference_paths,
    rename_image_provider_references,
)
from backend.config.image_quality_acceptance import (
    ImageQualityAcceptanceStore,
    MemoryImageQualityAcceptanceStore,
)
from backend.config.workflow_catalog import WORKFLOW_STEPS


class ConfigIssue(BaseModel):
    path: str
    code: str
    message: str
    severity: str = "warning"


class ImagePipelineStatusView(BaseModel):
    alias: str
    kind: Literal["quick", "consistency"]
    effective_revision: str = ""
    quality_status: Literal["accepted", "experimental", "drifted"]
    quality_reason: Literal[
        "real_provider_12_case_acceptance_missing",
        "real_provider_12_case_acceptance_rejected",
        "quality_acceptance_fingerprint_drift",
        "quality_acceptance_record_matches",
        "pipeline_revision_unavailable",
    ]
    issue: str = ""


class ConfigView(BaseModel):
    editable_data: dict[str, Any]
    resolutions: dict[str, Any] = Field(default_factory=dict)
    image_pipeline_statuses: list[ImagePipelineStatusView] = Field(
        default_factory=list
    )
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


ProviderCommandTarget = Literal["llm", "image"]


class RenameProviderCommand(BaseModel):
    kind: Literal["rename"] = "rename"
    target: ProviderCommandTarget = "llm"
    from_alias: str
    to_alias: str


class DeleteProviderCommand(BaseModel):
    kind: Literal["delete"] = "delete"
    target: ProviderCommandTarget = "llm"
    alias: str
    replacement_default_alias: str | None = None


ProviderCommand = Union[RenameProviderCommand, DeleteProviderCommand]


class ConfigPatch(BaseModel):
    changes: dict[str, Any] = Field(default_factory=dict)
    provider_commands: list[ProviderCommand] = Field(default_factory=list)
    provider_secrets: dict[str, SecretPatch] = Field(default_factory=dict)
    image_provider_secrets: dict[str, SecretPatch] = Field(default_factory=dict)
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

    def rename(
        self,
        target: ProviderCommandTarget,
        from_alias: str,
        to_alias: str,
    ) -> None: ...
    def snapshot_state(self) -> Any: ...

    def restore_state(self, snapshot: Any) -> None: ...

    def revision_state(self) -> dict[str, Any]: ...


_SECRET_STORE_VERSION = 2
_LLM_SECRET_PREFIX = "llm:"
_IMAGE_SECRET_PREFIX = "image:"


def _secret_record_key(
    target: ProviderCommandTarget,
    alias: str,
) -> str:
    prefix = _LLM_SECRET_PREFIX if target == "llm" else _IMAGE_SECRET_PREFIX
    return f"{prefix}{alias}"


def _secret_values(raw_config: dict[str, Any]) -> dict[str, str]:
    """返回需要跟踪的密钥；key 带命名空间，避免两类 Provider 别名碰撞。"""
    values: dict[str, str] = {}

    llm_providers = raw_config.get("llm", {}).get("providers", {})
    if isinstance(llm_providers, dict):
        for alias, provider in llm_providers.items():
            if isinstance(provider, dict):
                values[f"{_LLM_SECRET_PREFIX}{alias}"] = str(
                    provider.get("api_key") or ""
                )

    image_providers = raw_config.get("image_providers", {}).get("providers", {})
    if isinstance(image_providers, dict):
        for alias, provider in image_providers.items():
            if (
                isinstance(provider, dict)
                and provider.get("type") == "openai_compatible"
            ):
                values[f"{_IMAGE_SECRET_PREFIX}{alias}"] = str(
                    provider.get("api_key") or ""
                )

    return values


class MemorySecretVersionStore:
    """用 keyed digest 检测密钥变化，不保存或暴露密钥本身。"""

    def __init__(self, *, seed: bytes) -> None:
        self._seed = seed
        self._records: dict[str, tuple[str, int]] = {}
        self._store_id = hashlib.sha256(seed).hexdigest()

    def sync(self, raw_config: dict[str, Any]) -> dict[str, int]:
        values = _secret_values(raw_config)
        for record_key, secret in values.items():
            digest = hmac.new(self._seed, secret.encode("utf-8"), hashlib.sha256).hexdigest()
            old = self._records.get(record_key)
            generation = 1 if old is None else old[1] + int(old[0] != digest)
            self._records[record_key] = (digest, generation)

        for record_key in set(self._records) - set(values):
            del self._records[record_key]
        return {key: record[1] for key, record in self._records.items()}

    def rename(
        self,
        target: ProviderCommandTarget,
        from_alias: str,
        to_alias: str,
    ) -> None:
        source = _secret_record_key(target, from_alias)
        destination = _secret_record_key(target, to_alias)
        if source in self._records:
            self._records[destination] = self._records.pop(source)

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
            "version": _SECRET_STORE_VERSION,
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
            version = int(state.get("version", 1))
            if version == 1:
                state["records"] = {
                    f"{_LLM_SECRET_PREFIX}{alias}": record
                    for alias, record in state["records"].items()
                }
                state["version"] = _SECRET_STORE_VERSION
                self._write_atomic(state)
            elif version != _SECRET_STORE_VERSION:
                raise ValueError("unsupported secret version state")
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
            changed = False
            values = _secret_values(raw_config)
            for record_key, secret in values.items():
                digest = hmac.new(seed, secret.encode("utf-8"), hashlib.sha256).hexdigest()
                old = records.get(record_key)
                generation = 1 if not isinstance(old, dict) else int(old["generation"]) + int(old["digest"] != digest)
                next_record = {"digest": digest, "generation": generation}
                if old != next_record:
                    records[record_key] = next_record
                    changed = True
            for record_key in set(records) - set(values):
                del records[record_key]
                changed = True
            if changed:
                self._write_atomic(self._state)
            return {
                record_key: int(record["generation"])
                for record_key, record in records.items()
            }

    def rename(
        self,
        target: ProviderCommandTarget,
        from_alias: str,
        to_alias: str,
    ) -> None:
        with self._lock:
            records = self._state["records"]
            source = _secret_record_key(target, from_alias)
            destination = _secret_record_key(target, to_alias)
            if source in records:
                records[destination] = records.pop(source)
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
    issues: list[ConfigIssue] = []

    def check(
        path: str,
        value: Any,
        providers: dict[str, Any],
        provider_aliases: set[str],
    ) -> None:
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

    if isinstance(llm_config, dict):
        llm_providers = llm_config.get("providers")
        llm_providers = llm_providers if isinstance(llm_providers, dict) else {}
        llm_aliases = set(llm_providers)

        check(
            "llm.default_provider",
            llm_config.get("default_provider"),
            llm_providers,
            llm_aliases,
        )
        review = llm_config.get("format_review")
        if isinstance(review, dict) and review.get("mode") == "provider":
            check(
                "llm.format_review.provider_alias",
                review.get("provider_alias"),
                llm_providers,
                llm_aliases,
            )

        workflows = llm_config.get("workflows")
        if isinstance(workflows, dict):
            for workflow_name, workflow in workflows.items():
                if not isinstance(workflow, dict):
                    continue
                check(
                    f"llm.workflows.{workflow_name}.default_provider",
                    workflow.get("default_provider"),
                    llm_providers,
                    llm_aliases,
                )
                steps = workflow.get("steps")
                if not isinstance(steps, dict):
                    continue
                for step_name, step in steps.items():
                    if isinstance(step, dict):
                        check(
                            f"llm.workflows.{workflow_name}.steps.{step_name}.provider",
                            step.get("provider"),
                            llm_providers,
                            llm_aliases,
                        )

    image_config = raw_config.get("image_providers")
    if isinstance(image_config, dict):
        image_providers = image_config.get("providers")
        image_providers = (
            image_providers if isinstance(image_providers, dict) else {}
        )
        image_aliases = set(image_providers)
        for path, alias in image_provider_reference_paths(image_config).items():
            check(path, alias, image_providers, image_aliases)

        pipelines = image_config.get("pipelines")
        pipeline_aliases = set(pipelines) if isinstance(pipelines, dict) else set()
        for path, alias in image_pipeline_reference_paths(image_config).items():
            if alias not in pipeline_aliases:
                issues.append(
                    ConfigIssue(
                        path=path,
                        code="pipeline_not_found",
                        message=f"Image Pipeline does not exist: {alias}",
                    )
                )
    return issues


def _redact_config(raw_config: dict[str, Any]) -> dict[str, Any]:
    redacted = deepcopy(raw_config)
    if "_desktop_runtime" in redacted:
        redacted.pop("_desktop_runtime", None)
        redacted.pop("mongodb_url", None)
        redacted["desktop_managed"] = True
    providers = redacted.get("llm", {}).get("providers", {})
    if isinstance(providers, dict):
        for provider in providers.values():
            if not isinstance(provider, dict):
                continue
            secret = str(provider.pop("api_key", "") or "")
            provider["has_api_key"] = bool(secret)

    image_providers = redacted.get("image_providers", {}).get("providers", {})
    if isinstance(image_providers, dict):
        for provider in image_providers.values():
            if not isinstance(provider, dict):
                continue
            secret = str(provider.pop("api_key", "") or "")
            if provider.get("type") == "openai_compatible":
                provider["has_api_key"] = bool(secret)
            else:
                provider.pop("has_api_key", None)
    return redacted


def _guard_desktop_runtime_patch(raw_config: dict[str, Any], changes: dict[str, Any]) -> None:
    if {"_desktop_runtime", "desktop_managed"}.intersection(changes):
        raise ValueError("Desktop runtime settings cannot be edited through the configuration API")
    if "_desktop_runtime" in raw_config and {"mongodb_url", "mongo_database_name"}.intersection(changes):
        raise ValueError("The desktop application manages its database connection")


def _merge_patch(
    base: dict[str, Any],
    changes: dict[str, Any],
    path: tuple[str, ...] = (),
) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in changes.items():
        current_path = (*path, key)
        if (
            isinstance(value, dict)
            and _should_replace_dict_path(current_path, phase="patch")
        ):
            merged[key] = deepcopy(value)
            continue
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_patch(
                merged[key],
                value,
                current_path,
            )
        else:
            merged[key] = deepcopy(value)
    return merged


def _validate_image_provider_mapping_patch(
    current: dict[str, Any],
    changes: dict[str, Any],
) -> None:
    image_changes = changes.get("image_providers")
    if not isinstance(image_changes, dict) or "providers" not in image_changes:
        return
    submitted = image_changes.get("providers")
    if not isinstance(submitted, dict):
        return
    current_image = current.get("image_providers")
    current_providers = (
        current_image.get("providers")
        if isinstance(current_image, dict)
        else {}
    )
    if not isinstance(current_providers, dict):
        return
    omitted = sorted(set(current_providers) - set(submitted))
    if omitted:
        raise ValueError(
            "Image Provider aliases cannot be removed through config changes; "
            "use provider_commands with target=image: "
            + ", ".join(omitted)
        )


def _validate_image_pipeline_mapping_patch(
    current: dict[str, Any],
    changes: dict[str, Any],
) -> None:
    """Profile 尚无删除生命周期；普通 PATCH 不得以省略表达删除。"""
    image_changes = changes.get("image_providers")
    if not isinstance(image_changes, dict) or "pipelines" not in image_changes:
        return
    submitted = image_changes.get("pipelines")
    if not isinstance(submitted, dict):
        return
    current_image = current.get("image_providers")
    current_pipelines = (
        current_image.get("pipelines")
        if isinstance(current_image, dict)
        else {}
    )
    if not isinstance(current_pipelines, dict):
        return
    omitted = sorted(set(current_pipelines) - set(submitted))
    if omitted:
        raise ValueError(
            "Image Pipeline aliases cannot be removed through config changes; "
            "Pipeline Profile deletion is not supported: "
            + ", ".join(omitted)
        )


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


def _rename_image_provider_references(
    image_config: dict[str, Any],
    from_alias: str,
    to_alias: str,
) -> None:
    rename_image_provider_references(image_config, from_alias, to_alias)


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


def _clear_image_provider_references(
    image_config: dict[str, Any],
    alias: str,
) -> None:
    usages = image_config.get("usages")
    if not isinstance(usages, dict):
        return
    for usage, configured_alias in usages.items():
        if configured_alias == alias:
            usages[usage] = ""


def _provider_reference_map(raw_config: dict[str, Any]) -> dict[str, Any]:
    llm_config = raw_config.get("llm")
    references: dict[str, Any] = {}
    if isinstance(llm_config, dict):
        references["llm.default_provider"] = llm_config.get("default_provider")
        review = llm_config.get("format_review")
        if isinstance(review, dict):
            references["llm.format_review"] = deepcopy(review)

        workflows = llm_config.get("workflows")
        if isinstance(workflows, dict):
            for workflow_name, workflow in workflows.items():
                if not isinstance(workflow, dict):
                    continue
                references[
                    f"llm.workflows.{workflow_name}.default_provider"
                ] = workflow.get("default_provider")
                steps = workflow.get("steps")
                if not isinstance(steps, dict):
                    continue
                for step_name, step in steps.items():
                    if isinstance(step, dict):
                        references[
                            f"llm.workflows.{workflow_name}.steps.{step_name}.provider"
                        ] = step.get("provider")

    image_config = raw_config.get("image_providers")
    if isinstance(image_config, dict):
        references["image_providers.default_provider"] = image_config.get(
            "default_provider"
        )
        usages = image_config.get("usages")
        if isinstance(usages, dict):
            for usage, alias in usages.items():
                references[f"image_providers.usages.{usage}"] = alias
        for path, alias in image_provider_reference_paths(image_config).items():
            if path.startswith("image_providers.pipelines."):
                references[path] = alias
        references.update(image_pipeline_reference_paths(image_config))
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

    image_config = (
        raw_config.get("image_providers")
        if isinstance(raw_config.get("image_providers"), dict)
        else {}
    )
    image_providers = (
        image_config.get("providers")
        if isinstance(image_config.get("providers"), dict)
        else {}
    )
    image_default = str(image_config.get("default_provider") or "").strip()
    image_usages = (
        image_config.get("usages")
        if isinstance(image_config.get("usages"), dict)
        else {}
    )
    for usage, configured_alias in image_usages.items():
        usage_alias = str(configured_alias or "").strip()
        alias = usage_alias or image_default
        provider = image_providers.get(alias) if alias else None
        resolutions[f"image_providers.usages.{usage}"] = {
            "provider_alias": alias or None,
            "source": "usage" if usage_alias else "global",
            "exists": isinstance(provider, dict),
            "enabled": bool(
                isinstance(provider, dict) and provider.get("enabled")
            ),
            "type": provider.get("type") if isinstance(provider, dict) else None,
        }

    for path, alias in image_provider_reference_paths(image_config).items():
        prefix = "image_providers.pipelines."
        if not path.startswith(prefix):
            continue
        profile_path, field = path.rsplit(".", 1)
        stage = field.removesuffix("_provider")
        provider = image_providers.get(alias)
        resolutions[f"{profile_path}.{stage}"] = {
            "provider_alias": alias,
            "exists": isinstance(provider, dict),
            "enabled": bool(
                isinstance(provider, dict) and provider.get("enabled")
            ),
            "type": provider.get("type") if isinstance(provider, dict) else None,
        }
    return resolutions


def _apply_provider_commands(
    raw_config: dict[str, Any],
    commands: list[ProviderCommand],
    secret_store: SecretVersionStore,
) -> dict[str, Any]:
    candidate = deepcopy(raw_config)

    for command in commands:
        config_key = (
            "llm"
            if command.target == "llm"
            else "image_providers"
        )
        provider_config = candidate.get(config_key)
        if not isinstance(provider_config, dict):
            raise ValueError(f"{config_key} must be a mapping")
        providers = provider_config.get("providers")
        if not isinstance(providers, dict):
            raise ValueError(f"{config_key}.providers must be a mapping")

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
                raise ValueError("Provider rename source and target must differ")
            renamed: dict[str, Any] = {}
            for alias, provider in providers.items():
                renamed[target if alias == source else alias] = provider
            providers = renamed
            provider_config["providers"] = providers
            if command.target == "llm":
                _rename_provider_references(provider_config, source, target)
            else:
                _rename_image_provider_references(
                    provider_config,
                    source,
                    target,
                )
            continue

        if isinstance(command, DeleteProviderCommand):
            alias = command.alias.strip()
            replacement = (command.replacement_default_alias or "").strip()
            if not alias:
                raise ValueError("Provider delete alias must not be empty")
            if alias not in providers:
                raise ValueError(f"Provider delete target does not exist: {alias}")
            if provider_config.get("default_provider") == alias:
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
                provider_config["default_provider"] = replacement
            del providers[alias]
            if command.target == "llm":
                _clear_provider_references(provider_config, alias)
            else:
                _clear_image_provider_references(provider_config, alias)
            continue

        raise ValueError(f"Unsupported Provider command: {command.kind}")
    return candidate


def _delete_command_replacement_defaults(
    raw_config: dict[str, Any],
    commands: list[ProviderCommand],
) -> dict[ProviderCommandTarget, str]:
    current_defaults: dict[ProviderCommandTarget, str] = {}
    for target, config_key in (
        ("llm", "llm"),
        ("image", "image_providers"),
    ):
        provider_config = raw_config.get(config_key)
        current_defaults[target] = (
            str(provider_config.get("default_provider") or "").strip()
            if isinstance(provider_config, dict)
            else ""
        )

    replacements: dict[ProviderCommandTarget, str] = {}
    for command in commands:
        target = command.target
        if isinstance(command, RenameProviderCommand):
            if current_defaults[target] == command.from_alias.strip():
                current_defaults[target] = command.to_alias.strip()
                if target in replacements:
                    replacements[target] = current_defaults[target]
        elif isinstance(command, DeleteProviderCommand):
            if current_defaults[target] == command.alias.strip():
                current_defaults[target] = (
                    command.replacement_default_alias or ""
                ).strip()
                replacements[target] = current_defaults[target]
    return replacements


def _validate_confirmed_default_replacements(
    candidate: dict[str, Any],
    replacements: dict[ProviderCommandTarget, str],
) -> None:
    for target, expected_alias in replacements.items():
        config_key = "llm" if target == "llm" else "image_providers"
        provider_config = candidate.get(config_key)
        actual_alias = (
            provider_config.get("default_provider")
            if isinstance(provider_config, dict)
            else None
        )
        if actual_alias != expected_alias:
            raise ValueError(
                "Provider delete changes must preserve the confirmed "
                "replacement default alias"
            )


def _validate_provider_command_outcomes(
    command_candidate: dict[str, Any],
    candidate: dict[str, Any],
    commands: list[ProviderCommand],
) -> None:
    for target, config_key in (
        ("llm", "llm"),
        ("image", "image_providers"),
    ):
        retired_aliases: set[str] = set()
        for command in commands:
            if command.target != target:
                continue
            if isinstance(command, RenameProviderCommand):
                retired_aliases.add(command.from_alias.strip())
            else:
                retired_aliases.add(command.alias.strip())
        expected_config = command_candidate.get(config_key)
        actual_config = candidate.get(config_key)
        expected_providers = (
            expected_config.get("providers")
            if isinstance(expected_config, dict)
            else {}
        )
        actual_providers = (
            actual_config.get("providers")
            if isinstance(actual_config, dict)
            else {}
        )
        if not isinstance(expected_providers, dict) or not isinstance(
            actual_providers,
            dict,
        ):
            continue
        resurrected = sorted(
            alias
            for alias in retired_aliases
            if alias not in expected_providers and alias in actual_providers
        )
        if resurrected:
            raise ValueError(
                f"Provider command changes reintroduced target={target} aliases: "
                + ", ".join(resurrected)
            )

        if target == "image" and isinstance(actual_config, dict):
            retired_references = sorted(
                path
                for path, alias in image_provider_reference_paths(
                    actual_config
                ).items()
                if alias in retired_aliases
            )
            if retired_references:
                raise ValueError(
                    "Provider command changes left retired image Provider "
                    "references at: " + ", ".join(retired_references)
                )


def _validate_image_providers_config(
    raw_config: dict[str, Any],
) -> ImageProvidersConfig:
    image_config = raw_config.get("image_providers")
    if not isinstance(image_config, dict):
        raise ValueError("image_providers must be a mapping")
    return ImageProvidersConfig.model_validate(image_config)


def _apply_image_provider_secrets(
    raw_config: dict[str, Any],
    secret_patches: dict[str, SecretPatch],
) -> None:
    if not secret_patches:
        return
    image_config = raw_config.get("image_providers")
    if not isinstance(image_config, dict):
        raise ValueError("image_providers must be a mapping")
    providers = image_config.get("providers")
    if not isinstance(providers, dict):
        raise ValueError("image_providers.providers must be a mapping")

    for alias, secret_patch in secret_patches.items():
        provider = providers.get(alias)
        if not isinstance(provider, dict):
            raise ValueError(
                f"Image Provider does not exist for secret patch: {alias}"
            )
        if provider.get("type") != "openai_compatible":
            raise ValueError(
                f"Image Provider does not accept an API key: {alias}"
            )
        if secret_patch.mode == "replace":
            provider["api_key"] = secret_patch.value
        elif secret_patch.mode == "clear":
            provider["api_key"] = ""


class ConfigLifecycle:
    """统一提供脱敏查看、并发 revision 与后续补丁保存能力。"""

    def __init__(
        self,
        *,
        store: ConfigStore,
        secret_store: SecretVersionStore,
        confirmation_key: bytes | None = None,
        quality_acceptance_store: ImageQualityAcceptanceStore | None = None,
    ) -> None:
        self._store = store
        self._secret_store = secret_store
        self._confirmation_key = confirmation_key or hashlib.sha256(
            b"novel-generator-config-confirmation"
        ).digest()
        self._quality_acceptance_store = (
            quality_acceptance_store or MemoryImageQualityAcceptanceStore()
        )
        self._lock = RLock()
        self._apply_lock: asyncio.Lock | None = None

    @staticmethod
    def _request_digest(request: ConfigPatch) -> str:
        request_data = request.model_dump(exclude={"confirmation_token"})
        for secret_group in ("provider_secrets", "image_provider_secrets"):
            for secret_patch in request_data.get(secret_group, {}).values():
                value = secret_patch.get("value")
                if value is not None:
                    secret_patch["value"] = hashlib.sha256(
                        value.encode("utf-8")
                    ).hexdigest()
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
            _guard_desktop_runtime_patch(before, request.changes)
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
            _validate_image_provider_mapping_patch(
                command_candidate,
                request.changes,
            )
            _validate_image_pipeline_mapping_patch(
                command_candidate,
                request.changes,
            )
            replacements = _delete_command_replacement_defaults(
                before,
                request.provider_commands,
            )
            candidate = _merge_patch(command_candidate, request.changes)
            _validate_confirmed_default_replacements(candidate, replacements)
            _validate_provider_command_outcomes(
                command_candidate,
                candidate,
                request.provider_commands,
            )
            _validate_image_providers_config(candidate)
            _apply_image_provider_secrets(
                candidate,
                request.image_provider_secrets,
            )
            _validate_image_providers_config(candidate)
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

    def _image_pipeline_statuses(
        self,
        raw_config: dict[str, Any],
    ) -> list[ImagePipelineStatusView]:
        image_config = raw_config.get("image_providers")
        if not isinstance(image_config, dict):
            return []
        try:
            config = ImageProvidersConfig.model_validate(image_config)
        except ValueError:
            return []

        statuses: list[ImagePipelineStatusView] = []
        for alias in sorted(config.pipelines):
            profile = config.pipelines[alias]
            try:
                effective_revision = compute_image_pipeline_revision(config, alias)
            except ValueError as exc:
                statuses.append(
                    ImagePipelineStatusView(
                        alias=alias,
                        kind=profile.kind,
                        quality_status="drifted",
                        quality_reason="pipeline_revision_unavailable",
                        issue=str(exc),
                    )
                )
                continue
            quality_records = self._quality_acceptance_store.records_for_alias(alias)
            if not quality_records:
                statuses.append(
                    ImagePipelineStatusView(
                        alias=alias,
                        kind=profile.kind,
                        effective_revision=effective_revision,
                        quality_status="experimental",
                        quality_reason="real_provider_12_case_acceptance_missing",
                    )
                )
                continue
            quality_record = None
            quality_issue = ""
            for record in quality_records:
                try:
                    current_fingerprint = compute_image_pipeline_quality_fingerprint(
                        config,
                        alias,
                        stage_evidence=record.stage_evidence,
                    )
                except ValueError as exc:
                    quality_issue = str(exc)
                    continue
                if current_fingerprint == record.quality_fingerprint:
                    quality_record = record
                    break
            if quality_record is None:
                statuses.append(
                    ImagePipelineStatusView(
                        alias=alias,
                        kind=profile.kind,
                        effective_revision=effective_revision,
                        quality_status="drifted",
                        quality_reason="quality_acceptance_fingerprint_drift",
                        issue=(
                            quality_issue
                            or "当前 Pipeline 配置不匹配任何已记录的 12 案质量指纹"
                        ),
                    )
                )
                continue
            statuses.append(
                ImagePipelineStatusView(
                    alias=alias,
                    kind=profile.kind,
                    effective_revision=effective_revision,
                    quality_status=(
                        "accepted"
                        if quality_record.decision == "accepted"
                        else "experimental"
                    ),
                    quality_reason=(
                        "quality_acceptance_record_matches"
                        if quality_record.decision == "accepted"
                        else "real_provider_12_case_acceptance_rejected"
                    ),
                )
            )
        return statuses

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
                image_pipeline_statuses=self._image_pipeline_statuses(raw_config),
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
            _guard_desktop_runtime_patch(before, request.changes)
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
            _validate_image_provider_mapping_patch(
                command_candidate,
                request.changes,
            )
            _validate_image_pipeline_mapping_patch(
                command_candidate,
                request.changes,
            )
            replacements = _delete_command_replacement_defaults(
                before,
                request.provider_commands,
            )
            candidate = _merge_patch(command_candidate, request.changes)
            _validate_confirmed_default_replacements(candidate, replacements)
            _validate_provider_command_outcomes(
                command_candidate,
                candidate,
                request.provider_commands,
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

            _validate_image_providers_config(candidate)
            _apply_image_provider_secrets(
                candidate,
                request.image_provider_secrets,
            )
            _validate_image_providers_config(candidate)

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
                        self._secret_store.rename(
                            command.target,
                            command.from_alias.strip(),
                            command.to_alias.strip(),
                        )
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
