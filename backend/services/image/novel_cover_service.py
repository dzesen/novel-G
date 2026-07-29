"""User-triggered novel-cover generation over the shared single-image job."""

from __future__ import annotations

import secrets
import time
from datetime import datetime
from typing import Any, Callable, Protocol

from pydantic import BaseModel, ConfigDict
from backend.db.repositories.image_job_repository import image_job_repo
from backend.db.repositories.image_asset_repository import (
    ImageAssetRepository,
    image_asset_repo,
)
from backend.db.repositories.novel_repository import (
    NovelRepository,
    novel_repo,
)
from backend.db.utils import get_utc_now, to_object_id
from backend.services.image.single_image_job_service import (
    ConfiguredImageProviderResolver,
    ImageCompletionError,
    ImageJobCompletionAdapter,
    ImageJobPlan,
    ImageJobProjection,
    ImageJobScope,
    ImageProviderResolverProtocol,
    PortraitAssetProjection,
    PortraitProviderProjection,
    ScopedImageJobRepository,
    SingleImageJobService,
    _positive_prompt,
)
from backend.services.image.contracts import ImageFailure
from backend.services.image.managed_assets import (
    ImageAssetIntegrityError,
    ImageAssetNotFoundError,
    ImagePollAssetConsumer,
    ManagedImageAssetService,
)
from backend.services.llm.agent_orchestrator import IllustrationPromptResult


class CoverReferenceGatewayProtocol(Protocol):
    async def get_current(
        self,
        *,
        owner_id: str,
        novel_id: str,
    ) -> str | None: ...

    async def compare_and_set_current(
        self,
        *,
        owner_id: str,
        novel_id: str,
        expected_asset_id: str | None,
        asset_id: str | None,
    ) -> bool: ...


class CoverAssetNotFoundError(LookupError):
    """The requested asset is not an owned cover for this novel."""


class CoverReferenceConflictError(RuntimeError):
    """The explicit cover changed while a manual selection was applied."""


class NovelCoverStateProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    current_asset: PortraitAssetProjection | None = None
    assets: tuple[PortraitAssetProjection, ...] = ()
    active_job: ImageJobProjection | None = None
    cleanup_job: ImageJobProjection | None = None
    provider: PortraitProviderProjection
    warnings: tuple[str, ...] = ()


class NovelCoverReferenceGateway:
    def __init__(
        self,
        *,
        novels: NovelRepository,
        assets: ImageAssetRepository,
    ) -> None:
        self.novels = novels
        self.assets = assets

    async def get_current(
        self,
        *,
        owner_id: str,
        novel_id: str,
    ) -> str | None:
        return await self.novels.get_owned_cover_asset_id(
            owner_id=owner_id,
            novel_id=novel_id,
        )

    async def compare_and_set_current(
        self,
        *,
        owner_id: str,
        novel_id: str,
        expected_asset_id: str | None,
        asset_id: str | None,
    ) -> bool:
        if asset_id is not None:
            document = await self.assets.get_owned_by_id(
                owner_id=to_object_id(owner_id),
                asset_id=to_object_id(asset_id),
            )
            if (
                document is None
                or str(document.get("novel_id") or "") != novel_id
                or str(document.get("subject_kind") or "") != "cover"
                or str(document.get("subject_id") or "") != novel_id
            ):
                raise CoverAssetNotFoundError("Cover asset not found")
        return await self.novels.compare_and_set_cover_asset_id(
            owner_id=owner_id,
            novel_id=novel_id,
            expected_asset_id=expected_asset_id,
            asset_id=asset_id,
        )


