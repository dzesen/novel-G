"""Thin character-portrait adapter over the shared single-image job."""

from __future__ import annotations

from datetime import datetime
import secrets
import time
from typing import Callable

from backend.db.repositories.image_job_repository import image_job_repo
from backend.db.utils import get_utc_now
from backend.services.image.managed_assets import (
    ImagePollAssetConsumer,
)
from backend.services.image.single_image_job_service import (
    AppearanceAnchorGatewayProtocol,
    CharacterPortraitStateProjection,
    ConfiguredImageProviderResolver,
    ImageAssetMetadataRepositoryProtocol,
    ImageJobRepositoryProtocol,
    ImageProviderResolverProtocol,
    ImageProviderSnapshot,
    ManagedAssetReaderProtocol,
    PortraitAnchorResetRequired,
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


class CharacterPortraitService:
    """Supply portrait validation/state while delegating the durable job."""

    def __init__(
        self,
        *,
        jobs: ImageJobRepositoryProtocol,
        anchors: AppearanceAnchorGatewayProtocol,
        provider_resolver: ImageProviderResolverProtocol,
        asset_consumer: ImagePollAssetConsumer | None = None,
        asset_reader: ManagedAssetReaderProtocol | None = None,
        asset_repository: ImageAssetMetadataRepositoryProtocol | None = None,
        now: Callable[[], datetime],
        now_epoch: Callable[[], float] = time.time,
        seed_factory: Callable[[], int] = lambda: secrets.randbits(64),
    ) -> None:
        self._jobs = SingleImageJobService(
            jobs=jobs,
            anchors=anchors,
            provider_resolver=provider_resolver,
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
        return await self._jobs.get_state(
            owner_id=owner_id,
            novel_id=novel_id,
            card_id=card_id,
            provider_alias=provider_alias,
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
    ) -> PortraitJobProjection:
        return await self._jobs.start(
            owner_id=owner_id,
            novel_id=novel_id,
            card_id=card_id,
            prompt=prompt,
            seed=seed,
            provider_alias=provider_alias,
            confirm_anchor_reset=confirm_anchor_reset,
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
    now=get_utc_now,
)


__all__ = [
    "CharacterPortraitService",
    "CharacterPortraitStateProjection",
    "ConfiguredImageProviderResolver",
    "ImageJobRepositoryProtocol",
    "ImageProviderResolverProtocol",
    "ImageProviderSnapshot",
    "PortraitAnchorResetRequired",
    "PortraitAssetProjection",
    "PortraitConfigurationError",
    "PortraitJobNotFoundError",
    "PortraitJobProjection",
    "PortraitProviderProjection",
    "ReferenceCardAppearanceAnchorGateway",
    "ResolvedImageProvider",
    "character_portrait_service",
]
