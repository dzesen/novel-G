"""Persistent single-image jobs shared by portrait and cover adapters."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import inspect
import json
from pathlib import Path
import secrets
import time
from typing import Any, Callable, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from backend.config import config as config_module
from backend.config.image_providers import get_image_providers_config
from backend.db.repositories.image_asset_repository import (
    image_asset_repo,
)
from backend.db.repositories.image_job_repository import image_job_repo
from backend.db.utils import get_utc_now, to_object_id
from backend.services.image.comfyui_provider import (
    ComfyUIProvider,
    build_comfyui_runtime_fingerprint,
)
from backend.services.image.comfyui_template import load_comfyui_template
from backend.services.image.contracts import (
    ImageCancelResult,
    ImageFailure,
    ImageGenerationRequest,
    ImageJobHandle,
    ImageProvider,
    ImageProviderError,
    ImagePollResult,
)
from backend.services.image.managed_assets import (
    GeneratedImageAssetCreate,
    ImageAssetIntegrityError,
    ImageAssetNotFoundError,
    ImagePollAssetConsumer,
    ManagedImageAssetService,
)
from backend.services.llm.agent_orchestrator import IllustrationPromptResult
from backend.services.novel.appearance_anchor import (
    APPEARANCE_ANCHOR_RESET_WARNING,
    AppearanceAnchorSchema,
    AppearanceAnchorConflictError,
    RuntimeFingerprintSchema,
)
from backend.services.novel.reference_card_service import ReferenceCardService


ASSET_STORAGE_LEASE_SECONDS = 60
CANCEL_PROVIDER_REQUEST_UPPER_BOUND = 7
CANCEL_LEASE_MARGIN_SECONDS = 30


class PortraitJobNotFoundError(LookupError):
    """The owner-scoped portrait job does not exist."""


class PortraitConfigurationError(ValueError):
    """The selected provider cannot generate a portrait."""


class PortraitAnchorResetRequired(ValueError):
    """Replacing an existing frozen anchor requires explicit confirmation."""


ImageJobUsage = Literal["character_portrait", "cover", "scene_illustration"]


@dataclass(frozen=True, slots=True)
class ImageJobScope:
    owner_id: str
    novel_id: str
    usage: ImageJobUsage
    subject_id: str

    def canonical(self) -> "ImageJobScope":
        return ImageJobScope(
            owner_id=_canonical_object_id(self.owner_id, field="owner_id"),
            novel_id=_canonical_object_id(self.novel_id, field="novel_id"),
            usage=self.usage,
            subject_id=_canonical_object_id(
                self.subject_id,
                field="subject_id",
            ),
        )


@dataclass(frozen=True, slots=True)
class ImageJobPlan:
    prompt: IllustrationPromptResult
    seed: int | None
    slot_values: dict[str, Any]
    required_slots: frozenset[str]
    persisted_fields: dict[str, Any]
    idempotency_context: dict[str, Any]


class ImageCompletionError(RuntimeError):
    def __init__(self, failure: ImageFailure) -> None:
        super().__init__(failure.message)
        self.failure = failure


class ImageJobCompletionAdapter(Protocol):
    async def prepare(
        self,
        *,
        scope: ImageJobScope,
        job: dict[str, Any],
        handle: ImageJobHandle,
        asset: dict[str, Any],
    ) -> dict[str, Any]: ...

    async def finalize(
        self,
        *,
        scope: ImageJobScope,
        job: dict[str, Any],
        pending: dict[str, Any],
    ) -> dict[str, Any]: ...


class PortraitAssetProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    asset_id: str
    content_hash: str
    mime: str
    width: int
    height: int
    state: str = "available"
    content_url: str | None


class ImageJobProjection(BaseModel):
    """Safe job projection; provider handles and filesystem paths stay private."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    job_id: str
    status: str
    terminal: bool
    queue_position: int | None = None
    estimated_seconds: int
    elapsed_seconds: int
    completed_images: int
    submit_count: int
    selected_as_current: bool | None = None
    cleanup_pending: bool = False
    abandonable: bool = False
    ignored_slots: tuple[str, ...] = ()
    failure: ImageFailure | None = None
    warnings: tuple[str, ...] = ()
    asset: PortraitAssetProjection | None = None
    provider: PortraitProviderProjection | None = None


class PortraitJobProjection(ImageJobProjection):
    anchor: AppearanceAnchorSchema | None = None


class PortraitProviderProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    alias: str
    model: str = ""
    workflow_revision: str = ""
    reference_mode: str = "none"
    available: bool
    queue_position: int | None
    estimated_seconds: int | None
    warnings: tuple[str, ...] = ()


class CharacterPortraitStateProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    anchor: AppearanceAnchorSchema | None = None
    asset: PortraitAssetProjection | None = None
    active_job: PortraitJobProjection | None = None
    cleanup_job: PortraitJobProjection | None = None
    provider: PortraitProviderProjection
    warnings: tuple[str, ...] = ()


class ImageJobStateProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    active_job: ImageJobProjection | None = None
    cleanup_job: ImageJobProjection | None = None
    provider: PortraitProviderProjection
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ResolvedImageProvider:
    alias: str
    provider: ImageProvider
    reference_mode: str
    timeout_seconds: int = 600

    async def aclose(self) -> None:
        close = getattr(self.provider, "aclose", None)
        if close is None:
            return
        result = close()
        if inspect.isawaitable(result):
            await result


@dataclass(frozen=True, slots=True)
class ImageProviderSnapshot:
    alias: str
    available: bool
    timeout_seconds: int
    queue_position: int = 0
    model: str = ""
    workflow_revision: str = ""
    reference_mode: str = "none"
    runtime_fingerprint: dict[str, Any] | None = None
    warnings: tuple[str, ...] = ()


class ImageJobRepositoryProtocol(Protocol):
    async def create_job(self, document: dict[str, Any]) -> dict[str, Any]: ...

    async def get_owned_job(
        self,
        *,
        owner_id: str,
        novel_id: str,
        card_id: str,
        job_id: str,
    ) -> dict[str, Any] | None: ...

    async def update_owned_job(
        self,
        *,
        owner_id: str,
        novel_id: str,
        card_id: str,
        job_id: str,
        fields: dict[str, Any],
    ) -> dict[str, Any] | None: ...

    async def compare_and_update_owned_job(
        self,
        *,
        owner_id: str,
        novel_id: str,
        card_id: str,
        job_id: str,
        expected_revision: int,
        fields: dict[str, Any],
    ) -> dict[str, Any] | None: ...

    async def attach_late_handle(
        self,
        *,
        owner_id: str,
        novel_id: str,
        card_id: str,
        job_id: str,
        fields: dict[str, Any],
    ) -> dict[str, Any] | None: ...

    async def attach_terminal_late_handle(
        self,
        *,
        owner_id: str,
        novel_id: str,
        card_id: str,
        job_id: str,
        fields: dict[str, Any],
    ) -> dict[str, Any] | None: ...

    async def compare_and_update_terminal_job(
        self,
        *,
        owner_id: str,
        novel_id: str,
        card_id: str,
        job_id: str,
        expected_revision: int,
        fields: dict[str, Any],
    ) -> dict[str, Any] | None: ...

    async def find_active_owned_job(
        self,
        *,
        owner_id: str,
        novel_id: str,
        card_id: str,
    ) -> dict[str, Any] | None: ...

    async def find_pending_cleanup_owned_job(
        self,
        *,
        owner_id: str,
        novel_id: str,
        card_id: str,
    ) -> dict[str, Any] | None: ...

    async def find_latest_owned_job(
        self,
        *,
        owner_id: str,
        novel_id: str,
        card_id: str,
    ) -> dict[str, Any] | None: ...

    async def median_completed_seconds(
        self,
        *,
        provider_alias: str,
    ) -> int | None: ...


class ScopedImageJobRepository:
    """Adapt the legacy card-shaped repository calls to a generic subject.

    The durable state machine predates cover jobs and its private methods pass
    ``card_id`` throughout. This adapter keeps those methods ignorant of Mongo
    schema while the persistence seam is now owner/novel/usage/subject based.
    """

    _SUBJECT_METHODS = frozenset(
        {
            "get_owned_job",
            "update_owned_job",
            "compare_and_update_owned_job",
            "attach_late_handle",
            "attach_terminal_late_handle",
            "compare_and_update_terminal_job",
            "find_active_owned_job",
            "find_pending_cleanup_owned_job",
            "find_latest_owned_job",
        }
    )

    def __init__(
        self,
        repository: Any,
        *,
        usage: ImageJobUsage,
    ) -> None:
        self.repository = repository
        self.usage = usage

    async def create_job(self, document: dict[str, Any]) -> dict[str, Any]:
        prepared = {
            **dict(document),
            "usage": self.usage,
            "subject_id": document.get("subject_id"),
        }
        if prepared["subject_id"] is None:
            prepared["subject_id"] = document.get("character_card_id")
        return await self.repository.create_job(prepared)

    async def median_completed_seconds(
        self,
        *,
        provider_alias: str,
    ) -> int | None:
        return await self.repository.median_completed_seconds(
            provider_alias=provider_alias,
            usage=self.usage,
        )

    def __getattr__(self, name: str) -> Any:
        target = getattr(self.repository, name)
        if name not in self._SUBJECT_METHODS:
            return target

        async def call(*args: Any, **kwargs: Any) -> Any:
            subject_id = kwargs.pop("card_id", None)
            return await target(
                *args,
                **kwargs,
                usage=self.usage,
                subject_id=subject_id,
            )

        return call


class AppearanceAnchorGatewayProtocol(Protocol):
    async def get_anchor(
        self,
        *,
        novel_id: str,
        card_id: str,
    ) -> dict[str, Any] | None: ...

    async def establish_anchor(
        self,
        *,
        novel_id: str,
        card_id: str,
        anchor: dict[str, Any],
    ) -> dict[str, Any]: ...

    async def reset_anchor(
        self,
        *,
        novel_id: str,
        card_id: str,
        anchor: dict[str, Any],
        expected_previous: dict[str, Any],
        confirmed: bool,
    ) -> dict[str, Any]: ...


class ImageProviderResolverProtocol(Protocol):
    async def resolve(
        self,
        *,
        usage: str,
        provider_alias: str | None,
    ) -> ResolvedImageProvider: ...


class ManagedAssetReaderProtocol(Protocol):
    async def read_owned_asset(
        self,
        *,
        owner_id: str,
        asset_id: str,
    ) -> bytes: ...


class ImageAssetMetadataRepositoryProtocol(Protocol):
    async def get_owned_subject_hash(
        self,
        *,
        owner_id: Any,
        novel_id: Any,
        subject_kind: str,
        subject_id: str,
        content_hash: str,
    ) -> dict[str, Any] | None: ...

    async def get_latest_owned_imported_subject(
        self,
        *,
        owner_id: Any,
        novel_id: Any,
        subject_kind: str,
        subject_id: str,
    ) -> dict[str, Any] | None: ...


class ReferenceCardAppearanceAnchorGateway:
    async def get_anchor(
        self,
        *,
        novel_id: str,
        card_id: str,
    ) -> dict[str, Any] | None:
        return await ReferenceCardService.get_appearance_anchor(
            novel_id,
            card_id,
        )

    async def establish_anchor(
        self,
        *,
        novel_id: str,
        card_id: str,
        anchor: dict[str, Any],
    ) -> dict[str, Any]:
        return await ReferenceCardService.establish_appearance_anchor(
            novel_id,
            card_id,
            anchor,
        )

    async def reset_anchor(
        self,
        *,
        novel_id: str,
        card_id: str,
        anchor: dict[str, Any],
        expected_previous: dict[str, Any],
        confirmed: bool,
    ) -> dict[str, Any]:
        return await ReferenceCardService.reset_appearance_anchor(
            novel_id,
            card_id,
            anchor,
            expected_previous=expected_previous,
            confirmed=confirmed,
        )


