"""Chapter-scene illustration adapter over the shared single-image job."""

from __future__ import annotations

import secrets
import time
from datetime import datetime
from typing import Any, Callable, Protocol, Sequence

from pydantic import BaseModel, ConfigDict

from backend.db.errors import InvalidIdError, NotFoundError
from backend.db.repositories.chapter_repository import chapter_repo
from backend.db.repositories.image_asset_repository import (
    ImageAssetRepository,
    image_asset_repo,
)
from backend.db.repositories.image_job_repository import image_job_repo
from backend.db.utils import get_utc_now, to_object_id
from backend.services.image.contracts import ImageInputAsset
from backend.services.image.managed_assets import (
    ImageAssetIntegrityError,
    ImageAssetNotFoundError,
    ImagePollAssetConsumer,
    ManagedImageAssetService,
)
from backend.services.image.single_image_job_service import (
    ConfiguredImageProviderResolver,
    ImageJobCompletionAdapter,
    ImageJobPlan,
    ImageJobProjection,
    ImageJobScope,
    ImageJobStateProjection,
    ImageProviderResolverProtocol,
    PortraitAssetProjection,
    PortraitProviderProjection,
    ScopedImageJobRepository,
    SingleImageJobService,
    _positive_prompt,
    appearance_anchor_drift_labels,
)
from backend.services.llm.agent_orchestrator import IllustrationPromptResult
from backend.services.novel.appearance_anchor import AppearanceAnchorSchema
from backend.services.novel.reference_card_service import ReferenceCardService


MAX_SCENE_CHARACTER_CARD_IDS = 64
SCENE_ANCHOR_DESCRIPTOR_TOTAL_CHARACTER_LIMIT = 4_800


class SceneIllustrationConfigurationError(ValueError):
    """A scene request cannot satisfy its frozen-reference contract."""


class SceneCharacterProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    card_id: str
    name: str
    descriptor: str | None = None
    anchored: bool


class SceneIllustrationStateProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    characters: tuple[SceneCharacterProjection, ...] = ()
    assets: tuple[PortraitAssetProjection, ...] = ()
    active_job: ImageJobProjection | None = None
    cleanup_job: ImageJobProjection | None = None
    provider: PortraitProviderProjection
    warnings: tuple[str, ...] = ()


class ChapterGatewayProtocol(Protocol):
    async def get_chapter_by_id(self, chapter_id: str) -> dict[str, Any]: ...


class SceneCharacterGatewayProtocol(Protocol):
    async def get_character(
        self,
        *,
        novel_id: str,
        card_id: str,
    ) -> dict[str, Any]: ...


class SceneImageJobServiceProtocol(Protocol):
    async def get_job_state(
        self,
        *,
        scope: ImageJobScope,
        provider_alias: str | None = None,
    ) -> ImageJobStateProjection: ...

    async def start_job(
        self,
        *,
        scope: ImageJobScope,
        plan: ImageJobPlan,
        provider_alias: str | None = None,
    ) -> ImageJobProjection: ...

    async def poll_job(
        self,
        *,
        scope: ImageJobScope,
        job_id: str,
    ) -> ImageJobProjection: ...

    async def cancel_job(
        self,
        *,
        scope: ImageJobScope,
        job_id: str,
    ) -> ImageJobProjection: ...


class SceneCharacterGateway:
    async def get_character(
        self,
        *,
        novel_id: str,
        card_id: str,
    ) -> dict[str, Any]:
        return await ReferenceCardService.get(
            novel_id,
            "character",
            card_id,
        )


class _SceneIllustrationCompletion(ImageJobCompletionAdapter):
    """Finish storage without creating a portrait anchor or current pointer."""

    async def prepare(
        self,
        *,
        scope: ImageJobScope,
        job: dict[str, Any],
        handle,
        asset: dict[str, Any],
    ) -> dict[str, Any]:
        return {"asset_id": str(asset["asset_id"])}

    async def finalize(
        self,
        *,
        scope: ImageJobScope,
        job: dict[str, Any],
        pending: dict[str, Any],
    ) -> dict[str, Any]:
        return {"selected_as_current": None}