class NovelCoverCompletion(ImageJobCompletionAdapter):
    def __init__(self, references: CoverReferenceGatewayProtocol) -> None:
        self.references = references

    async def prepare(
        self,
        *,
        scope: ImageJobScope,
        job: dict[str, Any],
        handle,
        asset: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "asset_id": str(asset["asset_id"]),
            "expected_asset_id": job.get("cover_asset_id_before"),
        }

    async def finalize(
        self,
        *,
        scope: ImageJobScope,
        job: dict[str, Any],
        pending: dict[str, Any],
    ) -> dict[str, Any]:
        asset_id = str(pending.get("asset_id") or "")
        if not asset_id:
            raise ImageCompletionError(
                ImageFailure(
                    code="execution_failed",
                    message="封面素材已保存，但缺少素材引用",
                    action="从封面历史中手工选择该素材；不要重新提交图像任务",
                )
            )
        expected = pending.get("expected_asset_id")
        selected = await self.references.compare_and_set_current(
            owner_id=scope.owner_id,
            novel_id=scope.novel_id,
            expected_asset_id=(str(expected) if expected else None),
            asset_id=asset_id,
        )
        if selected:
            return {}
        current = await self.references.get_current(
            owner_id=scope.owner_id,
            novel_id=scope.novel_id,
        )
        if current == asset_id:
            return {}
        raise ImageCompletionError(
            ImageFailure(
                code="execution_failed",
                message="封面已保存，但生成期间当前封面被其他操作切换",
                action="保留当前选择；如需使用新封面，请从封面历史中手工选择",
            )
        )


