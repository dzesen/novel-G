"""Thin character-portrait adapter over the shared single-image job."""

from __future__ import annotations

from datetime import datetime
import secrets
import time
from typing import Any, Callable, Protocol

from backend.db.errors import NotFoundError
from backend.db.repositories.chapter_repository import chapter_repo
from backend.db.repositories.image_asset_repository import image_asset_repo
from backend.db.repositories.image_batch_repository import image_batch_repo
from backend.db.repositories.image_job_repository import image_job_repo
from backend.db.utils import get_utc_now, to_object_id
from backend.services.image.managed_assets import (
    ImagePollAssetConsumer,
)
from backend.services.image.single_image_job_service import (
    AppearanceAnchorDependencyProjection,
    AppearanceAnchorGatewayProtocol,
    CharacterPortraitStateProjection,
    ConfiguredImageProviderResolver,
    ImageAssetMetadataRepositoryProtocol,
    ImageJobRepositoryProtocol,
    ImageJobSubmissionFence,
    ImageJobSubmissionGuardProtocol,
    ImageProviderResolverProtocol,
    ImageProviderSnapshot,
    ManagedAssetReaderProtocol,
    PortraitAnchorResetRequired,
    PortraitBatchItemConflict,
    PortraitAssetProjection,
    PortraitConfigurationError,
    PortraitJobNotFoundError,
    PortraitJobProjection,
    PortraitProviderProjection,
    ReferenceCardAppearanceAnchorGateway,
    ResolvedImageProvider,
    SingleImageJobService,
)
from backend.services.llm.agent_orchestrator import IllustrationPromptResult
from backend.services.novel.appearance_anchor import (
    AppearanceAnchorConflictError,
)


class ChapterLookupProtocol(Protocol):
    async def get_chapter_by_id(
        self,
        chapter_id: str,
        *,
        include_deleted: bool = False,
    ) -> dict[str, Any]: ...


class PortraitBatchBindingRepositoryProtocol(Protocol):
    async def bind_starting_portrait_job(
        self,
        *,
        owner_id: str,
        novel_id: str,
        batch_id: str,
        card_id: str,
        start_claim_token: str,
        job_id: str,
    ) -> bool: ...


class RepositoryPortraitBatchSubmissionGuard:
    def __init__(self, batches: PortraitBatchBindingRepositoryProtocol) -> None:
        self.batches = batches

    async def bind_job(
        self,
        *,
        owner_id: str,
        novel_id: str,
        card_id: str,
        job_id: str,
        fence: ImageJobSubmissionFence,
    ) -> bool:
        return await self.batches.bind_starting_portrait_job(
            owner_id=owner_id,
            novel_id=novel_id,
            batch_id=fence.batch_id,
            card_id=card_id,
            start_claim_token=fence.start_claim_token,
            job_id=job_id,
        )


class AppearanceAnchorInUseError(RuntimeError):
    def __init__(
        self,
        *,
        dependencies: tuple[AppearanceAnchorDependencyProjection, ...],
        dependency_total: int,
    ) -> None:
        super().__init__("外观锚点仍被场景插图引用，暂时不能解绑")
        self.dependencies = dependencies
        self.dependency_total = dependency_total


class AppearanceAnchorBusyError(RuntimeError):
    pass