class ConfiguredImageProviderResolver:
    async def resolve(
        self,
        *,
        usage: str,
        provider_alias: str | None,
    ) -> ResolvedImageProvider:
        config = get_image_providers_config()
        configured_usage = getattr(config.usages, usage, "")
        alias = (
            str(provider_alias or "").strip()
            or str(configured_usage or "").strip()
            or config.default_provider.strip()
        )
        provider_config = config.providers.get(alias)
        if provider_config is None:
            raise PortraitConfigurationError(
                "未配置可用于当前图像用途的图像后端"
            )
        if not provider_config.enabled:
            raise PortraitConfigurationError(
                f"图像后端 {alias} 已停用，请先在设置中启用"
            )
        if provider_config.type != "comfyui":
            raise PortraitConfigurationError(
                "当前版本只支持 ComfyUI 图像后端"
            )
        provider = ComfyUIProvider(
            alias=alias,
            config=provider_config,
            template_root=(
                config_module.CONFIG_PATH.resolve().parent
            ),
        )
        return ResolvedImageProvider(
            alias=alias,
            provider=provider,
            reference_mode=provider_config.workflow.reference_mode,
            timeout_seconds=provider_config.timeout_seconds,
        )

    async def inspect(
        self,
        *,
        usage: str,
        provider_alias: str | None,
    ) -> ImageProviderSnapshot:
        try:
            resolved = await self.resolve(
                usage=usage,
                provider_alias=provider_alias,
            )
        except PortraitConfigurationError as error:
            return ImageProviderSnapshot(
                alias=str(provider_alias or ""),
                available=False,
                timeout_seconds=600,
                warnings=(str(error),),
            )
        provider = resolved.provider
        assert isinstance(provider, ComfyUIProvider)
        try:
            loaded = load_comfyui_template(
                provider.config.workflow,
                template_root=provider.template_root,
            )
            queue = await provider.client.get_queue()
            system_stats = await provider.client.get_system_stats()
            fingerprint = build_comfyui_runtime_fingerprint(
                system_stats,
                checkpoint_names=loaded.checkpoint_names,
                lora_names=loaded.lora_names,
                workflow_graph_hash=loaded.effective_graph_hash,
            )
            return ImageProviderSnapshot(
                alias=resolved.alias,
                available=True,
                timeout_seconds=resolved.timeout_seconds,
                queue_position=len(queue.running) + len(queue.pending),
                model=_stable_model_names(
                    loaded.checkpoint_names,
                    workflow_revision=loaded.revision,
                ),
                workflow_revision=loaded.revision,
                reference_mode=resolved.reference_mode,
                runtime_fingerprint=fingerprint.model_dump(mode="json"),
                warnings=(
                    "当前 ComfyUI 接口无法核验任意自定义节点包版本；checkpoint/LoRA 也只比较文件名，无法识别同名内容替换，这些变化都可能减弱外观一致性",
                ),
            )
        except ImageProviderError as error:
            return ImageProviderSnapshot(
                alias=resolved.alias,
                available=False,
                timeout_seconds=resolved.timeout_seconds,
                warnings=(error.failure.action,),
            )
        except Exception:
            return ImageProviderSnapshot(
                alias=resolved.alias,
                available=False,
                timeout_seconds=resolved.timeout_seconds,
                warnings=(
                    "无法读取图像后端队列与运行环境，请检查 ComfyUI 和 workflow 配置",
                ),
            )
        finally:
            await resolved.aclose()


def _positive_prompt(prompt: IllustrationPromptResult) -> str:
    parts = (
        ("Subject", prompt.subject),
        ("Appearance", prompt.appearance),
        ("Scene", prompt.scene),
        ("Style", prompt.style),
    )
    return "\n".join(
        f"{label}: {value}"
        for label, value in parts
        if value
    )


def _stable_model_names(
    checkpoint_names: tuple[str, ...] | list[str],
    *,
    workflow_revision: str,
) -> str:
    checkpoints = tuple(sorted(checkpoint_names))
    if len(checkpoints) == 1 and len(checkpoints[0]) <= 500:
        return checkpoints[0]
    if checkpoints:
        digest = hashlib.sha256(
            "\n".join(checkpoints).encode("utf-8")
        ).hexdigest()
        return f"checkpoints:sha256:{digest}"
    return f"workflow:{workflow_revision}"


def _stable_model(handle: ImageJobHandle) -> str:
    return _stable_model_names(
        handle.audit.checkpoint_names,
        workflow_revision=handle.audit.template_revision,
    )


def _job_id(document: dict[str, Any]) -> str:
    return str(document["_id"])


def _canonical_object_id(value: Any, *, field: str) -> str:
    if value is None:
        raise ValueError(f"{field} is required")
    return str(to_object_id(value))


def _projection(document: dict[str, Any]) -> PortraitJobProjection:
    asset = document.get("asset")
    safe_asset = None
    if isinstance(asset, dict):
        asset_id = str(asset.get("asset_id") or "")
        if asset_id:
            safe_asset = {
                "asset_id": asset_id,
                "content_hash": str(asset.get("content_hash") or ""),
                "mime": str(asset.get("mime") or ""),
                "width": int(asset.get("width") or 0),
                "height": int(asset.get("height") or 0),
                "state": str(asset.get("state") or "available"),
                "content_url": (
                    f"/api/image-assets/{asset_id}/content"
                    if str(asset.get("state") or "available")
                    == "available"
                    else None
                ),
            }
    asset_projection = (
        PortraitAssetProjection.model_validate(safe_asset)
        if safe_asset is not None
        else None
    )
    internal_status = str(document.get("status") or "pending")
    public_status = internal_status
    failure = (
        ImageFailure.model_validate(document["failure"])
        if isinstance(document.get("failure"), dict)
        else document.get("failure")
    )
    provider_alias = str(document.get("provider_alias") or "")
    return PortraitJobProjection(
        job_id=_job_id(document),
        status=public_status,
        terminal=(
            bool(document.get("is_terminal"))
            and not bool(document.get("cleanup_pending"))
        ),
        queue_position=document.get("queue_position"),
        estimated_seconds=max(0, int(document.get("estimated_seconds") or 0)),
        elapsed_seconds=max(0, int(document.get("elapsed_seconds") or 0)),
        completed_images=max(0, int(document.get("completed_images") or 0)),
        submit_count=max(0, int(document.get("submit_count") or 0)),
        selected_as_current=(
            bool(document.get("selected_as_current"))
            if document.get("selected_as_current") is not None
            else None
        ),
        cleanup_pending=bool(document.get("cleanup_pending")),
        abandonable=(
            bool(document.get("cleanup_pending"))
            and str(document.get("late_cleanup_phase") or "")
            == "await_handle"
            and not isinstance(document.get("handle"), dict)
        ),
        ignored_slots=tuple(document.get("ignored_slots") or ()),
        failure=failure,
        warnings=tuple(document.get("warnings") or ()),
        anchor=(
            AppearanceAnchorSchema.model_validate(document["anchor"])
            if isinstance(document.get("anchor"), dict)
            else None
        ),
        asset=asset_projection,
        provider=(
            PortraitProviderProjection(
                alias=provider_alias,
                model=str(document.get("model") or ""),
                workflow_revision=str(
                    document.get("workflow_revision") or ""
                ),
                reference_mode=str(
                    document.get("reference_mode") or "none"
                ),
                available=not (
                    failure is not None
                    and failure.code.value == "provider_unavailable"
                ),
                queue_position=document.get("queue_position"),
                estimated_seconds=max(
                    0,
                    int(document.get("estimated_seconds") or 0),
                ),
                warnings=tuple(document.get("warnings") or ()),
            )
            if provider_alias
            else None
        ),
    )


def _image_job_projection(document: dict[str, Any]) -> ImageJobProjection:
    return ImageJobProjection.model_validate(
        _projection(document).model_dump(
            mode="python",
            exclude={"anchor"},
            exclude_computed_fields=True,
        )
    )


def _without_portrait_fields(
    projection: PortraitJobProjection,
) -> ImageJobProjection:
    return ImageJobProjection.model_validate(
        projection.model_dump(
            mode="python",
            exclude={"anchor"},
            exclude_computed_fields=True,
        )
    )


def _runtime_signature(value: Any) -> tuple[Any, ...]:
    fingerprint = RuntimeFingerprintSchema.model_validate(value or {})
    precision = tuple(
        sorted(
            {
                item.strip()
                for item in fingerprint.precision.split(",")
                if item.strip()
            }
        )
    )
    return (
        fingerprint.comfyui_version,
        fingerprint.pytorch_version,
        tuple(
            sorted(
                (item.name, item.version)
                for item in fingerprint.package_versions
            )
        ),
        tuple(sorted(fingerprint.devices)),
        precision,
        tuple(sorted(fingerprint.checkpoint_names)),
        tuple(sorted(fingerprint.lora_names)),
    )


def appearance_anchor_drift_labels(
    anchor: dict[str, Any] | AppearanceAnchorSchema,
    snapshot: ImageProviderSnapshot,
) -> tuple[str, ...]:
    """Return the shared portrait/scene consistency dimensions that changed."""

    canonical = AppearanceAnchorSchema.model_validate(anchor).model_dump(
        mode="python"
    )
    comparisons = (
        ("后端", canonical["provider"], snapshot.alias),
        ("模型", canonical["model"], snapshot.model),
        (
            "workflow 版本",
            canonical["workflow_revision"],
            snapshot.workflow_revision,
        ),
        (
            "参考模式",
            canonical["reference_mode"],
            snapshot.reference_mode,
        ),
    )
    changed = [
        label
        for label, previous, current in comparisons
        if current and previous != current
    ]
    if snapshot.runtime_fingerprint is not None and (
        _runtime_signature(canonical["runtime_fingerprint"])
        != _runtime_signature(snapshot.runtime_fingerprint)
    ):
        changed.append("运行环境")
    return tuple(changed)