class NovelCoverService:
    def __init__(
        self,
        *,
        jobs: Any,
        cover_references: CoverReferenceGatewayProtocol,
        provider_resolver: ImageProviderResolverProtocol,
        asset_consumer: ImagePollAssetConsumer | None = None,
        asset_repository: ImageAssetRepository | None = None,
        asset_reader: Any | None = None,
        now: Callable[[], datetime],
        now_epoch: Callable[[], float] = time.time,
        seed_factory: Callable[[], int] = lambda: secrets.randbits(64),
    ) -> None:
        self.cover_references = cover_references
        self.asset_repository = asset_repository or image_asset_repo
        self.asset_reader = asset_reader or ManagedImageAssetService()
        self.job_service = SingleImageJobService(
            jobs=ScopedImageJobRepository(jobs, usage="cover"),
            anchors=None,
            provider_resolver=provider_resolver,
            usage="cover",
            completion_adapter=NovelCoverCompletion(cover_references),
            asset_consumer=asset_consumer,
            now=now,
            now_epoch=now_epoch,
            seed_factory=seed_factory,
        )

    async def _project_asset(
        self,
        *,
        owner_id: str,
        document: dict[str, Any],
    ) -> PortraitAssetProjection:
        asset_id = str(document.get("_id") or document.get("asset_id") or "")
        state = "available"
        try:
            await self.asset_reader.read_owned_asset(
                owner_id=owner_id,
                asset_id=asset_id,
            )
        except (ImageAssetNotFoundError, ImageAssetIntegrityError):
            state = "missing"
        return PortraitAssetProjection(
            asset_id=asset_id,
            content_hash=str(document.get("content_hash") or ""),
            mime=str(document.get("mime") or ""),
            width=int(document.get("width") or 0),
            height=int(document.get("height") or 0),
            state=state,
            content_url=(
                f"/api/image-assets/{asset_id}/content"
                if state == "available"
                else None
            ),
        )

    async def get_state(
        self,
        *,
        owner_id: str,
        novel_id: str,
        provider_alias: str | None = None,
    ) -> NovelCoverStateProjection:
        scope = ImageJobScope(
            owner_id=owner_id,
            novel_id=novel_id,
            usage="cover",
            subject_id=novel_id,
        ).canonical()
        current_id = await self.cover_references.get_current(
            owner_id=scope.owner_id,
            novel_id=scope.novel_id,
        )
        documents = await self.asset_repository.list_owned_subject(
            owner_id=to_object_id(scope.owner_id),
            novel_id=to_object_id(scope.novel_id),
            subject_kind="cover",
            subject_id=scope.novel_id,
        )
        projected_assets = tuple(
            [
                await self._project_asset(
                    owner_id=scope.owner_id,
                    document=document,
                )
                for document in documents
            ]
        )
        by_id = {asset.asset_id: asset for asset in projected_assets}
        current_asset = by_id.get(str(current_id or ""))
        if current_id is not None and current_asset is None:
            # Preserve the explicit-reference missing state. Falling back to a
            # newer asset or cover_image would silently change the cover.
            current_asset = PortraitAssetProjection(
                asset_id=str(current_id),
                content_hash="",
                mime="",
                width=0,
                height=0,
                state="missing",
                content_url=None,
            )
        jobs = await self.job_service.get_job_state(
            scope=scope,
            provider_alias=provider_alias,
        )
        return NovelCoverStateProjection(
            current_asset=current_asset,
            assets=projected_assets,
            active_job=jobs.active_job,
            cleanup_job=jobs.cleanup_job,
            provider=jobs.provider,
            warnings=jobs.warnings,
        )

    async def start(
        self,
        *,
        owner_id: str,
        novel_id: str,
        prompt: IllustrationPromptResult,
        width: int = 512,
        height: int = 768,
        seed: int | None = None,
        provider_alias: str | None = None,
    ) -> ImageJobProjection:
        for name, value in (("width", width), ("height", height)):
            if type(value) is not int or not 64 <= value <= 4096:
                raise ValueError(f"{name} must be an integer from 64 through 4096")
        current = await self.cover_references.get_current(
            owner_id=owner_id,
            novel_id=novel_id,
        )
        positive_prompt = _positive_prompt(prompt)
        return await self.job_service.start_job(
            scope=ImageJobScope(
                owner_id=owner_id,
                novel_id=novel_id,
                usage="cover",
                subject_id=novel_id,
            ),
            plan=ImageJobPlan(
                prompt=prompt,
                seed=seed,
                slot_values={
                    "positive_prompt": positive_prompt,
                    "negative_prompt": prompt.negative,
                    "batch_size": 1,
                    "width": width,
                    "height": height,
                },
                required_slots=frozenset({"positive_prompt", "seed"}),
                persisted_fields={
                    "final_prompt": positive_prompt,
                    "negative_prompt": prompt.negative,
                    "cover_asset_id_before": current,
                    "requested_width": width,
                    "requested_height": height,
                },
                idempotency_context={
                    "cover_asset_id_before": current,
                    "width": width,
                    "height": height,
                },
            ),
            provider_alias=provider_alias,
        )

    async def poll(
        self,
        *,
        owner_id: str,
        novel_id: str,
        job_id: str,
    ) -> ImageJobProjection:
        return await self.job_service.poll_job(
            scope=ImageJobScope(
                owner_id=owner_id,
                novel_id=novel_id,
                usage="cover",
                subject_id=novel_id,
            ),
            job_id=job_id,
        )

    async def cancel(
        self,
        *,
        owner_id: str,
        novel_id: str,
        job_id: str,
    ) -> ImageJobProjection:
        return await self.job_service.cancel_job(
            scope=ImageJobScope(
                owner_id=owner_id,
                novel_id=novel_id,
                usage="cover",
                subject_id=novel_id,
            ),
            job_id=job_id,
        )

    async def select_current(
        self,
        *,
        owner_id: str,
        novel_id: str,
        asset_id: str | None,
    ) -> NovelCoverStateProjection:
        scope = ImageJobScope(
            owner_id=owner_id,
            novel_id=novel_id,
            usage="cover",
            subject_id=novel_id,
        ).canonical()
        if asset_id is not None:
            asset_id = str(to_object_id(asset_id))
        current = await self.cover_references.get_current(
            owner_id=scope.owner_id,
            novel_id=scope.novel_id,
        )
        if current != asset_id:
            selected = await self.cover_references.compare_and_set_current(
                owner_id=scope.owner_id,
                novel_id=scope.novel_id,
                expected_asset_id=current,
                asset_id=asset_id,
            )
            if not selected:
                raise CoverReferenceConflictError(
                    "The current cover changed; refresh the cover history and try again"
                )
        return await self.get_state(
            owner_id=scope.owner_id,
            novel_id=scope.novel_id,
        )


novel_cover_service = NovelCoverService(
    jobs=image_job_repo,
    cover_references=NovelCoverReferenceGateway(
        novels=novel_repo,
        assets=image_asset_repo,
    ),
    provider_resolver=ConfiguredImageProviderResolver(),
    now=get_utc_now,
)