class CharacterPortraitService:
    """Supply portrait validation/state while delegating the durable job."""

    def __init__(
        self,
        *,
        jobs: ImageJobRepositoryProtocol,
        anchors: AppearanceAnchorGatewayProtocol,
        provider_resolver: ImageProviderResolverProtocol,
        submission_guard: ImageJobSubmissionGuardProtocol | None = None,
        asset_consumer: ImagePollAssetConsumer | None = None,
        asset_reader: ManagedAssetReaderProtocol | None = None,
        asset_repository: ImageAssetMetadataRepositoryProtocol | None = None,
        chapters: ChapterLookupProtocol | None = None,
        now: Callable[[], datetime],
        now_epoch: Callable[[], float] = time.time,
        seed_factory: Callable[[], int] = lambda: secrets.randbits(64),
    ) -> None:
        self._assets = asset_repository or image_asset_repo
        self._anchors = anchors
        self._job_repository = jobs
        self._chapters = chapters
        self._jobs = SingleImageJobService(
            jobs=jobs,
            anchors=anchors,
            provider_resolver=provider_resolver,
            submission_guard=submission_guard,
            usage="character_portrait",
            asset_consumer=asset_consumer,
            asset_reader=asset_reader,
            asset_repository=asset_repository,
            now=now,
            now_epoch=now_epoch,
            seed_factory=seed_factory,
        )

    async def get_state(
        self,
        *,
        owner_id: str,
        novel_id: str,
        card_id: str,
        provider_alias: str | None = None,
    ) -> CharacterPortraitStateProjection:
        state = await self._jobs.get_state(
            owner_id=owner_id,
            novel_id=novel_id,
            card_id=card_id,
            provider_alias=provider_alias,
        )
        history = await self._assets.list_owned_subject(
            owner_id=to_object_id(owner_id), novel_id=to_object_id(novel_id),
            subject_kind="character_portrait", subject_id=card_id,
        )
        assets = tuple(
            PortraitAssetProjection(
                asset_id=str(item["_id"]), content_hash=str(item.get("content_hash") or ""),
                mime=str(item.get("mime") or ""), width=int(item.get("width") or 0),
                height=int(item.get("height") or 0), state="available",
                content_url=f"/api/image-assets/{item['_id']}/content",
            )
            for item in history if item.get("_id") is not None
        )
        state = state.model_copy(update={"assets": assets})
        if state.anchor is None:
            return state
        dependency_total, dependencies = await self._anchor_dependencies(
            owner_id=owner_id,
            novel_id=novel_id,
            card_id=card_id,
        )
        return state.model_copy(
            update={
                "anchor_dependencies": dependencies,
                "anchor_dependency_total": dependency_total,
            }
        )

    async def _anchor_dependencies(
        self,
        *,
        owner_id: str,
        novel_id: str,
        card_id: str,
    ) -> tuple[int, tuple[AppearanceAnchorDependencyProjection, ...]]:
        total, documents = await self._job_repository.list_anchor_dependencies(
            owner_id=owner_id,
            novel_id=novel_id,
            card_id=card_id,
            limit=5,
        )
        dependencies: list[AppearanceAnchorDependencyProjection] = []
        for document in documents:
            chapter_id = str(document.get("subject_id") or "")
            chapter_title = ""
            chapter_order: int | None = None
            if self._chapters is not None and chapter_id:
                try:
                    chapter = await self._chapters.get_chapter_by_id(
                        chapter_id,
                        include_deleted=True,
                    )
                except NotFoundError:
                    chapter = None
                if chapter is not None:
                    chapter_title = str(chapter.get("title") or "").strip()
                    raw_order = chapter.get("order_index")
                    chapter_order = (
                        int(raw_order) if raw_order is not None else None
                    )
            dependencies.append(
                AppearanceAnchorDependencyProjection(
                    job_id=str(document.get("_id") or ""),
                    chapter_id=chapter_id,
                    chapter_title=chapter_title,
                    chapter_order=chapter_order,
                    status=str(document.get("status") or "unknown"),
                )
            )
        return total, tuple(dependencies)

    async def detach_anchor(
        self,
        *,
        owner_id: str,
        novel_id: str,
        card_id: str,
        expected_reference_asset: str,
    ) -> CharacterPortraitStateProjection:
        current = await self._anchors.get_anchor(
            novel_id=novel_id,
            card_id=card_id,
        )
        if current is None:
            return await self.get_state(
                owner_id=owner_id,
                novel_id=novel_id,
                card_id=card_id,
            )
        if current.get("reference_asset") != expected_reference_asset:
            raise AppearanceAnchorConflictError(
                "外观锚点已变化，请刷新角色卡后再决定是否解绑"
            )
        state = await self.get_state(
            owner_id=owner_id,
            novel_id=novel_id,
            card_id=card_id,
        )
        if state.active_job is not None or state.cleanup_job is not None:
            raise AppearanceAnchorBusyError(
                "当前立绘作业尚未结束，请等待作业完成后再解绑"
            )
        if state.anchor_dependency_total:
            raise AppearanceAnchorInUseError(
                dependencies=state.anchor_dependencies,
                dependency_total=state.anchor_dependency_total,
            )
        await self._anchors.clear_anchor(
            novel_id=novel_id,
            card_id=card_id,
            expected_previous=current,
        )
        return await self.get_state(
            owner_id=owner_id,
            novel_id=novel_id,
            card_id=card_id,
        )

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
        preserve_anchor: bool = False,
        expected_anchor_reference_asset: str | None = None,
        portrait_batch_id: str | None = None,
        submission_fence: ImageJobSubmissionFence | None = None,
    ) -> PortraitJobProjection:
        return await self._jobs.start(
            owner_id=owner_id,
            novel_id=novel_id,
            card_id=card_id,
            prompt=prompt,
            seed=seed,
            provider_alias=provider_alias,
            confirm_anchor_reset=confirm_anchor_reset,
            preserve_anchor=preserve_anchor,
            expected_anchor_reference_asset=expected_anchor_reference_asset,
            portrait_batch_id=portrait_batch_id,
            submission_fence=submission_fence,
        )

    async def poll(
        self,
        *,
        owner_id: str,
        novel_id: str,
        card_id: str,
        job_id: str,
    ) -> PortraitJobProjection:
        return await self._jobs.poll(
            owner_id=owner_id,
            novel_id=novel_id,
            card_id=card_id,
            job_id=job_id,
        )

    async def cancel(
        self,
        *,
        owner_id: str,
        novel_id: str,
        card_id: str,
        job_id: str,
    ) -> PortraitJobProjection:
        return await self._jobs.cancel(
            owner_id=owner_id,
            novel_id=novel_id,
            card_id=card_id,
            job_id=job_id,
        )


character_portrait_service = CharacterPortraitService(
    jobs=image_job_repo,
    anchors=ReferenceCardAppearanceAnchorGateway(),
    provider_resolver=ConfiguredImageProviderResolver(),
    submission_guard=RepositoryPortraitBatchSubmissionGuard(
        image_batch_repo
    ),
    chapters=chapter_repo,
    now=get_utc_now,
)


__all__ = [
    "AppearanceAnchorBusyError",
    "AppearanceAnchorInUseError",
    "CharacterPortraitService",
    "CharacterPortraitStateProjection",
    "ConfiguredImageProviderResolver",
    "ImageJobRepositoryProtocol",
    "ImageProviderResolverProtocol",
    "ImageProviderSnapshot",
    "PortraitAnchorResetRequired",
    "PortraitBatchItemConflict",
    "PortraitAssetProjection",
    "PortraitConfigurationError",
    "PortraitJobNotFoundError",
    "PortraitJobProjection",
    "PortraitProviderProjection",
    "ReferenceCardAppearanceAnchorGateway",
    "ResolvedImageProvider",
    "character_portrait_service",
]