class SingleImageJobService:
    def __init__(
        self,
        *,
        jobs: ImageJobRepositoryProtocol,
        anchors: AppearanceAnchorGatewayProtocol | None,
        provider_resolver: ImageProviderResolverProtocol,
        usage: ImageJobUsage = "character_portrait",
        completion_adapter: ImageJobCompletionAdapter | None = None,
        asset_consumer: ImagePollAssetConsumer | None = None,
        asset_reader: ManagedAssetReaderProtocol | None = None,
        asset_repository: ImageAssetMetadataRepositoryProtocol | None = None,
        now: Callable[[], datetime],
        now_epoch: Callable[[], float] = time.time,
        seed_factory: Callable[[], int] = lambda: secrets.randbits(64),
    ) -> None:
        self.jobs = jobs
        self.anchors = anchors
        self.provider_resolver = provider_resolver
        self.usage = usage
        self.completion_adapter = completion_adapter
        self.asset_consumer = asset_consumer or ImagePollAssetConsumer()
        self.asset_reader = asset_reader or ManagedImageAssetService()
        self.asset_repository = asset_repository or image_asset_repo
        self._now = now
        self._now_epoch = now_epoch
        self._seed_factory = seed_factory

    async def get_job_state(
        self,
        *,
        scope: ImageJobScope,
        provider_alias: str | None = None,
    ) -> ImageJobStateProjection:
        state, _snapshot = await self.get_job_state_with_snapshot(
            scope=scope,
            provider_alias=provider_alias,
        )
        return state

    async def get_job_state_with_snapshot(
        self,
        *,
        scope: ImageJobScope,
        provider_alias: str | None = None,
    ) -> tuple[ImageJobStateProjection, ImageProviderSnapshot]:
        scope = scope.canonical()
        if scope.usage != self.usage:
            raise ValueError("Image job scope usage does not match the service")
        active = await self.jobs.find_active_owned_job(
            owner_id=scope.owner_id,
            novel_id=scope.novel_id,
            card_id=scope.subject_id,
        )
        cleanup = await self.jobs.find_pending_cleanup_owned_job(
            owner_id=scope.owner_id,
            novel_id=scope.novel_id,
            card_id=scope.subject_id,
        )
        if (
            active is not None
            and cleanup is not None
            and _job_id(active) == _job_id(cleanup)
        ):
            cleanup = None
        inspect_provider = getattr(self.provider_resolver, "inspect", None)
        if inspect_provider is None:
            snapshot = ImageProviderSnapshot(
                alias=str(provider_alias or ""),
                available=False,
                timeout_seconds=600,
                warnings=("当前无法读取图像后端队列",),
            )
        else:
            snapshot = await inspect_provider(
                usage=scope.usage,
                provider_alias=provider_alias,
            )
        historical = (
            await self.jobs.median_completed_seconds(
                provider_alias=snapshot.alias,
            )
            if snapshot.alias
            else None
        )
        base_estimate = int(historical or snapshot.timeout_seconds)
        provider_warnings = list(snapshot.warnings)
        if historical is None:
            provider_warnings.append(
                "暂无历史耗时样本，预计耗时使用后端超时值作为保守上限"
            )
        provider = PortraitProviderProjection(
            alias=snapshot.alias,
            model=snapshot.model,
            workflow_revision=snapshot.workflow_revision,
            reference_mode=snapshot.reference_mode,
            available=snapshot.available,
            queue_position=(
                max(0, snapshot.queue_position)
                if snapshot.available
                else None
            ),
            estimated_seconds=(
                max(
                    0,
                    base_estimate * max(1, snapshot.queue_position + 1),
                )
                if snapshot.available
                else None
            ),
            warnings=tuple(dict.fromkeys(provider_warnings)),
        )
        return (
            ImageJobStateProjection(
                active_job=(
                    _image_job_projection(active)
                    if active is not None
                    else None
                ),
                cleanup_job=(
                    _image_job_projection(cleanup)
                    if cleanup is not None
                    else None
                ),
                provider=provider,
            ),
            snapshot,
        )

    async def get_state(
        self,
        *,
        owner_id: str,
        novel_id: str,
        card_id: str,
        provider_alias: str | None = None,
    ) -> CharacterPortraitStateProjection:
        owner_id = _canonical_object_id(owner_id, field="owner_id")
        novel_id = _canonical_object_id(novel_id, field="novel_id")
        card_id = _canonical_object_id(card_id, field="card_id")
        if self.anchors is None:
            raise PortraitConfigurationError(
                "Character portrait state requires an appearance-anchor gateway"
            )
        anchor = await self.anchors.get_anchor(
            novel_id=novel_id,
            card_id=card_id,
        )
        active = await self.jobs.find_active_owned_job(
            owner_id=owner_id,
            novel_id=novel_id,
            card_id=card_id,
        )
        cleanup = await self.jobs.find_pending_cleanup_owned_job(
            owner_id=owner_id,
            novel_id=novel_id,
            card_id=card_id,
        )
        if (
            active is not None
            and cleanup is not None
            and _job_id(active) == _job_id(cleanup)
        ):
            cleanup = None
        latest = (
            active
            if active is not None
            else await self.jobs.find_latest_owned_job(
                owner_id=owner_id,
                novel_id=novel_id,
                card_id=card_id,
            )
        )

        inspect_provider = getattr(self.provider_resolver, "inspect", None)
        if inspect_provider is None:
            snapshot = ImageProviderSnapshot(
                alias=str(provider_alias or ""),
                available=False,
                timeout_seconds=600,
                warnings=("当前无法读取图像后端队列",),
            )
        else:
            snapshot = await inspect_provider(
                usage="character_portrait",
                provider_alias=provider_alias,
            )
        historical = (
            await self.jobs.median_completed_seconds(
                provider_alias=snapshot.alias,
            )
            if snapshot.alias
            else None
        )
        base_estimate = int(historical or snapshot.timeout_seconds)
        provider_warnings = list(snapshot.warnings)
        if historical is None:
            provider_warnings.append(
                "暂无历史耗时样本，预计耗时使用后端超时值作为保守上限"
            )
        provider_projection = PortraitProviderProjection(
            alias=snapshot.alias,
            model=snapshot.model,
            workflow_revision=snapshot.workflow_revision,
            reference_mode=snapshot.reference_mode,
            available=snapshot.available,
            queue_position=(
                max(0, snapshot.queue_position)
                if snapshot.available
                else None
            ),
            estimated_seconds=(
                max(
                    0,
                    base_estimate
                    * max(1, snapshot.queue_position + 1),
                )
                if snapshot.available
                else None
            ),
            warnings=tuple(dict.fromkeys(provider_warnings)),
        )

        canonical_anchor = self._canonical_anchor(anchor)
        asset_projection: PortraitAssetProjection | None = None
        raw_asset: dict[str, Any] | None = None
        if canonical_anchor is not None:
            raw_asset = await self.asset_repository.get_owned_subject_hash(
                owner_id=to_object_id(owner_id),
                novel_id=to_object_id(novel_id),
                subject_kind="character_portrait",
                subject_id=card_id,
                content_hash=str(canonical_anchor["reference_asset"]),
            )
            if raw_asset is not None:
                raw_asset = {
                    "asset_id": str(raw_asset["_id"]),
                    **raw_asset,
                }
        elif isinstance(latest, dict) and isinstance(
            latest.get("asset"), dict
        ):
            raw_asset = dict(latest["asset"])
        if raw_asset is None and canonical_anchor is None:
            get_imported = getattr(
                self.asset_repository,
                "get_latest_owned_imported_subject",
                None,
            )
            if callable(get_imported):
                imported_asset = await get_imported(
                    owner_id=to_object_id(owner_id),
                    novel_id=to_object_id(novel_id),
                    subject_kind="character_portrait",
                    subject_id=card_id,
                )
                if imported_asset is not None:
                    raw_asset = {
                        "asset_id": str(imported_asset["_id"]),
                        **imported_asset,
                    }
        if isinstance(raw_asset, dict) and raw_asset.get("asset_id"):
            asset_state = "available"
            try:
                await self.asset_reader.read_owned_asset(
                    owner_id=owner_id,
                    asset_id=str(raw_asset["asset_id"]),
                )
            except (ImageAssetNotFoundError, ImageAssetIntegrityError):
                asset_state = "missing"
            asset_projection = PortraitAssetProjection(
                asset_id=str(raw_asset["asset_id"]),
                content_hash=str(raw_asset.get("content_hash") or ""),
                mime=str(raw_asset.get("mime") or ""),
                width=int(raw_asset.get("width") or 0),
                height=int(raw_asset.get("height") or 0),
                state=asset_state,
                content_url=(
                    f"/api/image-assets/{raw_asset['asset_id']}/content"
                    if asset_state == "available"
                    else None
                ),
            )

        consistency_warnings: list[str] = []
        if canonical_anchor is not None and snapshot.available:
            changed = appearance_anchor_drift_labels(
                canonical_anchor,
                snapshot,
            )
            if changed:
                consistency_warnings.append(
                    "当前图像环境与外观锚点建立时不同（"
                    + "、".join(changed)
                    + "），后续插图的一致性保证会减弱"
                )
        return CharacterPortraitStateProjection(
            anchor=canonical_anchor,
            asset=asset_projection,
            active_job=(
                _projection(active) if active is not None else None
            ),
            cleanup_job=(
                _projection(cleanup) if cleanup is not None else None
            ),
            provider=provider_projection,
            warnings=tuple(
                dict.fromkeys(consistency_warnings)
            ),
        )

    async def _cas_or_winner(
        self,
        *,
        owner_id: str,
        novel_id: str,
        card_id: str,
        job_id: str,
        expected_revision: int,
        fields: dict[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        updated = await self.jobs.compare_and_update_owned_job(
            owner_id=owner_id,
            novel_id=novel_id,
            card_id=card_id,
            job_id=job_id,
            expected_revision=expected_revision,
            fields=fields,
        )
        if updated is not None:
            return updated, True
        winner = await self.jobs.get_owned_job(
            owner_id=owner_id,
            novel_id=novel_id,
            card_id=card_id,
            job_id=job_id,
        )
        if winner is None:
            raise PortraitJobNotFoundError(
                "Image job disappeared during state transition"
            )
        return winner, False

    @staticmethod
    def _canonical_anchor(value: Any) -> dict[str, Any] | None:
        if value is None:
            return None
        return AppearanceAnchorSchema.model_validate(value).model_dump(
            mode="json"
        )

    @staticmethod
    def _submit_handle_fields(
        handle: ImageJobHandle,
        *,
        estimated_seconds: int | None,
    ) -> dict[str, Any]:
        unit_estimated_seconds = max(
            0,
            int(estimated_seconds or handle.timeout_seconds),
        )
        return {
            "handle": handle.model_dump(mode="json"),
            "submit_count": 1,
            "ignored_slots": list(handle.ignored_slots),
            "model": _stable_model(handle),
            "workflow_revision": handle.audit.template_revision,
            "unit_estimated_seconds": unit_estimated_seconds,
            "estimated_seconds": unit_estimated_seconds,
        }

    def _elapsed_seconds(
        self,
        job: dict[str, Any],
        *,
        at_epoch: float | None = None,
    ) -> int:
        current_epoch = self._now_epoch() if at_epoch is None else at_epoch
        return max(
            0,
            int(
                current_epoch
                - float(job.get("started_at_epoch") or self._now_epoch())
            ),
        )

    @staticmethod
    def _cancel_result_fields(
        result: ImageCancelResult,
        *,
        elapsed_seconds: int,
        handle_fields: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        common = {
            **(handle_fields or {}),
            "queue_position": None,
            "elapsed_seconds": elapsed_seconds,
            "cancel_claim_token": None,
            "cancel_claimed_at_epoch": None,
            "failure": (
                result.failure.model_dump(
                    mode="json",
                    exclude_computed_fields=True,
                )
                if result.failure is not None
                else None
            ),
        }
        if result.status == "cancelled":
            return {
                **common,
                "status": "cancelled",
                "is_terminal": True,
            }
        if result.status == "already_finished":
            return {
                **common,
                "status": "pending",
                "is_terminal": False,
            }
        return {
            **common,
            "status": "cancel_failed",
            "is_terminal": False,
        }

    @staticmethod
    def _cancellation_lease_seconds(handle: ImageJobHandle) -> int:
        # ComfyUI cancellation can issue cancel + two history/queue
        # confirmations + one legacy fallback. Every HTTP request uses the
        # provider timeout, so the lease must outlive the complete sequence.
        return max(
            120,
            (
                max(1, int(handle.timeout_seconds))
                * CANCEL_PROVIDER_REQUEST_UPPER_BOUND
            )
            + CANCEL_LEASE_MARGIN_SECONDS,
        )

    def _cancellation_lease_is_fresh(
        self,
        job: dict[str, Any],
        handle: ImageJobHandle,
        *,
        now_epoch: float,
    ) -> bool:
        claimed_at = float(job.get("cancel_claimed_at_epoch") or 0)
        return (
            bool(job.get("cancel_claim_token"))
            and claimed_at > 0
            and now_epoch - claimed_at
            < self._cancellation_lease_seconds(handle)
        )

    async def _cancel_returned_handle(
        self,
        *,
        provider: ImageProvider,
        handle: ImageJobHandle,
    ) -> ImageCancelResult:
        try:
            return await provider.cancel(handle)
        except ImageProviderError as error:
            return ImageCancelResult(
                status="failed",
                failure=error.failure,
            )
        except Exception:
            return ImageCancelResult(
                status="failed",
                failure=ImageFailure(
                    code="execution_failed",
                    message="图像任务取消确认失败",
                    action="检查图像后端队列，确认任务状态后再决定是否重试取消",
                ),
            )

    async def _claim_nonterminal_cancellation(
        self,
        *,
        owner_id: str,
        novel_id: str,
        card_id: str,
        job_id: str,
        job: dict[str, Any],
        handle: ImageJobHandle,
        cleanup_pending: bool = False,
    ) -> tuple[dict[str, Any], bool]:
        current = job
        while True:
            if bool(current.get("is_terminal")) or str(
                current.get("status") or ""
            ) in {"storing_asset", "finalizing"}:
                return current, False
            now_epoch = self._now_epoch()
            if self._cancellation_lease_is_fresh(
                current,
                handle,
                now_epoch=now_epoch,
            ):
                return current, False
            claimed, won = await self._cas_or_winner(
                owner_id=owner_id,
                novel_id=novel_id,
                card_id=card_id,
                job_id=job_id,
                expected_revision=int(
                    current.get("job_revision") or 0
                ),
                fields={
                    "status": (
                        "late_cleanup_pending"
                        if cleanup_pending
                        else "cancelling"
                    ),
                    "cancel_requested": True,
                    **(
                        {
                            "cleanup_pending": True,
                            "late_cleanup_phase": "cancel",
                        }
                        if cleanup_pending
                        else {}
                    ),
                    "cancel_claim_token": secrets.token_hex(16),
                    "cancel_claimed_at_epoch": now_epoch,
                },
            )
            if won:
                return claimed, True
            current = claimed

    async def _execute_nonterminal_cancellation(
        self,
        *,
        owner_id: str,
        novel_id: str,
        card_id: str,
        job_id: str,
        job: dict[str, Any],
        provider: ImageProvider | None = None,
    ) -> PortraitJobProjection:
        raw_handle = job.get("handle")
        if not isinstance(raw_handle, dict):
            return _projection(job)
        handle = ImageJobHandle.model_validate(raw_handle)
        claimed, won = await self._claim_nonterminal_cancellation(
            owner_id=owner_id,
            novel_id=novel_id,
            card_id=card_id,
            job_id=job_id,
            job=job,
            handle=handle,
        )
        if not won:
            return _projection(claimed)

        resolved: ResolvedImageProvider | None = None
        selected_provider = provider
        try:
            if selected_provider is None:
                resolved = await self.provider_resolver.resolve(
                    usage=self.usage,
                    provider_alias=handle.provider_alias,
                )
                selected_provider = resolved.provider
            assert selected_provider is not None
            result = await self._cancel_returned_handle(
                provider=selected_provider,
                handle=handle,
            )
        except Exception:
            result = ImageCancelResult(
                status="failed",
                failure=ImageFailure(
                    code="provider_configuration_error",
                    message="无法载入该任务原有的图像后端以确认取消",
                    action="恢复或启用原图像后端配置后，再次取消或检查其队列",
                ),
            )
        finally:
            if resolved is not None:
                try:
                    await resolved.aclose()
                except Exception:
                    pass
        updated, _ = await self._cas_or_winner(
            owner_id=owner_id,
            novel_id=novel_id,
            card_id=card_id,
            job_id=job_id,
            expected_revision=int(claimed.get("job_revision") or 0),
            fields=self._cancel_result_fields(
                result,
                elapsed_seconds=self._elapsed_seconds(claimed),
            ),
        )
        return _projection(updated)

    @staticmethod
    def _late_cleanup_cancel_result_fields(
        result: ImageCancelResult,
        *,
        elapsed_seconds: int,
        attempt_count: int,
    ) -> dict[str, Any]:
        failure = (
            result.failure.model_dump(
                mode="json",
                exclude_computed_fields=True,
            )
            if result.failure is not None
            else None
        )
        common = {
            "queue_position": None,
            "elapsed_seconds": elapsed_seconds,
            "cancel_claim_token": None,
            "cancel_claimed_at_epoch": None,
            "failure": failure,
        }
        if result.status == "cancelled":
            return {
                **common,
                "status": "cancelled",
                "is_terminal": True,
                "cleanup_pending": False,
                "late_cleanup_phase": None,
            }
        if result.status == "already_finished":
            return {
                **common,
                "status": "late_cleanup_pending",
                "is_terminal": False,
                "cleanup_pending": True,
                "late_cleanup_phase": "poll",
                "failure": None,
            }
        return {
            **common,
            "status": "late_cleanup_pending",
            "is_terminal": False,
            "cleanup_pending": True,
            "late_cleanup_phase": "cancel",
            "late_cleanup_attempts": attempt_count + 1,
        }

    async def _execute_late_cleanup_cancellation(
        self,
        *,
        owner_id: str,
        novel_id: str,
        card_id: str,
        job_id: str,
        job: dict[str, Any],
        provider: ImageProvider | None = None,
        explicit: bool = False,
    ) -> PortraitJobProjection:
        raw_handle = job.get("handle")
        if not isinstance(raw_handle, dict):
            return _projection(job)
        previous_failure = (
            ImageFailure.model_validate(job["failure"])
            if isinstance(job.get("failure"), dict)
            else None
        )
        attempt_count = max(0, int(job.get("late_cleanup_attempts") or 0))
        if (
            not explicit
            and attempt_count > 0
            and previous_failure is not None
            and not previous_failure.retryable
        ):
            return _projection(job)

        handle = ImageJobHandle.model_validate(raw_handle)
        claimed, won = await self._claim_nonterminal_cancellation(
            owner_id=owner_id,
            novel_id=novel_id,
            card_id=card_id,
            job_id=job_id,
            job=job,
            handle=handle,
            cleanup_pending=True,
        )
        if not won:
            return _projection(claimed)

        resolved: ResolvedImageProvider | None = None
        selected_provider = provider
        try:
            if selected_provider is None:
                resolved = await self.provider_resolver.resolve(
                    usage=self.usage,
                    provider_alias=handle.provider_alias,
                )
                selected_provider = resolved.provider
            assert selected_provider is not None
            result = await self._cancel_returned_handle(
                provider=selected_provider,
                handle=handle,
            )
        except Exception:
            result = ImageCancelResult(
                status="failed",
                failure=ImageFailure(
                    code="provider_configuration_error",
                    message="无法载入该任务原有的图像后端以确认取消",
                    action="恢复或启用原图像后端配置后，再次取消或检查其队列",
                ),
            )
        finally:
            if resolved is not None:
                try:
                    await resolved.aclose()
                except Exception:
                    pass

        updated, persisted = await self._cas_or_winner(
            owner_id=owner_id,
            novel_id=novel_id,
            card_id=card_id,
            job_id=job_id,
            expected_revision=int(claimed.get("job_revision") or 0),
            fields=self._late_cleanup_cancel_result_fields(
                result,
                elapsed_seconds=self._elapsed_seconds(claimed),
                attempt_count=attempt_count,
            ),
        )
        if not persisted:
            return _projection(updated)
        if result.status == "already_finished":
            return await self.poll(
                owner_id=owner_id,
                novel_id=novel_id,
                card_id=card_id,
                job_id=job_id,
            )
        return _projection(updated)

    async def _terminal_late_cas_or_winner(
        self,
        *,
        owner_id: str,
        novel_id: str,
        card_id: str,
        job_id: str,
        expected_revision: int,
        fields: dict[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        updated = await self.jobs.compare_and_update_terminal_job(
            owner_id=owner_id,
            novel_id=novel_id,
            card_id=card_id,
            job_id=job_id,
            expected_revision=expected_revision,
            fields=fields,
        )
        if updated is not None:
            return updated, True
        winner = await self.jobs.get_owned_job(
            owner_id=owner_id,
            novel_id=novel_id,
            card_id=card_id,
            job_id=job_id,
        )
        if winner is None:
            raise PortraitJobNotFoundError(
                "Image job disappeared during late reconciliation"
            )
        return winner, False

    async def _resume_terminal_late_reconciliation(
        self,
        *,
        owner_id: str,
        novel_id: str,
        card_id: str,
        job_id: str,
        job: dict[str, Any],
        provider: ImageProvider | None = None,
        explicit: bool = False,
    ) -> PortraitJobProjection:
        raw_handle = job.get("handle")
        if not isinstance(raw_handle, dict):
            return _projection(job)
        handle = ImageJobHandle.model_validate(raw_handle)
        phase = str(job.get("late_cleanup_phase") or "")
        previous_failure = (
            ImageFailure.model_validate(job["failure"])
            if isinstance(job.get("failure"), dict)
            else None
        )
        attempts = max(0, int(job.get("late_cleanup_attempts") or 0))
        if (
            not explicit
            and attempts > 0
            and previous_failure is not None
            and not previous_failure.retryable
        ):
            return _projection(job)
        now_epoch = self._now_epoch()
        if self._cancellation_lease_is_fresh(
            job,
            handle,
            now_epoch=now_epoch,
        ):
            return _projection(job)
        claimed, won = await self._terminal_late_cas_or_winner(
            owner_id=owner_id,
            novel_id=novel_id,
            card_id=card_id,
            job_id=job_id,
            expected_revision=int(job.get("job_revision") or 0),
            fields={
                "status": "late_cleanup_pending",
                "cleanup_pending": True,
                "cancel_claim_token": secrets.token_hex(16),
                "cancel_claimed_at_epoch": now_epoch,
            },
        )
        if not won:
            return _projection(claimed)

        resolved: ResolvedImageProvider | None = None
        selected_provider = provider
        try:
            if selected_provider is None:
                try:
                    resolved = await self.provider_resolver.resolve(
                        usage=self.usage,
                        provider_alias=handle.provider_alias,
                    )
                    selected_provider = resolved.provider
                except Exception:
                    failure = ImageFailure(
                        code="provider_configuration_error",
                        message="无法载入晚到任务原有的图像后端",
                        action="恢复或启用原图像后端配置后，再次取消或查询该任务",
                    )
                    updated, _ = (
                        await self._terminal_late_cas_or_winner(
                            owner_id=owner_id,
                            novel_id=novel_id,
                            card_id=card_id,
                            job_id=job_id,
                            expected_revision=int(
                                claimed.get("job_revision") or 0
                            ),
                            fields={
                                "status": "late_cleanup_pending",
                                "cleanup_pending": True,
                                "late_cleanup_phase": phase,
                                "late_cleanup_attempts": attempts + 1,
                                "cancel_claim_token": None,
                                "cancel_claimed_at_epoch": None,
                                "elapsed_seconds": self._elapsed_seconds(
                                    claimed
                                ),
                                "failure": failure.model_dump(
                                    mode="json",
                                    exclude_computed_fields=True,
                                ),
                            },
                        )
                    )
                    return _projection(updated)
            assert selected_provider is not None
            if phase == "cancel":
                result = await self._cancel_returned_handle(
                    provider=selected_provider,
                    handle=handle,
                )
                if result.status == "already_finished":
                    polling, persisted = (
                        await self._terminal_late_cas_or_winner(
                            owner_id=owner_id,
                            novel_id=novel_id,
                            card_id=card_id,
                            job_id=job_id,
                            expected_revision=int(
                                claimed.get("job_revision") or 0
                            ),
                            fields={
                                "status": "late_cleanup_pending",
                                "cleanup_pending": True,
                                "late_cleanup_phase": "poll",
                                "cancel_claim_token": None,
                                "cancel_claimed_at_epoch": None,
                                "failure": None,
                            },
                        )
                    )
                    if not persisted:
                        return _projection(polling)
                    return await self._resume_terminal_late_reconciliation(
                        owner_id=owner_id,
                        novel_id=novel_id,
                        card_id=card_id,
                        job_id=job_id,
                        job=polling,
                        provider=selected_provider,
                    )
                failure = (
                    result.failure.model_dump(
                        mode="json",
                        exclude_computed_fields=True,
                    )
                    if result.failure is not None
                    else None
                )
                if result.status == "cancelled":
                    fields = {
                        "status": "cancelled",
                        "cleanup_pending": False,
                        "late_cleanup_phase": None,
                        "cancel_claim_token": None,
                        "cancel_claimed_at_epoch": None,
                        "elapsed_seconds": self._elapsed_seconds(claimed),
                        "failure": failure,
                    }
                else:
                    fields = {
                        "status": "late_cleanup_pending",
                        "cleanup_pending": True,
                        "late_cleanup_phase": "cancel",
                        "late_cleanup_attempts": attempts + 1,
                        "cancel_claim_token": None,
                        "cancel_claimed_at_epoch": None,
                        "elapsed_seconds": self._elapsed_seconds(claimed),
                        "failure": failure,
                    }
                updated, _ = await self._terminal_late_cas_or_winner(
                    owner_id=owner_id,
                    novel_id=novel_id,
                    card_id=card_id,
                    job_id=job_id,
                    expected_revision=int(
                        claimed.get("job_revision") or 0
                    ),
                    fields=fields,
                )
                return _projection(updated)

            try:
                poll_result = await selected_provider.poll(handle)
            except ImageProviderError as error:
                poll_result = ImagePollResult(
                    status="failed",
                    failure=error.failure,
                )
            except Exception:
                poll_result = ImagePollResult(
                    status="failed",
                    failure=ImageFailure(
                        code="execution_failed",
                        message="无法确认晚到图像任务的最终结果",
                        action="检查图像后端与该任务记录后，再决定是否重新查询",
                    ),
                )
            elapsed_seconds = self._elapsed_seconds(claimed)
            if not poll_result.is_terminal:
                updated, _ = await self._terminal_late_cas_or_winner(
                    owner_id=owner_id,
                    novel_id=novel_id,
                    card_id=card_id,
                    job_id=job_id,
                    expected_revision=int(
                        claimed.get("job_revision") or 0
                    ),
                    fields={
                        "status": "late_cleanup_pending",
                        "cleanup_pending": True,
                        "late_cleanup_phase": "poll",
                        "cancel_claim_token": None,
                        "cancel_claimed_at_epoch": None,
                        "elapsed_seconds": elapsed_seconds,
                        "failure": (
                            poll_result.failure.model_dump(
                                mode="json",
                                exclude_computed_fields=True,
                            )
                            if poll_result.failure is not None
                            else None
                        ),
                    },
                )
                return _projection(updated)
            if poll_result.status != "succeeded":
                updated, _ = await self._terminal_late_cas_or_winner(
                    owner_id=owner_id,
                    novel_id=novel_id,
                    card_id=card_id,
                    job_id=job_id,
                    expected_revision=int(
                        claimed.get("job_revision") or 0
                    ),
                    fields={
                        "status": poll_result.status,
                        "cleanup_pending": False,
                        "late_cleanup_phase": None,
                        "cancel_claim_token": None,
                        "cancel_claimed_at_epoch": None,
                        "elapsed_seconds": elapsed_seconds,
                        "completed_images": len(poll_result.artifacts),
                        "failure": (
                            poll_result.failure.model_dump(
                                mode="json",
                                exclude_computed_fields=True,
                            )
                            if poll_result.failure is not None
                            else None
                        ),
                    },
                )
                return _projection(updated)
            if len(poll_result.artifacts) != 1:
                failure = ImageFailure(
                    code="execution_failed",
                    message="单张图像任务没有返回恰好一张图片",
                    action="检查 workflow 产物节点，确保当前图像用途只输出一张图片",
                    details={
                        "artifact_count": len(poll_result.artifacts)
                    },
                )
                updated, _ = await self._terminal_late_cas_or_winner(
                    owner_id=owner_id,
                    novel_id=novel_id,
                    card_id=card_id,
                    job_id=job_id,
                    expected_revision=int(
                        claimed.get("job_revision") or 0
                    ),
                    fields={
                        "status": "failed",
                        "cleanup_pending": False,
                        "late_cleanup_phase": None,
                        "cancel_claim_token": None,
                        "cancel_claimed_at_epoch": None,
                        "elapsed_seconds": elapsed_seconds,
                        "completed_images": len(poll_result.artifacts),
                        "failure": failure.model_dump(
                            mode="json",
                            exclude_computed_fields=True,
                        ),
                    },
                )
                return _projection(updated)
            try:
                asset, _pending_anchor = (
                    await self._consume_successful_image(
                        owner_id=owner_id,
                        novel_id=novel_id,
                        card_id=card_id,
                        job=claimed,
                        handle=handle,
                        result=poll_result,
                    )
                )
            except Exception:
                failure = ImageFailure(
                    code="execution_failed",
                    message="图片已生成，但写入受管素材失败",
                    action="检查素材目录权限和剩余空间；已生成张数与消耗已记账，修复后由用户决定是否重新发起",
                )
                updated, _ = await self._terminal_late_cas_or_winner(
                    owner_id=owner_id,
                    novel_id=novel_id,
                    card_id=card_id,
                    job_id=job_id,
                    expected_revision=int(
                        claimed.get("job_revision") or 0
                    ),
                    fields={
                        "status": "failed",
                        "cleanup_pending": False,
                        "late_cleanup_phase": None,
                        "cancel_claim_token": None,
                        "cancel_claimed_at_epoch": None,
                        "elapsed_seconds": elapsed_seconds,
                        "completed_images": 1,
                        "failure": failure.model_dump(
                            mode="json",
                            exclude_computed_fields=True,
                        ),
                    },
                )
                return _projection(updated)
            updated, _ = await self._terminal_late_cas_or_winner(
                owner_id=owner_id,
                novel_id=novel_id,
                card_id=card_id,
                job_id=job_id,
                expected_revision=int(
                    claimed.get("job_revision") or 0
                ),
                fields={
                    "status": "succeeded",
                    "cleanup_pending": False,
                    "late_cleanup_phase": None,
                    "cancel_claim_token": None,
                    "cancel_claimed_at_epoch": None,
                    "elapsed_seconds": elapsed_seconds,
                    "completed_images": 1,
                    # A replacement job is already active. Preserve the late
                    # artifact for accounting without making it the current
                    # portrait or changing the replacement's anchor baseline.
                    "late_result_asset": asset,
                    "failure": None,
                },
            )
            return _projection(updated)
        finally:
            if resolved is not None:
                try:
                    await resolved.aclose()
                except Exception:
                    pass

    async def _attach_submitted_handle(
        self,
        *,
        owner_id: str,
        novel_id: str,
        card_id: str,
        job_id: str,
        handle: ImageJobHandle,
        estimated_seconds: int | None,
        provider: ImageProvider,
    ) -> PortraitJobProjection:
        """Attach exactly one returned handle without losing a cancel intent."""

        handle_fields = self._submit_handle_fields(
            handle,
            estimated_seconds=estimated_seconds,
        )
        while True:
            current = await self.jobs.get_owned_job(
                owner_id=owner_id,
                novel_id=novel_id,
                card_id=card_id,
                job_id=job_id,
            )
            if current is None:
                # The active-job unique index gives this returned handle one
                # submit owner. With the record hard-deleted there is no
                # user-cancel path that could race this cleanup.
                await self._cancel_returned_handle(
                    provider=provider,
                    handle=handle,
                )
                raise PortraitJobNotFoundError(
                    "Image job disappeared after provider submission"
                )
            persisted_handle = current.get("handle")
            if bool(current.get("is_terminal")):
                if not isinstance(persisted_handle, dict):
                    reopened = await self.jobs.attach_late_handle(
                        owner_id=owner_id,
                        novel_id=novel_id,
                        card_id=card_id,
                        job_id=job_id,
                        fields={
                            **handle_fields,
                            "status": "late_cleanup_pending",
                            "is_terminal": False,
                            "cleanup_pending": True,
                            "late_cleanup_phase": "cancel",
                            "late_cleanup_attempts": 0,
                        },
                    )
                    if reopened is not None:
                        return await self._execute_late_cleanup_cancellation(
                            owner_id=owner_id,
                            novel_id=novel_id,
                            card_id=card_id,
                            job_id=job_id,
                            job=reopened,
                            provider=provider,
                        )
                    replacement = await self.jobs.find_active_owned_job(
                        owner_id=owner_id,
                        novel_id=novel_id,
                        card_id=card_id,
                    )
                    audited = await self.jobs.attach_terminal_late_handle(
                        owner_id=owner_id,
                        novel_id=novel_id,
                        card_id=card_id,
                        job_id=job_id,
                        fields={
                            **handle_fields,
                            "status": "late_cleanup_pending",
                            "cleanup_pending": True,
                            "late_cleanup_phase": "cancel",
                            "late_cleanup_attempts": 0,
                        },
                    )
                    if audited is not None:
                        reconciled = await (
                            self._resume_terminal_late_reconciliation(
                                owner_id=owner_id,
                                novel_id=novel_id,
                                card_id=card_id,
                                job_id=job_id,
                                job=audited,
                                provider=provider,
                            )
                        )
                        if (
                            replacement is not None
                            and _job_id(replacement) != job_id
                        ):
                            current_replacement = (
                                await self.jobs.get_owned_job(
                                    owner_id=owner_id,
                                    novel_id=novel_id,
                                    card_id=card_id,
                                    job_id=_job_id(replacement),
                                )
                            )
                            return _projection(
                                current_replacement or replacement
                            )
                        return reconciled
                if (
                    not isinstance(persisted_handle, dict)
                    or ImageJobHandle.model_validate(
                        persisted_handle
                    ).prompt_id
                    != handle.prompt_id
                ):
                    # Explicitly abandoning an await_handle state may race the
                    # original submit coroutine. That coroutine remains the
                    # sole owner of its unexpected return and must not leave
                    # the provider job running.
                    await self._cancel_returned_handle(
                        provider=provider,
                        handle=handle,
                    )
                return _projection(current)
            if isinstance(persisted_handle, dict):
                persisted = ImageJobHandle.model_validate(persisted_handle)
                if persisted.prompt_id != handle.prompt_id:
                    await self._cancel_returned_handle(
                        provider=provider,
                        handle=handle,
                    )
                    return _projection(current)
                if (
                    bool(current.get("cleanup_pending"))
                    and str(current.get("late_cleanup_phase") or "")
                    == "cancel"
                ):
                    return await self._execute_late_cleanup_cancellation(
                        owner_id=owner_id,
                        novel_id=novel_id,
                        card_id=card_id,
                        job_id=job_id,
                        job=current,
                        provider=provider,
                    )
                if (
                    bool(current.get("cancel_requested"))
                    and str(current.get("status") or "") == "cancelling"
                ):
                    return await self._execute_nonterminal_cancellation(
                        owner_id=owner_id,
                        novel_id=novel_id,
                        card_id=card_id,
                        job_id=job_id,
                        job=current,
                        provider=provider,
                    )
                return _projection(current)

            if bool(current.get("cleanup_pending")):
                fields = {
                    **handle_fields,
                    "status": "late_cleanup_pending",
                    "late_cleanup_phase": "cancel",
                }
            elif bool(current.get("cancel_requested")):
                # Persist the only resumable provider identity before issuing
                # cancellation I/O; a process crash must never lose it.
                fields = {
                    **handle_fields,
                    "status": "cancelling",
                }
            else:
                fields = {
                    **handle_fields,
                    "status": "pending",
                }
            updated, won = await self._cas_or_winner(
                owner_id=owner_id,
                novel_id=novel_id,
                card_id=card_id,
                job_id=job_id,
                expected_revision=int(current.get("job_revision") or 0),
                fields=fields,
            )
            if won:
                if bool(updated.get("cleanup_pending")):
                    return await self._execute_late_cleanup_cancellation(
                        owner_id=owner_id,
                        novel_id=novel_id,
                        card_id=card_id,
                        job_id=job_id,
                        job=updated,
                        provider=provider,
                    )
                if fields.get("status") == "cancelling":
                    continue
                return _projection(updated)

    async def _record_submit_failure(
        self,
        *,
        owner_id: str,
        novel_id: str,
        card_id: str,
        job_id: str,
        failure: ImageFailure,
    ) -> PortraitJobProjection:
        while True:
            current = await self.jobs.get_owned_job(
                owner_id=owner_id,
                novel_id=novel_id,
                card_id=card_id,
                job_id=job_id,
            )
            if current is None:
                raise PortraitJobNotFoundError(
                    "Image job disappeared after submission failure"
                )
            if bool(current.get("is_terminal")):
                return _projection(current)
            failed, won = await self._cas_or_winner(
                owner_id=owner_id,
                novel_id=novel_id,
                card_id=card_id,
                job_id=job_id,
                expected_revision=int(current.get("job_revision") or 0),
                fields={
                    "status": "failed",
                    "is_terminal": True,
                    "cleanup_pending": False,
                    "late_cleanup_phase": None,
                    "submit_count": 1,
                    "completed_images": 0,
                    "elapsed_seconds": self._elapsed_seconds(current),
                    "failure": failure.model_dump(
                        mode="json",
                        exclude_computed_fields=True,
                    ),
                },
            )
            if won:
                return _projection(failed)

    async def _consume_successful_image(
        self,
        *,
        owner_id: str,
        novel_id: str,
        card_id: str,
        job: dict[str, Any],
        handle: ImageJobHandle,
        result: ImagePollResult,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Store one provider artifact and derive its frozen anchor payload."""

        command = GeneratedImageAssetCreate(
            owner_id=owner_id,
            novel_id=novel_id,
            subject_kind=str(job.get("usage") or self.usage),
            subject_id=str(job.get("subject_id") or card_id),
            provider_alias=str(job["provider_alias"]),
            model=str(job["model"]),
            request_params={
                "usage": str(job.get("usage") or self.usage),
                "batch_size": 1,
                "workflow_revision": str(job["workflow_revision"]),
                "submitted_graph_hash": (
                    handle.audit.submitted_graph_hash
                ),
                "reference_mode": str(job["reference_mode"]),
                "input_asset_hashes": dict(
                    handle.audit.input_asset_hashes
                ),
                "runtime_fingerprint": (
                    handle.audit.runtime_fingerprint.model_dump(
                        mode="json"
                    )
                ),
            },
            final_prompt=str(job["final_prompt"]),
            negative_prompt=str(job.get("negative_prompt") or ""),
            seed=int(job["seed"]),
        )
        consumption = await self.asset_consumer.consume(
            result=result,
            command=command,
        )
        if len(consumption.assets) != 1:
            raise RuntimeError(
                "Managed single-image storage did not return one asset"
            )
        record = consumption.assets[0]
        asset = {
            "asset_id": record.asset_id,
            "content_hash": record.content_hash,
            "mime": record.mime,
            "width": record.width,
            "height": record.height,
            "state": "available",
        }
        if self.completion_adapter is not None:
            pending = await self.completion_adapter.prepare(
                scope=ImageJobScope(
                    owner_id=owner_id,
                    novel_id=novel_id,
                    usage=self.usage,
                    subject_id=str(job.get("subject_id") or card_id),
                ).canonical(),
                job=job,
                handle=handle,
                asset=asset,
            )
            return asset, pending
        fingerprint = handle.audit.runtime_fingerprint.model_copy(deep=True)
        if not fingerprint.checkpoint_names:
            fingerprint.checkpoint_names = list(
                handle.audit.checkpoint_names
            )
        if not fingerprint.lora_names:
            fingerprint.lora_names = list(handle.audit.lora_names)
        anchor_model = AppearanceAnchorSchema(
            descriptor=str(job["descriptor"]),
            seed=int(job["seed"]),
            reference_asset=record.content_hash,
            established_at=self._now(),
            provider=str(job["provider_alias"]),
            model=str(job["model"]),
            workflow_revision=str(job["workflow_revision"]),
            reference_mode=str(job["reference_mode"]),
            runtime_fingerprint=fingerprint,
        )
        return asset, anchor_model.model_dump(mode="json")

    async def _establish_pending_anchor(
        self,
        *,
        novel_id: str,
        card_id: str,
        job: dict[str, Any],
        pending: dict[str, Any],
    ) -> dict[str, Any]:
        current = await self.anchors.get_anchor(
            novel_id=novel_id,
            card_id=card_id,
        )
        canonical_current = self._canonical_anchor(current)
        expected_previous = self._canonical_anchor(
            job.get("anchor_before")
        )
        if canonical_current == pending:
            stored_anchor = pending
        elif canonical_current is None and expected_previous is None:
            stored_anchor = await self.anchors.establish_anchor(
                novel_id=novel_id,
                card_id=card_id,
                anchor=pending,
            )
        elif (
            canonical_current == expected_previous
            and expected_previous is not None
            and bool(job.get("confirm_anchor_reset"))
        ):
            stored_anchor = await self.anchors.reset_anchor(
                novel_id=novel_id,
                card_id=card_id,
                anchor=pending,
                expected_previous=expected_previous,
                confirmed=True,
            )
        else:
            raise AppearanceAnchorConflictError(
                "Appearance anchor changed while the portrait was running"
            )
        normalized = self._canonical_anchor(stored_anchor)
        if normalized is None:
            raise AppearanceAnchorConflictError(
                "Appearance anchor storage returned no anchor"
            )
        return normalized

    async def _resume_finalizing(
        self,
        *,
        owner_id: str,
        novel_id: str,
        card_id: str,
        job_id: str,
        job: dict[str, Any],
    ) -> PortraitJobProjection:
        revision = int(job.get("job_revision") or 0)
        if self.completion_adapter is not None:
            pending_completion = job.get("pending_completion")
            if not isinstance(pending_completion, dict):
                failure = ImageFailure(
                    code="execution_failed",
                    message="图片素材已保存，但缺少待完成的当前引用",
                    action="保留现有素材并从历史中手工选择；不要重新提交图像任务",
                )
                failed, _ = await self._cas_or_winner(
                    owner_id=owner_id,
                    novel_id=novel_id,
                    card_id=card_id,
                    job_id=job_id,
                    expected_revision=revision,
                    fields={
                        "status": "failed",
                        "is_terminal": True,
                        "cleanup_pending": False,
                        "late_cleanup_phase": None,
                        "selected_as_current": False,
                        "failure": failure.model_dump(
                            mode="json",
                            exclude_computed_fields=True,
                        ),
                    },
                )
                return _projection(failed)
            try:
                completion_fields = await self.completion_adapter.finalize(
                    scope=ImageJobScope(
                        owner_id=owner_id,
                        novel_id=novel_id,
                        usage=self.usage,
                        subject_id=card_id,
                    ).canonical(),
                    job=job,
                    pending=pending_completion,
                )
            except ImageCompletionError as error:
                failed, _ = await self._cas_or_winner(
                    owner_id=owner_id,
                    novel_id=novel_id,
                    card_id=card_id,
                    job_id=job_id,
                    expected_revision=revision,
                    fields={
                        "status": "failed",
                        "is_terminal": True,
                        "cleanup_pending": False,
                        "late_cleanup_phase": None,
                        "selected_as_current": False,
                        "completed_images": 1,
                        "failure": error.failure.model_dump(
                            mode="json",
                            exclude_computed_fields=True,
                        ),
                    },
                )
                return _projection(failed)
            completed, _ = await self._cas_or_winner(
                owner_id=owner_id,
                novel_id=novel_id,
                card_id=card_id,
                job_id=job_id,
                expected_revision=revision,
                fields={
                    "status": "succeeded",
                    "is_terminal": True,
                    "cleanup_pending": False,
                    "late_cleanup_phase": None,
                    "selected_as_current": True,
                    "completed_images": 1,
                    "failure": None,
                    **dict(completion_fields),
                },
            )
            return _projection(completed)

        pending = self._canonical_anchor(job.get("pending_anchor"))
        if pending is None:
            failure = ImageFailure(
                code="execution_failed",
                message="立绘素材已保存，但缺少待确立的外观锚点",
                action="保留现有素材并人工检查角色锚点；不要重新提交图像任务",
            )
            failed, _ = await self._cas_or_winner(
                owner_id=owner_id,
                novel_id=novel_id,
                card_id=card_id,
                job_id=job_id,
                expected_revision=revision,
                fields={
                    "status": "failed",
                    "is_terminal": True,
                    "cleanup_pending": False,
                    "late_cleanup_phase": None,
                    "failure": failure.model_dump(
                        mode="json",
                        exclude_computed_fields=True,
                    ),
                },
            )
            return _projection(failed)

        try:
            normalized_anchor = await self._establish_pending_anchor(
                novel_id=novel_id,
                card_id=card_id,
                job=job,
                pending=pending,
            )
        except AppearanceAnchorConflictError:
            latest = await self.anchors.get_anchor(
                novel_id=novel_id,
                card_id=card_id,
            )
            if self._canonical_anchor(latest) == pending:
                normalized_anchor = pending
            else:
                failure = ImageFailure(
                    code="execution_failed",
                    message="立绘已保存，但角色外观锚点同时被其他操作修改",
                    action="当前锚点显示在本角色卡的“外观锚点”区；核对基准素材后再决定是否人工重设。不要自动重跑。",
                )
                failed, _ = await self._cas_or_winner(
                    owner_id=owner_id,
                    novel_id=novel_id,
                    card_id=card_id,
                    job_id=job_id,
                    expected_revision=revision,
                    fields={
                        "status": "failed",
                        "is_terminal": True,
                        "cleanup_pending": False,
                        "late_cleanup_phase": None,
                        "failure": failure.model_dump(
                            mode="json",
                            exclude_computed_fields=True,
                        ),
                    },
                )
                return _projection(failed)

        completed, _ = await self._cas_or_winner(
            owner_id=owner_id,
            novel_id=novel_id,
            card_id=card_id,
            job_id=job_id,
            expected_revision=revision,
            fields={
                "status": "succeeded",
                "is_terminal": True,
                "cleanup_pending": False,
                "late_cleanup_phase": None,
                "anchor": normalized_anchor,
                "completed_images": 1,
                "failure": None,
            },
        )
        return _projection(completed)

    async def start_job(
        self,
        *,
        scope: ImageJobScope,
        plan: ImageJobPlan,
        provider_alias: str | None = None,
    ) -> ImageJobProjection:
        scope = scope.canonical()
        if scope.usage != self.usage:
            raise ValueError("Image job scope usage does not match the service")
        if plan.seed is not None and (
            type(plan.seed) is not int
            or not 0 <= plan.seed <= (2**64 - 1)
        ):
            raise ValueError("seed must be an integer from 0 through 2^64-1")
        active = await self.jobs.find_active_owned_job(
            owner_id=scope.owner_id,
            novel_id=scope.novel_id,
            card_id=scope.subject_id,
        )
        if active is not None:
            return _image_job_projection(active)
        pending_cleanup = await self.jobs.find_pending_cleanup_owned_job(
            owner_id=scope.owner_id,
            novel_id=scope.novel_id,
            card_id=scope.subject_id,
        )
        if pending_cleanup is not None:
            return _image_job_projection(pending_cleanup)

        chosen_seed = self._seed_factory() if plan.seed is None else plan.seed
        if (
            type(chosen_seed) is not int
            or not 0 <= chosen_seed <= (2**64 - 1)
        ):
            raise ValueError("seed must be an integer from 0 through 2^64-1")
        resolved = await self.provider_resolver.resolve(
            usage=scope.usage,
            provider_alias=provider_alias,
        )
        idempotency_material = {
            "owner_id": scope.owner_id,
            "novel_id": scope.novel_id,
            "usage": scope.usage,
            "subject_id": scope.subject_id,
            "provider_alias": resolved.alias,
            "prompt": plan.prompt.model_dump(mode="json"),
            "seed": chosen_seed,
            **dict(plan.idempotency_context),
        }
        idempotency_key = hashlib.sha256(
            json.dumps(
                idempotency_material,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        submitted_at = self._now()
        estimated_seconds = await self.jobs.median_completed_seconds(
            provider_alias=resolved.alias,
        )
        unit_estimated_seconds = max(
            0,
            int(estimated_seconds or resolved.timeout_seconds),
        )
        final_prompt = str(
            plan.persisted_fields.get("final_prompt")
            or _positive_prompt(plan.prompt)
        )
        negative_prompt = str(
            plan.persisted_fields.get(
                "negative_prompt",
                plan.prompt.negative,
            )
            or ""
        )
        document = {
            "owner_id": scope.owner_id,
            "novel_id": scope.novel_id,
            "subject_id": scope.subject_id,
            "usage": scope.usage,
            "idempotency_key": idempotency_key,
            "status": "submitting",
            "is_terminal": False,
            "job_revision": 0,
            "cancel_requested": False,
            "queue_position": None,
            "unit_estimated_seconds": unit_estimated_seconds,
            "estimated_seconds": unit_estimated_seconds,
            "elapsed_seconds": 0,
            "completed_images": 0,
            "submit_count": 0,
            "cleanup_pending": False,
            "late_cleanup_phase": None,
            "late_cleanup_attempts": 0,
            "ignored_slots": [],
            "failure": None,
            "warnings": [],
            "provider_alias": resolved.alias,
            "reference_mode": resolved.reference_mode,
            "prompt": plan.prompt.model_dump(mode="json"),
            "final_prompt": final_prompt,
            "negative_prompt": negative_prompt,
            "seed": str(chosen_seed),
            "submitted_at": submitted_at,
            "started_at_epoch": self._now_epoch(),
            "submit_timeout_seconds": resolved.timeout_seconds,
            **dict(plan.persisted_fields),
        }
        if scope.usage == "character_portrait":
            document["character_card_id"] = scope.subject_id
        try:
            job = await self.jobs.create_job(document)
        except Exception:
            await resolved.aclose()
            raise
        if job.get("_was_created") is False:
            await resolved.aclose()
            return _image_job_projection(job)

        request = ImageGenerationRequest(
            usage=scope.usage,
            slot_values={
                **dict(plan.slot_values),
                "seed": chosen_seed,
            },
            required_slots=plan.required_slots,
        )
        try:
            try:
                handle = await resolved.provider.submit(request)
            except ImageProviderError as error:
                failed = await self._record_submit_failure(
                    owner_id=scope.owner_id,
                    novel_id=scope.novel_id,
                    card_id=scope.subject_id,
                    job_id=_job_id(job),
                    failure=error.failure,
                )
                return _without_portrait_fields(failed)
            except Exception:
                failed = await self._record_submit_failure(
                    owner_id=scope.owner_id,
                    novel_id=scope.novel_id,
                    card_id=scope.subject_id,
                    job_id=_job_id(job),
                    failure=ImageFailure(
                        code="execution_failed",
                        message="图像后端提交任务时发生异常",
                        action="检查图像后端与 workflow 配置后，由用户重新发起",
                    ),
                )
                return _without_portrait_fields(failed)
            attached = await self._attach_submitted_handle(
                owner_id=scope.owner_id,
                novel_id=scope.novel_id,
                card_id=scope.subject_id,
                job_id=_job_id(job),
                handle=handle,
                estimated_seconds=estimated_seconds,
                provider=resolved.provider,
            )
            return _without_portrait_fields(attached)
        finally:
            await resolved.aclose()

    async def start(
        self,
        *,
        owner_id: str,
        novel_id: str,
        card_id: str,
        prompt: IllustrationPromptResult,
        seed: int | None = None,
        provider_alias: str | None = None,
        confirm_anchor_reset: bool = False,
    ) -> PortraitJobProjection:
        owner_id = _canonical_object_id(owner_id, field="owner_id")
        novel_id = _canonical_object_id(novel_id, field="novel_id")
        card_id = _canonical_object_id(card_id, field="card_id")
        if not prompt.appearance:
            raise ValueError("appearance must not be empty for a character portrait")
        if seed is not None and (
            type(seed) is not int or not 0 <= seed <= (2**64 - 1)
        ):
            raise ValueError("seed must be an integer from 0 through 2^64-1")
        if self.anchors is None:
            raise PortraitConfigurationError(
                "Character portrait generation requires an appearance-anchor gateway"
            )
        active = await self.jobs.find_active_owned_job(
            owner_id=owner_id,
            novel_id=novel_id,
            card_id=card_id,
        )
        if active is not None:
            return _projection(active)
        pending_cleanup = await self.jobs.find_pending_cleanup_owned_job(
            owner_id=owner_id,
            novel_id=novel_id,
            card_id=card_id,
        )
        if pending_cleanup is not None:
            return _projection(pending_cleanup)
        current_anchor = await self.anchors.get_anchor(
            novel_id=novel_id,
            card_id=card_id,
        )
        if current_anchor is not None and not confirm_anchor_reset:
            raise PortraitAnchorResetRequired(
                APPEARANCE_ANCHOR_RESET_WARNING
            )
        positive_prompt = _positive_prompt(prompt)
        result = await self.start_job(
            scope=ImageJobScope(
                owner_id=owner_id,
                novel_id=novel_id,
                usage="character_portrait",
                subject_id=card_id,
            ),
            plan=ImageJobPlan(
                prompt=prompt,
                seed=seed,
                slot_values={
                    "positive_prompt": positive_prompt,
                    "negative_prompt": prompt.negative,
                    "batch_size": 1,
                },
                required_slots=frozenset({"positive_prompt", "seed"}),
                persisted_fields={
                    "final_prompt": positive_prompt,
                    "negative_prompt": prompt.negative,
                    "descriptor": prompt.appearance,
                    "anchor_before": current_anchor,
                    "confirm_anchor_reset": confirm_anchor_reset,
                },
                idempotency_context={
                    "anchor_before": self._canonical_anchor(
                        current_anchor
                    ),
                    "confirm_anchor_reset": confirm_anchor_reset,
                },
            ),
            provider_alias=provider_alias,
        )
        persisted = await self.jobs.get_owned_job(
            owner_id=owner_id,
            novel_id=novel_id,
            card_id=card_id,
            job_id=result.job_id,
        )
        if persisted is None:
            raise PortraitJobNotFoundError("Image job not found")
        return _projection(persisted)

    async def poll_job(
        self,
        *,
        scope: ImageJobScope,
        job_id: str,
    ) -> ImageJobProjection:
        scope = scope.canonical()
        if scope.usage != self.usage:
            raise ValueError("Image job scope usage does not match the service")
        result = await self.poll(
            owner_id=scope.owner_id,
            novel_id=scope.novel_id,
            card_id=scope.subject_id,
            job_id=job_id,
        )
        return _without_portrait_fields(result)

    async def cancel_job(
        self,
        *,
        scope: ImageJobScope,
        job_id: str,
    ) -> ImageJobProjection:
        scope = scope.canonical()
        if scope.usage != self.usage:
            raise ValueError("Image job scope usage does not match the service")
        result = await self.cancel(
            owner_id=scope.owner_id,
            novel_id=scope.novel_id,
            card_id=scope.subject_id,
            job_id=job_id,
        )
        return _without_portrait_fields(result)

    async def poll(
        self,
        *,
        owner_id: str,
        novel_id: str,
        card_id: str,
        job_id: str,
    ) -> PortraitJobProjection:
        owner_id = _canonical_object_id(owner_id, field="owner_id")
        novel_id = _canonical_object_id(novel_id, field="novel_id")
        card_id = _canonical_object_id(card_id, field="card_id")
        job_id = _canonical_object_id(job_id, field="job_id")
        job = await self.jobs.get_owned_job(
            owner_id=owner_id,
            novel_id=novel_id,
            card_id=card_id,
            job_id=job_id,
        )
        if job is None:
            raise PortraitJobNotFoundError("Image job not found")
        if bool(job.get("is_terminal")):
            if bool(job.get("late_reconciliation")) and bool(
                job.get("cleanup_pending")
            ):
                return await self._resume_terminal_late_reconciliation(
                    owner_id=owner_id,
                    novel_id=novel_id,
                    card_id=card_id,
                    job_id=job_id,
                    job=job,
                )
            return _projection(job)
        if bool(job.get("cleanup_pending")):
            cleanup_phase = str(job.get("late_cleanup_phase") or "")
            if cleanup_phase == "await_handle":
                return _projection(job)
            if cleanup_phase == "cancel":
                return await self._execute_late_cleanup_cancellation(
                    owner_id=owner_id,
                    novel_id=novel_id,
                    card_id=card_id,
                    job_id=job_id,
                    job=job,
                )
        if str(job.get("status") or "") == "cancelling":
            return await self.cancel(
                owner_id=owner_id,
                novel_id=novel_id,
                card_id=card_id,
                job_id=job_id,
            )
        if str(job.get("status") or "") == "finalizing":
            return await self._resume_finalizing(
                owner_id=owner_id,
                novel_id=novel_id,
                card_id=card_id,
                job_id=job_id,
                job=job,
            )
        owns_asset_lease = False
        if str(job.get("status") or "") == "storing_asset":
            claimed_at = float(job.get("asset_claimed_at_epoch") or 0)
            if (
                claimed_at > 0
                and self._now_epoch() - claimed_at
                < ASSET_STORAGE_LEASE_SECONDS
            ):
                return _projection(job)
            takeover, won_takeover = await self._cas_or_winner(
                owner_id=owner_id,
                novel_id=novel_id,
                card_id=card_id,
                job_id=job_id,
                expected_revision=int(job.get("job_revision") or 0),
                fields={
                    "status": "storing_asset",
                    "completed_images": max(
                        1,
                        int(job.get("completed_images") or 0),
                    ),
                    "asset_claim_token": secrets.token_hex(16),
                    "asset_claimed_at_epoch": self._now_epoch(),
                },
            )
            if not won_takeover:
                return _projection(takeover)
            job = takeover
            owns_asset_lease = True
        if (
            str(job.get("status") or "") == "submitting"
            and not isinstance(job.get("handle"), dict)
        ):
            elapsed = max(
                0,
                int(
                    self._now_epoch()
                    - float(job.get("started_at_epoch") or 0)
                ),
            )
            timeout_seconds = max(
                1,
                int(
                    job.get("submit_timeout_seconds")
                    or job.get("estimated_seconds")
                    or 600
                ),
            )
            fields: dict[str, Any] = {"elapsed_seconds": elapsed}
            if elapsed < timeout_seconds:
                return _projection(
                    {
                        **job,
                        "elapsed_seconds": elapsed,
                    }
                )
            failure = ImageFailure(
                code="job_lost",
                message="图像任务提交后没有留下可继续查询的句柄",
                action="确认 ComfyUI 中没有对应任务后，由用户手工重新发起；系统不会自动提交",
            )
            fields.update(
                {
                    "status": "late_cleanup_pending",
                    "is_terminal": False,
                    "cleanup_pending": True,
                    "late_cleanup_phase": "await_handle",
                    "late_cleanup_attempts": 0,
                    "submit_count": max(
                        1,
                        int(job.get("submit_count") or 0),
                    ),
                    "failure": failure.model_dump(
                        mode="json",
                        exclude_computed_fields=True,
                    ),
                }
            )
            updated, _ = await self._cas_or_winner(
                owner_id=owner_id,
                novel_id=novel_id,
                card_id=card_id,
                job_id=job_id,
                expected_revision=int(job.get("job_revision") or 0),
                fields=fields,
            )
            return _projection(updated)
        raw_handle = job.get("handle")
        if not isinstance(raw_handle, dict):
            raise PortraitConfigurationError(
                "Image job has no persisted provider handle"
            )
        handle = ImageJobHandle.model_validate(raw_handle)
        resolved = await self.provider_resolver.resolve(
            usage=self.usage,
            provider_alias=handle.provider_alias,
        )
        try:
            result: ImagePollResult = await resolved.provider.poll(handle)
        finally:
            await resolved.aclose()
        elapsed_seconds = max(
            0,
            int(
                self._now_epoch()
                - float(job.get("started_at_epoch") or self._now_epoch())
            ),
        )
        base_estimate = max(
            0,
            int(
                job.get("unit_estimated_seconds")
                or job.get("estimated_seconds")
                or handle.timeout_seconds
            ),
        )
        queue_multiplier = (
            max(1, int(result.queue_position))
            if result.status == "queued" and result.queue_position is not None
            else 1
        )
        was_cleanup_pending = bool(job.get("cleanup_pending"))
        fields: dict[str, Any] = {
            "status": result.status,
            "is_terminal": result.is_terminal,
            "queue_position": result.queue_position,
            "estimated_seconds": base_estimate * queue_multiplier,
            "elapsed_seconds": elapsed_seconds,
            "ignored_slots": list(result.ignored_slots or handle.ignored_slots),
            "failure": (
                result.failure.model_dump(
                    mode="json",
                    exclude_computed_fields=True,
                )
                if result.failure is not None
                else None
            ),
        }
        if bool(job.get("cleanup_pending")):
            fields.update(
                {
                    "status": (
                        "late_cleanup_pending"
                        if not result.is_terminal
                        else result.status
                    ),
                    "cleanup_pending": not result.is_terminal,
                    "late_cleanup_phase": (
                        "poll" if not result.is_terminal else None
                    ),
                }
            )
        if owns_asset_lease:
            fields.update(
                {
                    "asset_claim_token": None,
                    "asset_claimed_at_epoch": None,
                }
            )
        if result.status == "succeeded":
            if len(result.artifacts) != 1:
                failure = ImageFailure(
                    code="execution_failed",
                    message="单张图像任务没有返回恰好一张图片",
                    action="检查 workflow 产物节点，确保当前图像用途只输出一张图片",
                    details={"artifact_count": len(result.artifacts)},
                )
                fields.update(
                    {
                        "status": "failed",
                        "is_terminal": True,
                        "completed_images": len(result.artifacts),
                        "failure": failure.model_dump(
                            mode="json",
                            exclude_computed_fields=True,
                        ),
                    }
                )
            else:
                if not owns_asset_lease:
                    claimed, won_claim = await self._cas_or_winner(
                        owner_id=owner_id,
                        novel_id=novel_id,
                        card_id=card_id,
                        job_id=job_id,
                        expected_revision=int(job.get("job_revision") or 0),
                        fields={
                            "status": "storing_asset",
                            "is_terminal": False,
                            "cleanup_pending": False,
                            "late_cleanup_phase": None,
                            "queue_position": None,
                            "elapsed_seconds": elapsed_seconds,
                            "completed_images": 1,
                            "failure": None,
                            "asset_claim_token": secrets.token_hex(16),
                            "asset_claimed_at_epoch": self._now_epoch(),
                        },
                    )
                    if not won_claim:
                        return _projection(claimed)
                    job = claimed
                try:
                    asset, pending_completion = (
                        await self._consume_successful_image(
                            owner_id=owner_id,
                            novel_id=novel_id,
                            card_id=card_id,
                            job=job,
                            handle=handle,
                            result=result,
                        )
                    )
                except Exception:
                    failure = ImageFailure(
                        code="execution_failed",
                        message="图片已生成，但写入受管素材失败",
                        action="检查素材目录权限和剩余空间；已生成张数与消耗已记账，修复后由用户决定是否重新发起",
                    )
                    recovering, _ = await self._cas_or_winner(
                        owner_id=owner_id,
                        novel_id=novel_id,
                        card_id=card_id,
                        job_id=job_id,
                        expected_revision=int(
                            job.get("job_revision") or 0
                        ),
                        fields={
                            "status": "failed",
                            "is_terminal": True,
                            "cleanup_pending": False,
                            "late_cleanup_phase": None,
                            "completed_images": 1,
                            "asset_claim_token": None,
                            "asset_claimed_at_epoch": None,
                            "failure": failure.model_dump(
                                mode="json",
                                exclude_computed_fields=True,
                            ),
                        },
                    )
                    return _projection(recovering)
                if (
                    was_cleanup_pending
                    and self.completion_adapter is not None
                ):
                    accounted, _ = await self._cas_or_winner(
                        owner_id=owner_id,
                        novel_id=novel_id,
                        card_id=card_id,
                        job_id=job_id,
                        expected_revision=int(
                            job.get("job_revision") or 0
                        ),
                        fields={
                            "status": "succeeded",
                            "is_terminal": True,
                            "cleanup_pending": False,
                            "late_cleanup_phase": None,
                            "queue_position": None,
                            "asset": asset,
                            "completed_images": 1,
                            "elapsed_seconds": elapsed_seconds,
                            "selected_as_current": False,
                            "asset_claim_token": None,
                            "asset_claimed_at_epoch": None,
                            "failure": None,
                        },
                    )
                    return _projection(accounted)
                pending_field = (
                    "pending_completion"
                    if self.completion_adapter is not None
                    else "pending_anchor"
                )
                finalizing, won_finalizing = await self._cas_or_winner(
                    owner_id=owner_id,
                    novel_id=novel_id,
                    card_id=card_id,
                    job_id=job_id,
                    expected_revision=int(job.get("job_revision") or 0),
                    fields={
                        "status": "finalizing",
                        "is_terminal": False,
                        "cleanup_pending": False,
                        "late_cleanup_phase": None,
                        "asset": asset,
                        "completed_images": 1,
                        "elapsed_seconds": elapsed_seconds,
                        pending_field: pending_completion,
                        "asset_claim_token": None,
                        "asset_claimed_at_epoch": None,
                    },
                )
                if not won_finalizing:
                    return _projection(finalizing)
                return await self._resume_finalizing(
                    owner_id=owner_id,
                    novel_id=novel_id,
                    card_id=card_id,
                    job_id=job_id,
                    job=finalizing,
                )
        updated, _ = await self._cas_or_winner(
            owner_id=owner_id,
            novel_id=novel_id,
            card_id=card_id,
            job_id=job_id,
            expected_revision=int(job.get("job_revision") or 0),
            fields=fields,
        )
        return _projection(updated)
    async def cancel(
        self,
        *,
        owner_id: str,
        novel_id: str,
        card_id: str,
        job_id: str,
    ) -> PortraitJobProjection:
        owner_id = _canonical_object_id(owner_id, field="owner_id")
        novel_id = _canonical_object_id(novel_id, field="novel_id")
        card_id = _canonical_object_id(card_id, field="card_id")
        job_id = _canonical_object_id(job_id, field="job_id")
        while True:
            job = await self.jobs.get_owned_job(
                owner_id=owner_id,
                novel_id=novel_id,
                card_id=card_id,
                job_id=job_id,
            )
            if job is None:
                raise PortraitJobNotFoundError("Image job not found")
            if bool(job.get("is_terminal")):
                if bool(job.get("late_reconciliation")) and bool(
                    job.get("cleanup_pending")
                ):
                    return await self._resume_terminal_late_reconciliation(
                        owner_id=owner_id,
                        novel_id=novel_id,
                        card_id=card_id,
                        job_id=job_id,
                        job=job,
                        explicit=True,
                    )
                return _projection(job)
            status = str(job.get("status") or "")
            if bool(job.get("cleanup_pending")):
                cleanup_phase = str(job.get("late_cleanup_phase") or "")
                if cleanup_phase == "await_handle":
                    failure = ImageFailure(
                        code="job_lost",
                        message="图像任务提交后没有留下可继续查询的句柄",
                        action="确认 ComfyUI 中没有对应任务后，由用户手工重新发起；系统不会自动提交",
                    )
                    abandoned, _ = await self._cas_or_winner(
                        owner_id=owner_id,
                        novel_id=novel_id,
                        card_id=card_id,
                        job_id=job_id,
                        expected_revision=int(
                            job.get("job_revision") or 0
                        ),
                        fields={
                            "status": "failed",
                            "is_terminal": True,
                            "cleanup_pending": False,
                            "late_cleanup_phase": None,
                            "failure": failure.model_dump(
                                mode="json",
                                exclude_computed_fields=True,
                            ),
                        },
                    )
                    return _projection(abandoned)
                if cleanup_phase == "cancel":
                    return await self._execute_late_cleanup_cancellation(
                        owner_id=owner_id,
                        novel_id=novel_id,
                        card_id=card_id,
                        job_id=job_id,
                        job=job,
                        explicit=True,
                    )
                if cleanup_phase == "poll":
                    return await self.poll(
                        owner_id=owner_id,
                        novel_id=novel_id,
                        card_id=card_id,
                        job_id=job_id,
                    )
            if status == "finalizing":
                return await self._resume_finalizing(
                    owner_id=owner_id,
                    novel_id=novel_id,
                    card_id=card_id,
                    job_id=job_id,
                    job=job,
                )
            if status == "storing_asset":
                return _projection(job)
            raw_handle = job.get("handle")
            if isinstance(raw_handle, dict):
                break
            if status != "submitting" or bool(job.get("cancel_requested")):
                return _projection(job)
            intent, won_intent = await self._cas_or_winner(
                owner_id=owner_id,
                novel_id=novel_id,
                card_id=card_id,
                job_id=job_id,
                expected_revision=int(job.get("job_revision") or 0),
                fields={"cancel_requested": True},
            )
            if won_intent:
                return _projection(intent)

        return await self._execute_nonterminal_cancellation(
            owner_id=owner_id,
            novel_id=novel_id,
            card_id=card_id,
            job_id=job_id,
            job=job,
        )