def _canonical_id(value: Any, *, field: str) -> str:
    label = {
        "owner_id": "用户标识",
        "novel_id": "小说标识",
        "chapter_id": "章节标识",
        "scene_character_card_ids": "入画角色标识",
        "reference_character_card_id": "参考角色标识",
        "outline.present_character_card_ids": (
            "已接受章细纲中的出场角色标识"
        ),
    }.get(field, "资源标识")
    if value is None:
        raise SceneIllustrationConfigurationError(f"缺少{label}")
    try:
        return str(to_object_id(value))
    except InvalidIdError as error:
        raise SceneIllustrationConfigurationError(
            f"{label}无效"
        ) from error


def _reference_filename(*, mime: str, content_hash: str) -> str:
    extensions = {
        "image/png": "png",
        "image/jpeg": "jpg",
        "image/gif": "gif",
        "image/webp": "webp",
    }
    extension = extensions.get(mime.lower())
    if extension is None:
        raise SceneIllustrationConfigurationError(
            "角色外观锚点引用的素材不是受支持的图片格式，请重新生成角色立绘"
        )
    return f"{content_hash}.{extension}"


class SceneIllustrationService:
    """Validate chapter/card evidence, then delegate the durable job."""

    def __init__(
        self,
        *,
        chapters: ChapterGatewayProtocol,
        cards: SceneCharacterGatewayProtocol,
        asset_repository: ImageAssetRepository,
        asset_reader: Any,
        job_service: SceneImageJobServiceProtocol | None = None,
        jobs: Any | None = None,
        provider_resolver: ImageProviderResolverProtocol | None = None,
        asset_consumer: ImagePollAssetConsumer | None = None,
        now: Callable[[], datetime] = get_utc_now,
        now_epoch: Callable[[], float] = time.time,
        seed_factory: Callable[[], int] = lambda: secrets.randbits(64),
    ) -> None:
        self.chapters = chapters
        self.cards = cards
        self.asset_repository = asset_repository
        self.asset_reader = asset_reader
        if job_service is not None:
            self.job_service = job_service
        else:
            if jobs is None or provider_resolver is None:
                raise ValueError(
                    "jobs and provider_resolver are required without job_service"
                )
            self.job_service = SingleImageJobService(
                jobs=ScopedImageJobRepository(
                    jobs,
                    usage="scene_illustration",
                ),
                anchors=None,
                provider_resolver=provider_resolver,
                usage="scene_illustration",
                completion_adapter=_SceneIllustrationCompletion(),
                asset_consumer=asset_consumer,
                now=now,
                now_epoch=now_epoch,
                seed_factory=seed_factory,
            )

    async def _chapter_and_declared_ids(
        self,
        *,
        novel_id: str,
        chapter_id: str,
    ) -> tuple[dict[str, Any], tuple[str, ...]]:
        canonical_novel_id = _canonical_id(novel_id, field="novel_id")
        canonical_chapter_id = _canonical_id(chapter_id, field="chapter_id")
        chapter = await self.chapters.get_chapter_by_id(
            canonical_chapter_id
        )
        if str(chapter.get("novel_id") or "") != canonical_novel_id:
            raise NotFoundError("Chapter was not found in this novel")
        outline = chapter.get("outline")
        if not isinstance(outline, dict):
            raise SceneIllustrationConfigurationError(
                "章节尚未有已确认的细纲，请先确认章细纲"
            )
        raw_ids = outline.get("present_character_card_ids") or []
        if not isinstance(raw_ids, (list, tuple)):
            raise SceneIllustrationConfigurationError(
                "章细纲的出场角色清单无效，请先修正章细纲"
            )
        if len(raw_ids) > MAX_SCENE_CHARACTER_CARD_IDS:
            raise SceneIllustrationConfigurationError(
                "章细纲的出场角色超过 64 个，请先缩小正式清单"
            )
        canonical_declared = [
            _canonical_id(value, field="outline.present_character_card_ids")
            for value in raw_ids
        ]
        declared = tuple(dict.fromkeys(canonical_declared))
        return chapter, declared

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
        chapter_id: str,
        provider_alias: str | None = None,
    ) -> SceneIllustrationStateProjection:
        owner_id = _canonical_id(owner_id, field="owner_id")
        novel_id = _canonical_id(novel_id, field="novel_id")
        chapter_id = _canonical_id(chapter_id, field="chapter_id")
        _chapter, declared = await self._chapter_and_declared_ids(
            novel_id=novel_id,
            chapter_id=chapter_id,
        )
        characters: list[SceneCharacterProjection] = []
        anchors_by_id: dict[str, AppearanceAnchorSchema] = {}
        for card_id in declared:
            card = await self.cards.get_character(
                novel_id=novel_id,
                card_id=card_id,
            )
            raw_anchor = card.get("appearance_anchor")
            descriptor = None
            if raw_anchor is not None:
                try:
                    anchor = AppearanceAnchorSchema.model_validate(raw_anchor)
                    descriptor = anchor.descriptor
                    anchors_by_id[card_id] = anchor
                except ValueError as error:
                    raise SceneIllustrationConfigurationError(
                        f"角色 {str(card.get('name') or card_id)} "
                        "的外观锚点无效，请重新生成该角色立绘"
                    ) from error
            characters.append(
                SceneCharacterProjection(
                    card_id=card_id,
                    name=str(card.get("name") or ""),
                    descriptor=descriptor,
                    anchored=descriptor is not None,
                )
            )
        documents = await self.asset_repository.list_owned_subject(
            owner_id=to_object_id(owner_id),
            novel_id=to_object_id(novel_id),
            subject_kind="scene_illustration",
            subject_id=chapter_id,
        )
        assets = tuple(
            [
                await self._project_asset(
                    owner_id=owner_id,
                    document=document,
                )
                for document in documents
            ]
        )
        scope = ImageJobScope(
            owner_id=owner_id,
            novel_id=novel_id,
            usage="scene_illustration",
            subject_id=chapter_id,
        )
        state_with_snapshot = getattr(
            self.job_service,
            "get_job_state_with_snapshot",
            None,
        )
        snapshot = None
        if callable(state_with_snapshot):
            jobs, snapshot = await state_with_snapshot(
                scope=scope,
                provider_alias=provider_alias,
            )
        else:
            jobs = await self.job_service.get_job_state(
                scope=scope,
                provider_alias=provider_alias,
            )
        consistency_warnings: list[str] = []
        if snapshot is not None and snapshot.available:
            names = {
                character.card_id: character.name
                for character in characters
            }
            for card_id, anchor in anchors_by_id.items():
                changed = appearance_anchor_drift_labels(anchor, snapshot)
                if changed:
                    consistency_warnings.append(
                        f"角色 {names.get(card_id) or card_id} 的外观锚点"
                        "与当前图像环境不同（"
                        + "、".join(changed)
                        + "），本张图的一致性保证会减弱"
                    )
        return SceneIllustrationStateProjection(
            characters=tuple(characters),
            assets=assets,
            active_job=jobs.active_job,
            cleanup_job=jobs.cleanup_job,
            provider=jobs.provider,
            warnings=tuple(
                dict.fromkeys(
                    [*jobs.warnings, *consistency_warnings]
                )
            ),
        )

    async def start(
        self,
        *,
        owner_id: str,
        novel_id: str,
        chapter_id: str,
        prompt: IllustrationPromptResult,
        scene_character_card_ids: Sequence[str],
        reference_character_card_id: str,
        seed: int | None = None,
        provider_alias: str | None = None,
    ) -> ImageJobProjection:
        owner_id = _canonical_id(owner_id, field="owner_id")
        novel_id = _canonical_id(novel_id, field="novel_id")
        chapter_id = _canonical_id(chapter_id, field="chapter_id")
        if not scene_character_card_ids:
            raise SceneIllustrationConfigurationError(
                "请至少选择一个章细纲已声明的出场角色"
            )
        if len(scene_character_card_ids) > MAX_SCENE_CHARACTER_CARD_IDS:
            raise SceneIllustrationConfigurationError(
                "一次场景插图最多选择 64 个出场角色"
            )
        selected = tuple(
            _canonical_id(value, field="scene_character_card_ids")
            for value in scene_character_card_ids
        )
        if len(set(selected)) != len(selected):
            raise SceneIllustrationConfigurationError(
                "本张图的入画角色不能重复选择"
            )
        reference_id = _canonical_id(
            reference_character_card_id,
            field="reference_character_card_id",
        )
        if reference_id not in selected:
            raise SceneIllustrationConfigurationError(
                "参考角色必须同时包含在本次场景角色中"
            )

        _chapter, declared = await self._chapter_and_declared_ids(
            novel_id=novel_id,
            chapter_id=chapter_id,
        )
        undeclared = [card_id for card_id in selected if card_id not in declared]
        if undeclared:
            raise SceneIllustrationConfigurationError(
                "本张图的入画角色必须全部来自已接受章细纲的出场角色清单"
            )

        selected_anchors: dict[str, AppearanceAnchorSchema] = {}
        selected_names: dict[str, str] = {}
        missing_anchor_names: list[str] = []
        for card_id in selected:
            card = await self.cards.get_character(
                novel_id=novel_id,
                card_id=card_id,
            )
            name = str(card.get("name") or card_id)
            selected_names[card_id] = name
            raw_anchor = card.get("appearance_anchor")
            if raw_anchor is None:
                missing_anchor_names.append(name)
                continue
            try:
                selected_anchors[card_id] = (
                    AppearanceAnchorSchema.model_validate(raw_anchor)
                )
            except ValueError as error:
                raise SceneIllustrationConfigurationError(
                    f"角色 {name} 的外观锚点无效，请重新生成该角色立绘"
                ) from error
        if missing_anchor_names:
            names = "、".join(missing_anchor_names)
            raise SceneIllustrationConfigurationError(
                f"入画角色 {names} 尚未建立外观锚点；请先生成角色立绘，"
                "或从本张图的角色选择中移除"
            )
        descriptor_total = sum(
            len(selected_anchors[card_id].descriptor)
            for card_id in selected
        )
        if descriptor_total > SCENE_ANCHOR_DESCRIPTOR_TOTAL_CHARACTER_LIMIT:
            raise SceneIllustrationConfigurationError(
                "本张图的外观锚点合计超过 4800 字符；请减少本张图的入画角色，"
                "或在确认一致性影响后重设过长的角色锚点"
            )
        anchor = selected_anchors[reference_id]

        asset = await self.asset_repository.get_owned_subject_hash(
            owner_id=to_object_id(owner_id),
            novel_id=to_object_id(novel_id),
            subject_kind="character_portrait",
            subject_id=reference_id,
            content_hash=anchor.reference_asset,
        )
        if asset is None:
            raise SceneIllustrationConfigurationError(
                "所选参考角色的锚点素材记录缺失，请先重新生成角色立绘"
            )
        asset_id = str(asset.get("_id") or "")
        if not asset_id:
            raise SceneIllustrationConfigurationError(
                "所选参考角色的锚点素材记录无效，请先重新生成角色立绘"
            )
        try:
            reference_bytes = await self.asset_reader.read_owned_asset(
                owner_id=owner_id,
                asset_id=asset_id,
            )
        except (ImageAssetNotFoundError, ImageAssetIntegrityError) as error:
            raise SceneIllustrationConfigurationError(
                "所选参考角色的锚点图片缺失，请恢复素材或重新生成角色立绘"
            ) from error
        mime = str(asset.get("mime") or "").lower()
        reference_image = ImageInputAsset(
            filename=_reference_filename(
                mime=mime,
                content_hash=anchor.reference_asset,
            ),
            content=reference_bytes,
            mime_type=mime,
        )

        mandatory_anchor_prefix = anchor.descriptor
        secondary_anchor_lines = [
            (
                "Appearance anchor — "
                f"{selected_names[card_id]}: "
                f"{selected_anchors[card_id].descriptor}"
            )
            for card_id in selected
            if card_id != reference_id
        ]
        user_prompt = _positive_prompt(prompt)
        positive_prompt = "\n".join(
            [
                mandatory_anchor_prefix,
                f"Reference character — {selected_names[reference_id]}",
                *secondary_anchor_lines,
                *([user_prompt] if user_prompt else []),
            ]
        )
        anchor_context = [
            {
                "card_id": card_id,
                "name": selected_names[card_id],
                "descriptor": selected_anchors[card_id].descriptor,
                "reference_asset": (
                    selected_anchors[card_id].reference_asset
                ),
            }
            for card_id in selected
        ]
        return await self.job_service.start_job(
            scope=ImageJobScope(
                owner_id=owner_id,
                novel_id=novel_id,
                usage="scene_illustration",
                subject_id=chapter_id,
            ),
            plan=ImageJobPlan(
                prompt=prompt,
                seed=seed,
                slot_values={
                    "positive_prompt": positive_prompt,
                    "negative_prompt": prompt.negative,
                    "batch_size": 1,
                    "reference_image": reference_image,
                },
                required_slots=frozenset(
                    {"positive_prompt", "seed", "reference_image"}
                ),
                persisted_fields={
                    "final_prompt": positive_prompt,
                    "negative_prompt": prompt.negative,
                    "scene_character_card_ids": list(selected),
                    "reference_character_card_id": reference_id,
                    "reference_asset": anchor.reference_asset,
                    "appearance_anchor_card_ids": list(selected),
                    "mandatory_anchor_prefix": mandatory_anchor_prefix,
                },
                idempotency_context={
                    "scene_character_card_ids": list(selected),
                    "reference_character_card_id": reference_id,
                    "reference_asset": anchor.reference_asset,
                    "appearance_anchors": anchor_context,
                },
            ),
            provider_alias=provider_alias,
        )

    async def poll(
        self,
        *,
        owner_id: str,
        novel_id: str,
        chapter_id: str,
        job_id: str,
    ) -> ImageJobProjection:
        return await self.job_service.poll_job(
            scope=ImageJobScope(
                owner_id=owner_id,
                novel_id=novel_id,
                usage="scene_illustration",
                subject_id=chapter_id,
            ),
            job_id=job_id,
        )

    async def cancel(
        self,
        *,
        owner_id: str,
        novel_id: str,
        chapter_id: str,
        job_id: str,
    ) -> ImageJobProjection:
        return await self.job_service.cancel_job(
            scope=ImageJobScope(
                owner_id=owner_id,
                novel_id=novel_id,
                usage="scene_illustration",
                subject_id=chapter_id,
            ),
            job_id=job_id,
        )


scene_illustration_service = SceneIllustrationService(
    chapters=chapter_repo,
    cards=SceneCharacterGateway(),
    asset_repository=image_asset_repo,
    asset_reader=ManagedImageAssetService(),
    jobs=image_job_repo,
    provider_resolver=ConfiguredImageProviderResolver(),
)


__all__ = [
    "MAX_SCENE_CHARACTER_CARD_IDS",
    "SCENE_ANCHOR_DESCRIPTOR_TOTAL_CHARACTER_LIMIT",
    "SceneCharacterProjection",
    "SceneIllustrationConfigurationError",
    "SceneIllustrationService",
    "SceneIllustrationStateProjection",
    "scene_illustration_service",
]
