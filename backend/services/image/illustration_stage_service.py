"""Explicit staged illustration orchestration.

Slice 7 intentionally exposes only the quick ``compose`` stage.  Every
provider submission requires an explicit start request, while polling and
candidate selection only reconcile already-created jobs and assets.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import datetime
from typing import Any, Literal, Protocol
from uuid import UUID

from bson import ObjectId
from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.config.image_providers import (
    ComfyUIImageProviderConfig,
    ImageProvidersConfig,
    QuickIllustrationPipelineProfile,
    compute_image_pipeline_revision,
    get_image_providers_config,
)
from backend.db.errors import InvalidIdError, NotFoundError
from backend.db.repositories.image_job_repository import image_job_repo
from backend.db.repositories.illustration_run_repository import (
    TERMINAL_ILLUSTRATION_RUN_STATUSES,
    IllustrationRunRepository,
    illustration_run_repo,
)
from backend.db.repositories.image_asset_repository import (
    ImageAssetRepository,
    image_asset_repo,
)
from backend.db.transaction import run_mongo_write_unit
from backend.db.utils import get_utc_now, to_object_id
from backend.services.image.contracts import ImageInputAsset
from backend.services.image.illustration_lineage import IllustrationJobLineage
from backend.services.image.illustration_run_service import (
    IllustrationRunProjection,
    IllustrationRunRevisionConflict,
    IllustrationRunService,
    IllustrationRunStateError,
    IllustrationRunStateMachine,
)
from backend.services.image.managed_assets import ManagedImageAssetService
from backend.services.image.single_image_job_service import (
    ConfiguredImageProviderResolver,
    ImageJobPlan,
    ImageJobProjection,
    ImageJobScope,
    SingleImageJobService,
)
from backend.services.llm.agent_orchestrator import IllustrationPromptResult


class _StageJobService(Protocol):
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


class _AssetReader(Protocol):
    async def read_owned_asset(
        self,
        *,
        owner_id: str,
        asset_id: str,
    ) -> bytes: ...


def _canonical_id(value: Any, *, field_name: str) -> ObjectId:
    if value is None or not str(value).strip():
        raise InvalidIdError(f"{field_name} must be a valid ObjectId")
    return to_object_id(str(value).strip())


def _hash_payload(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _reference_filename(*, mime: str, content_hash: str) -> str:
    extension = {
        "image/jpeg": "jpg",
        "image/png": "png",
        "image/webp": "webp",
    }.get(mime.lower(), "img")
    return f"illustration-reference-{content_hash[:16]}.{extension}"


def _compose_positive_prompt(
    *,
    descriptor: str,
    prompt: IllustrationPromptResult,
    trigger_word: str | None,
) -> str:
    parts = [
        descriptor,
        *(
            [f"Loaded adapter trigger: {trigger_word}"]
            if trigger_word
            else []
        ),
        *(
            f"{label}: {value}"
            for label, value in (
                ("Subject", prompt.subject),
                ("Appearance", prompt.appearance),
                ("Scene", prompt.scene),
                ("Style", prompt.style),
            )
            if value
        ),
    ]
    return "\n".join(parts)


class IllustrationStageStart(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    expected_revision: int = Field(ge=1)
    attempt_id: str = Field(min_length=36, max_length=36)
    prompt: IllustrationPromptResult
    seed: int | None = None
    use_external_adapter: bool = False

    @field_validator("attempt_id", mode="before")
    @classmethod
    def normalize_attempt_id(cls, value: Any) -> str:
        try:
            return str(UUID(str(value or "").strip()))
        except (ValueError, AttributeError) as error:
            raise ValueError("attempt_id must be a canonical UUID") from error

    @field_validator("seed", mode="before")
    @classmethod
    def validate_seed(cls, value: Any) -> int | None:
        if value is None:
            return None
        if type(value) is not int or not 0 <= value <= (2**64 - 1):
            raise ValueError("seed must be an integer from 0 through 2^64-1")
        return value


class IllustrationCandidateSelect(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    expected_revision: int = Field(ge=1)


class IllustrationCandidateProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    asset_id: str
    illustration_brief_id: str
    illustration_run_id: str
    pipeline_stage: Literal["compose"]
    content_hash: str
    mime: str
    width: int
    height: int
    byte_size: int
    source: Literal["generated", "imported"]
    candidate_state: Literal[
        "available",
        "selected",
        "discarded",
        "finalized",
    ]
    selected: bool
    content_url: str
    created_at: datetime | None = None


class IllustrationCandidateListProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    data: tuple[IllustrationCandidateProjection, ...]


class IllustrationStageJobProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run: IllustrationRunProjection
    job: ImageJobProjection


class IllustrationCandidateSelectionProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run: IllustrationRunProjection
    candidate: IllustrationCandidateProjection


class IllustrationStageService:
    """Compose orchestration with manual candidate gates."""

    def __init__(
        self,
        *,
        runs: IllustrationRunRepository | None = None,
        assets: ImageAssetRepository | None = None,
        jobs: _StageJobService | None = None,
        asset_reader: _AssetReader | None = None,
        pipeline_config_loader: Callable[[], ImageProvidersConfig] | None = None,
    ) -> None:
        self._runs = runs or illustration_run_repo
        self._assets = assets or image_asset_repo
        self._jobs = jobs or SingleImageJobService(
            jobs=image_job_repo,
            anchors=None,
            provider_resolver=ConfiguredImageProviderResolver(),
            usage="scene_illustration",
            asset_reader=ManagedImageAssetService(),
            asset_repository=image_asset_repo,
            now=get_utc_now,
        )
        self._asset_reader = asset_reader or ManagedImageAssetService()
        self._pipeline_config_loader = (
            pipeline_config_loader or get_image_providers_config
        )

    @staticmethod
    def _project_run(document: dict[str, Any]) -> IllustrationRunProjection:
        return IllustrationRunService.project_document(document)

    @staticmethod
    def _scope(
        *,
        owner_id: ObjectId,
        novel_id: ObjectId,
        run_id: ObjectId,
    ) -> ImageJobScope:
        return ImageJobScope(
            owner_id=str(owner_id),
            novel_id=str(novel_id),
            usage="scene_illustration",
            subject_id=str(run_id),
        )

    async def _get_run(
        self,
        *,
        owner_id: ObjectId,
        novel_id: ObjectId,
        run_id: ObjectId,
    ) -> dict[str, Any]:
        document = await self._runs.get_owned(
            owner_id=owner_id,
            novel_id=novel_id,
            run_id=run_id,
        )
        if document is None:
            raise NotFoundError("Illustration run was not found")
        return document

    @staticmethod
    def _require_quick_compose(
        document: dict[str, Any],
        *,
        stage: str,
        require_active: bool = True,
        expected_revision: int | None = None,
    ) -> None:
        if stage != "compose":
            raise IllustrationRunStateError(
                "Slice 7 only supports the quick compose stage"
            )
        if (
            require_active
            and document.get("status") in TERMINAL_ILLUSTRATION_RUN_STATUSES
        ):
            raise IllustrationRunStateError("Illustration run is terminal")
        if document.get("pipeline_snapshot", {}).get("kind") != "quick":
            raise IllustrationRunStateError(
                "Slice 7 compose requires a quick illustration pipeline"
            )
        if (
            expected_revision is not None
            and int(document.get("revision") or 0) != expected_revision
        ):
            raise IllustrationRunRevisionConflict(
                "Illustration run revision is stale"
            )

    def _require_frozen_pipeline(
        self,
        document: dict[str, Any],
    ) -> tuple[ImageProvidersConfig, str]:
        snapshot = document["pipeline_snapshot"]
        alias = str(snapshot["alias"])
        config = self._pipeline_config_loader()
        profile = config.pipelines.get(alias)
        if not isinstance(profile, QuickIllustrationPipelineProfile):
            raise IllustrationRunStateError(
                "Frozen quick pipeline is no longer configured"
            )
        revision = compute_image_pipeline_revision(config, alias)
        if revision != snapshot.get("revision"):
            raise IllustrationRunStateError(
                "Frozen pipeline revision drifted; create a new run"
            )
        provider_alias = str(
            snapshot.get("effective_stage_providers", {}).get("compose") or ""
        )
        if provider_alias != profile.compose_provider:
            raise IllustrationRunStateError(
                "Frozen compose provider no longer matches the pipeline revision"
            )
        provider = config.providers.get(provider_alias)
        if provider is None or not provider.enabled:
            raise IllustrationRunStateError(
                "Frozen compose provider is unavailable"
            )
        return config, provider_alias

    async def _reference_input(
        self,
        *,
        owner_id: ObjectId,
        novel_id: ObjectId,
        document: dict[str, Any],
    ) -> ImageInputAsset:
        reference = document["reference_snapshot"]
        asset_id = _canonical_id(reference.get("asset_id"), field_name="asset_id")
        character_id = _canonical_id(
            reference.get("character_card_id"),
            field_name="character_card_id",
        )
        asset = await self._assets.get_owned_subject_asset(
            owner_id=owner_id,
            novel_id=novel_id,
            asset_id=asset_id,
            subject_kind="character_portrait",
            subject_id=str(character_id),
        )
        expected_hash = str(reference.get("asset_sha256") or "")
        if asset is None or asset.get("content_hash") != expected_hash:
            raise IllustrationRunStateError(
                "Frozen reference asset is missing or has changed"
            )
        content = await self._asset_reader.read_owned_asset(
            owner_id=str(owner_id),
            asset_id=str(asset_id),
        )
        mime = str(asset.get("mime") or "application/octet-stream")
        return ImageInputAsset(
            filename=_reference_filename(
                mime=mime,
                content_hash=expected_hash,
            ),
            content=content,
            mime_type=mime,
        )

    @staticmethod
    def _adapter_slots(
        *,
        config: ImageProvidersConfig,
        provider_alias: str,
        document: dict[str, Any],
        enabled: bool,
    ) -> tuple[dict[str, Any], frozenset[str], str | None]:
        if not enabled:
            return {}, frozenset(), None
        adapter = document.get("external_adapter_snapshot")
        if not isinstance(adapter, dict):
            raise IllustrationRunStateError(
                "This run has no frozen external adapter"
            )
        provider = config.providers[provider_alias]
        if not isinstance(provider, ComfyUIImageProviderConfig):
            raise IllustrationRunStateError(
                "External adapters require a bound ComfyUI compose workflow"
            )
        required_adapter_slots = {"lora_name", "lora_strength"}
        if not required_adapter_slots.issubset(provider.workflow.bindings):
            raise IllustrationRunStateError(
                "Compose workflow does not expose safe external adapter slots"
            )
        lora_name = str(adapter.get("lora_name") or "")
        if lora_name not in provider.workflow.dependencies.loras:
            raise IllustrationRunStateError(
                "Frozen external adapter is not declared by the compose provider"
            )
        return (
            {
                "lora_name": lora_name,
                "lora_strength": float(adapter.get("strength") or 0),
            },
            frozenset(required_adapter_slots),
            str(adapter.get("trigger_word") or "").strip() or None,
        )

    async def _persist_attempt(
        self,
        *,
        owner_id: ObjectId,
        novel_id: ObjectId,
        document: dict[str, Any],
        job: ImageJobProjection,
    ) -> dict[str, Any]:
        stages = IllustrationRunStateMachine.begin_attempt(
            document["stages"],
            stage="compose",
            job_id=job.job_id,
        )
        if job.terminal:
            stages = await self._finish_stages_for_job(
                owner_id=owner_id,
                novel_id=novel_id,
                document=document,
                stages=stages,
                job=job,
            )
        updated = await self._runs.update_if_revision(
            owner_id=owner_id,
            novel_id=novel_id,
            run_id=document["_id"],
            expected_revision=int(document["revision"]),
            changes={"stages": stages.model_dump(mode="python")},
        )
        if updated is not None:
            return updated
        winner = await self._get_run(
            owner_id=owner_id,
            novel_id=novel_id,
            run_id=document["_id"],
        )
        if str(
            winner["stages"]["compose"].get("latest_job_id") or ""
        ) == str(job.job_id):
            return winner
        raise IllustrationRunRevisionConflict(
            "Illustration run changed while starting compose"
        )

    async def _candidate_for_job(
        self,
        *,
        owner_id: ObjectId,
        novel_id: ObjectId,
        document: dict[str, Any],
        job: ImageJobProjection,
    ) -> dict[str, Any] | None:
        if job.asset is None:
            return None
        return await self._assets.get_owned_stage_candidate(
            owner_id=owner_id,
            novel_id=novel_id,
            brief_id=document["illustration_brief_id"],
            run_id=document["_id"],
            stage="compose",
            asset_id=_canonical_id(job.asset.asset_id, field_name="asset_id"),
        )

    async def _finish_stages_for_job(
        self,
        *,
        owner_id: ObjectId,
        novel_id: ObjectId,
        document: dict[str, Any],
        stages: Any,
        job: ImageJobProjection,
    ):
        outcome: Literal["candidate_ready", "failed", "cancelled"]
        if job.status == "succeeded" and await self._candidate_for_job(
            owner_id=owner_id,
            novel_id=novel_id,
            document=document,
            job=job,
        ) is not None:
            outcome = "candidate_ready"
        elif job.status == "cancelled":
            outcome = "cancelled"
        else:
            outcome = "failed"
        return IllustrationRunStateMachine.finish_attempt(
            stages,
            stage="compose",
            job_id=job.job_id,
            outcome=outcome,
        )

    async def start_stage(
        self,
        *,
        owner_id: str | ObjectId,
        novel_id: str | ObjectId,
        run_id: str | ObjectId,
        stage: str,
        request: IllustrationStageStart,
    ) -> IllustrationStageJobProjection:
        owner = _canonical_id(owner_id, field_name="owner_id")
        novel = _canonical_id(novel_id, field_name="novel_id")
        run_key = _canonical_id(run_id, field_name="run_id")
        document = await self._get_run(
            owner_id=owner,
            novel_id=novel,
            run_id=run_key,
        )
        self._require_quick_compose(
            document,
            stage=stage,
            expected_revision=request.expected_revision,
        )
        current_stage = document["stages"]["compose"]
        if current_stage.get("status") == "running":
            raise IllustrationRunStateError("Compose already has a running job")
        config, provider_alias = self._require_frozen_pipeline(document)
        reference_input = await self._reference_input(
            owner_id=owner,
            novel_id=novel,
            document=document,
        )
        adapter_slots, adapter_required, trigger_word = self._adapter_slots(
            config=config,
            provider_alias=provider_alias,
            document=document,
            enabled=request.use_external_adapter,
        )
        descriptor = str(document["reference_snapshot"]["descriptor"])
        final_prompt = _compose_positive_prompt(
            descriptor=descriptor,
            prompt=request.prompt,
            trigger_word=trigger_word,
        )
        prompt_revision = _hash_payload(
            {
                "prompt": request.prompt.model_dump(mode="json"),
                "final_prompt": final_prompt,
            }
        )
        pipeline_revision = str(document["pipeline_snapshot"]["revision"])
        lineage = IllustrationJobLineage(
            illustration_brief_id=str(document["illustration_brief_id"]),
            illustration_run_id=str(document["_id"]),
            pipeline_stage="compose",
            reference_asset_hash=str(
                document["reference_snapshot"]["asset_sha256"]
            ),
            profile_revision=pipeline_revision,
            prompt_revision=prompt_revision,
        )
        job = await self._jobs.start_job(
            scope=self._scope(
                owner_id=owner,
                novel_id=novel,
                run_id=run_key,
            ),
            plan=ImageJobPlan(
                prompt=request.prompt,
                seed=request.seed,
                slot_values={
                    "positive_prompt": final_prompt,
                    "negative_prompt": request.prompt.negative,
                    "batch_size": 1,
                    "reference_image": reference_input,
                    **adapter_slots,
                },
                required_slots=frozenset(
                    {
                        "positive_prompt",
                        "negative_prompt",
                        "seed",
                        "batch_size",
                        "reference_image",
                    }
                )
                | adapter_required,
                persisted_fields={
                    "attempt_id": request.attempt_id,
                    "pipeline_alias": document["pipeline_snapshot"]["alias"],
                    "pipeline_revision": pipeline_revision,
                    "prompt_revision": prompt_revision,
                    "final_prompt": final_prompt,
                    "negative_prompt": request.prompt.negative,
                    "appearance_anchor_card_ids": [
                        str(document["reference_snapshot"]["character_card_id"])
                    ],
                    "reference_character_card_id": str(
                        document["reference_snapshot"]["character_card_id"]
                    ),
                    "reference_asset": str(
                        document["reference_snapshot"]["asset_sha256"]
                    ),
                    "use_external_adapter": request.use_external_adapter,
                },
                idempotency_context={
                    "attempt_id": request.attempt_id,
                    "use_external_adapter": request.use_external_adapter,
                    "external_adapter_snapshot": (
                        document.get("external_adapter_snapshot")
                        if request.use_external_adapter
                        else None
                    ),
                },
                illustration_lineage=lineage,
            ),
            provider_alias=provider_alias,
        )
        updated = await self._persist_attempt(
            owner_id=owner,
            novel_id=novel,
            document=document,
            job=job,
        )
        return IllustrationStageJobProjection(
            run=self._project_run(updated),
            job=job,
        )

    async def _reconcile_job(
        self,
        *,
        owner_id: ObjectId,
        novel_id: ObjectId,
        document: dict[str, Any],
        job: ImageJobProjection,
    ) -> dict[str, Any]:
        compose = document["stages"]["compose"]
        if str(compose.get("latest_job_id") or "") != job.job_id:
            raise NotFoundError("Image job does not belong to this run stage")
        if not job.terminal or compose.get("status") != "running":
            return document
        stages = await self._finish_stages_for_job(
            owner_id=owner_id,
            novel_id=novel_id,
            document=document,
            stages=document["stages"],
            job=job,
        )
        updated = await self._runs.update_if_revision(
            owner_id=owner_id,
            novel_id=novel_id,
            run_id=document["_id"],
            expected_revision=int(document["revision"]),
            changes={"stages": stages.model_dump(mode="python")},
        )
        if updated is not None:
            return updated
        winner = await self._get_run(
            owner_id=owner_id,
            novel_id=novel_id,
            run_id=document["_id"],
        )
        if str(
            winner["stages"]["compose"].get("latest_job_id") or ""
        ) == job.job_id and winner["stages"]["compose"].get("status") != "running":
            return winner
        raise IllustrationRunRevisionConflict(
            "Illustration run changed while reconciling compose"
        )

    async def poll_job(
        self,
        *,
        owner_id: str | ObjectId,
        novel_id: str | ObjectId,
        run_id: str | ObjectId,
        job_id: str | ObjectId,
    ) -> IllustrationStageJobProjection:
        owner = _canonical_id(owner_id, field_name="owner_id")
        novel = _canonical_id(novel_id, field_name="novel_id")
        run_key = _canonical_id(run_id, field_name="run_id")
        job_key = _canonical_id(job_id, field_name="job_id")
        document = await self._get_run(
            owner_id=owner,
            novel_id=novel,
            run_id=run_key,
        )
        self._require_quick_compose(document, stage="compose")
        if str(
            document["stages"]["compose"].get("latest_job_id") or ""
        ) != str(job_key):
            raise NotFoundError("Image job does not belong to this run stage")
        job = await self._jobs.poll_job(
            scope=self._scope(
                owner_id=owner,
                novel_id=novel,
                run_id=run_key,
            ),
            job_id=str(job_key),
        )
        updated = await self._reconcile_job(
            owner_id=owner,
            novel_id=novel,
            document=document,
            job=job,
        )
        return IllustrationStageJobProjection(
            run=self._project_run(updated),
            job=job,
        )

    async def cancel_job(
        self,
        *,
        owner_id: str | ObjectId,
        novel_id: str | ObjectId,
        run_id: str | ObjectId,
        job_id: str | ObjectId,
    ) -> IllustrationStageJobProjection:
        owner = _canonical_id(owner_id, field_name="owner_id")
        novel = _canonical_id(novel_id, field_name="novel_id")
        run_key = _canonical_id(run_id, field_name="run_id")
        job_key = _canonical_id(job_id, field_name="job_id")
        document = await self._get_run(
            owner_id=owner,
            novel_id=novel,
            run_id=run_key,
        )
        self._require_quick_compose(document, stage="compose")
        if str(
            document["stages"]["compose"].get("latest_job_id") or ""
        ) != str(job_key):
            raise NotFoundError("Image job does not belong to this run stage")
        job = await self._jobs.cancel_job(
            scope=self._scope(
                owner_id=owner,
                novel_id=novel,
                run_id=run_key,
            ),
            job_id=str(job_key),
        )
        updated = await self._reconcile_job(
            owner_id=owner,
            novel_id=novel,
            document=document,
            job=job,
        )
        return IllustrationStageJobProjection(
            run=self._project_run(updated),
            job=job,
        )

    @staticmethod
    def _project_candidate(
        document: dict[str, Any],
        *,
        selected_asset_id: str | None,
    ) -> IllustrationCandidateProjection:
        asset_id = str(document["_id"])
        selected = asset_id == selected_asset_id
        return IllustrationCandidateProjection(
            asset_id=asset_id,
            illustration_brief_id=str(document["illustration_brief_id"]),
            illustration_run_id=str(document["illustration_run_id"]),
            pipeline_stage="compose",
            content_hash=str(document["content_hash"]),
            mime=str(document["mime"]),
            width=int(document["width"]),
            height=int(document["height"]),
            byte_size=int(document["byte_size"]),
            source=document["source"],
            candidate_state=(
                "selected" if selected else document["candidate_state"]
            ),
            selected=selected,
            content_url=f"/api/image-assets/{asset_id}/content",
            created_at=document.get("created_at"),
        )

    async def list_candidates(
        self,
        *,
        owner_id: str | ObjectId,
        novel_id: str | ObjectId,
        run_id: str | ObjectId,
        stage: str,
    ) -> IllustrationCandidateListProjection:
        owner = _canonical_id(owner_id, field_name="owner_id")
        novel = _canonical_id(novel_id, field_name="novel_id")
        run_key = _canonical_id(run_id, field_name="run_id")
        run = await self._get_run(
            owner_id=owner,
            novel_id=novel,
            run_id=run_key,
        )
        self._require_quick_compose(
            run,
            stage=stage,
            require_active=False,
        )
        candidates = await self._assets.list_owned_stage_candidates(
            owner_id=owner,
            novel_id=novel,
            brief_id=run["illustration_brief_id"],
            run_id=run_key,
            stage="compose",
        )
        selected_asset_id = (
            str(run["stages"]["compose"].get("selected_asset_id"))
            if run["stages"]["compose"].get("selected_asset_id") is not None
            else None
        )
        return IllustrationCandidateListProjection(
            data=tuple(
                self._project_candidate(
                    item,
                    selected_asset_id=selected_asset_id,
                )
                for item in candidates
            )
        )

    async def select_candidate(
        self,
        *,
        owner_id: str | ObjectId,
        novel_id: str | ObjectId,
        run_id: str | ObjectId,
        asset_id: str | ObjectId,
        request: IllustrationCandidateSelect,
    ) -> IllustrationCandidateSelectionProjection:
        owner = _canonical_id(owner_id, field_name="owner_id")
        novel = _canonical_id(novel_id, field_name="novel_id")
        run_key = _canonical_id(run_id, field_name="run_id")
        asset_key = _canonical_id(asset_id, field_name="asset_id")
        initial = await self._get_run(
            owner_id=owner,
            novel_id=novel,
            run_id=run_key,
        )
        self._require_quick_compose(
            initial,
            stage="compose",
            expected_revision=request.expected_revision,
        )
        candidate = await self._assets.get_owned_stage_candidate(
            owner_id=owner,
            novel_id=novel,
            brief_id=initial["illustration_brief_id"],
            run_id=run_key,
            stage="compose",
            asset_id=asset_key,
        )
        if candidate is None:
            raise NotFoundError("Illustration candidate was not found")
        if candidate.get("candidate_state") not in {"available", "selected"}:
            raise IllustrationRunStateError(
                "Only available illustration candidates can be selected"
            )

        async def _select(session):
            current = await self._runs.get_owned(
                owner_id=owner,
                novel_id=novel,
                run_id=run_key,
                session=session,
            )
            if current is None:
                raise NotFoundError("Illustration run was not found")
            self._require_quick_compose(
                current,
                stage="compose",
                expected_revision=request.expected_revision,
            )
            stages = IllustrationRunStateMachine.select_candidate(
                current["stages"],
                stage="compose",
                asset_id=asset_key,
            )
            updated = await self._runs.update_if_revision(
                owner_id=owner,
                novel_id=novel,
                run_id=run_key,
                expected_revision=request.expected_revision,
                changes={"stages": stages.model_dump(mode="python")},
                session=session,
            )
            if updated is None:
                raise IllustrationRunRevisionConflict(
                    "Illustration run revision is stale"
                )
            selected = await self._assets.select_owned_stage_candidate(
                owner_id=owner,
                novel_id=novel,
                brief_id=current["illustration_brief_id"],
                run_id=run_key,
                stage="compose",
                asset_id=asset_key,
                session=session,
            )
            if selected is None:  # fully prevalidated before ordered fallback.
                raise NotFoundError("Illustration candidate was not found")
            return updated, selected

        updated, selected = await run_mongo_write_unit(
            _select,
            "select_illustration_candidate",
        )
        run_projection = self._project_run(updated)
        return IllustrationCandidateSelectionProjection(
            run=run_projection,
            candidate=self._project_candidate(
                selected,
                selected_asset_id=str(asset_key),
            ),
        )


illustration_stage_service = IllustrationStageService()


__all__ = [
    "IllustrationCandidateListProjection",
    "IllustrationCandidateProjection",
    "IllustrationCandidateSelect",
    "IllustrationCandidateSelectionProjection",
    "IllustrationStageJobProjection",
    "IllustrationStageService",
    "IllustrationStageStart",
    "illustration_stage_service",
]
