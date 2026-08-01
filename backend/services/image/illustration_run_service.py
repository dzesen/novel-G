"""Frozen staged-run snapshots for chapter illustrations.

This image-only module does not submit providers, mutate prose context, or
advance narrative revision. Later slices attach jobs and managed candidates
through the run's public state-machine seams.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any, Literal

from bson import ObjectId
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo.errors import DuplicateKeyError

from backend.config.image_providers import (
    ConsistencyIllustrationPipelineProfile,
    ImageProvidersConfig,
    compute_image_pipeline_revision,
    get_image_providers_config,
)
from backend.db.errors import InvalidIdError, NotFoundError
from backend.db.repositories.illustration_brief_repository import (
    IllustrationBriefRepository,
    illustration_brief_repo,
)
from backend.db.repositories.illustration_run_repository import (
    IllustrationRunRepository,
    TERMINAL_ILLUSTRATION_RUN_STATUSES,
    illustration_run_repo,
)
from backend.db.repositories.image_asset_repository import (
    ImageAssetRepository,
    image_asset_repo,
)
from backend.db.utils import to_object_id
from backend.services.image.character_visual_profile_service import (
    CharacterVisualProfileService,
    ExternalLoraAdapter,
)


_PIPELINE_ALIAS = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class IllustrationRunActiveConflict(RuntimeError):
    """A brief already has a non-terminal staged run."""


class IllustrationRunRevisionConflict(RuntimeError):
    """A staged run changed before the requested state transition."""


class IllustrationRunStateError(ValueError):
    """A staged illustration transition violates the closed state machine."""


def _canonical_object_id(value: Any, *, field_name: str) -> ObjectId:
    if value is None or not str(value).strip():
        raise InvalidIdError(f"{field_name} must be a valid ObjectId")
    return to_object_id(str(value).strip())


def _optional_object_id(value: Any, *, field_name: str) -> ObjectId | None:
    if value is None or not str(value).strip():
        return None
    return _canonical_object_id(value, field_name=field_name)


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class IllustrationRunCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    pipeline_alias: str | None = None
    reference_asset_id: str
    parent_run_id: str | None = None
    branch_from_asset_id: str | None = None

    @field_validator("pipeline_alias", mode="before")
    @classmethod
    def normalize_pipeline_alias(cls, value: Any) -> str | None:
        if value is None or not str(value).strip():
            return None
        normalized = str(value).strip()
        if _PIPELINE_ALIAS.fullmatch(normalized) is None:
            raise ValueError("pipeline_alias must be a canonical alias")
        return normalized

    @field_validator(
        "reference_asset_id",
        "parent_run_id",
        "branch_from_asset_id",
        mode="before",
    )
    @classmethod
    def normalize_ids(cls, value: Any, info) -> str | None:
        try:
            parsed = _optional_object_id(value, field_name=info.field_name)
        except InvalidIdError as error:
            raise ValueError(f"{info.field_name} must be a valid ObjectId") from error
        if info.field_name == "reference_asset_id" and parsed is None:
            raise ValueError("reference_asset_id is required")
        return str(parsed) if parsed is not None else None

    @model_validator(mode="after")
    def require_complete_branch_lineage(self) -> "IllustrationRunCreate":
        if (self.parent_run_id is None) != (self.branch_from_asset_id is None):
            raise ValueError(
                "parent_run_id and branch_from_asset_id must be provided together"
            )
        return self


class IllustrationPipelineSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    alias: str
    revision: str
    kind: Literal["quick", "consistency"]
    effective_stage_providers: dict[str, str]


class IllustrationReferenceSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    character_card_id: str
    asset_id: str
    asset_sha256: str
    descriptor: str
    appearance_anchor_sha256: str | None = None


class IllustrationStageProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal[
        "locked",
        "ready",
        "running",
        "awaiting_selection",
        "selected",
        "failed",
        "cancelled",
        "skipped",
    ]
    selected_asset_id: str | None = None
    latest_job_id: str | None = None


class IllustrationStagesProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    compose: IllustrationStageProjection
    identity_edit: IllustrationStageProjection
    refine: IllustrationStageProjection


class IllustrationRunProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    owner_id: str
    novel_id: str
    chapter_id: str
    illustration_brief_id: str
    pipeline_snapshot: IllustrationPipelineSnapshot
    reference_snapshot: IllustrationReferenceSnapshot
    external_adapter_snapshot: ExternalLoraAdapter | None = None
    parent_run_id: str | None = None
    branch_from_asset_id: str | None = None
    stages: IllustrationStagesProjection
    status: Literal["active", "finalized", "cancelled", "failed", "branched"]
    final_asset_id: str | None = None
    revision: int = Field(ge=1)
    created_at: datetime | None = None
    updated_at: datetime | None = None


class IllustrationRunStateMachine:
    """Pure transitions shared by later provider/job orchestration slices."""

    _STAGES = ("compose", "identity_edit", "refine")

    @staticmethod
    def _canonical_id(value: Any, *, field_name: str) -> str:
        return str(_canonical_object_id(value, field_name=field_name))

    @classmethod
    def _validated(
        cls,
        value: IllustrationStagesProjection | dict[str, Any],
    ) -> IllustrationStagesProjection:
        return IllustrationStagesProjection.model_validate(value)

    @classmethod
    def _stage(
        cls,
        stages: IllustrationStagesProjection,
        stage: str,
    ) -> IllustrationStageProjection:
        if stage not in cls._STAGES:
            raise IllustrationRunStateError(
                f"Unknown illustration stage: {stage}"
            )
        return getattr(stages, stage)

    @staticmethod
    def _replace_stage(
        stages: IllustrationStagesProjection,
        stage: str,
        replacement: IllustrationStageProjection,
    ) -> IllustrationStagesProjection:
        payload = stages.model_dump(mode="python")
        payload[stage] = replacement.model_dump(mode="python")
        return IllustrationStagesProjection.model_validate(payload)

    @classmethod
    def initial_stages(
        cls,
        *,
        kind: Literal["quick", "consistency"],
        has_refine: bool,
    ) -> IllustrationStagesProjection:
        if kind == "quick" and has_refine:
            raise IllustrationRunStateError(
                "Quick pipelines cannot declare a refine stage"
            )
        return IllustrationStagesProjection(
            compose=IllustrationStageProjection(status="ready"),
            identity_edit=IllustrationStageProjection(
                status="locked" if kind == "consistency" else "skipped"
            ),
            refine=IllustrationStageProjection(
                status=(
                    "locked"
                    if kind == "consistency" and has_refine
                    else "skipped"
                )
            ),
        )

    @classmethod
    def begin_attempt(
        cls,
        value: IllustrationStagesProjection | dict[str, Any],
        *,
        stage: str,
        job_id: str | ObjectId,
    ) -> IllustrationStagesProjection:
        stages = cls._validated(value)
        current = cls._stage(stages, stage)
        if current.status in {"locked", "skipped", "running"}:
            raise IllustrationRunStateError(
                f"Stage {stage} cannot start from {current.status}"
            )
        return cls._replace_stage(
            stages,
            stage,
            current.model_copy(
                update={
                    "status": "running",
                    "latest_job_id": cls._canonical_id(
                        job_id,
                        field_name="job_id",
                    ),
                }
            ),
        )

    @classmethod
    def finish_attempt(
        cls,
        value: IllustrationStagesProjection | dict[str, Any],
        *,
        stage: str,
        job_id: str | ObjectId,
        outcome: Literal["candidate_ready", "failed", "cancelled"],
    ) -> IllustrationStagesProjection:
        stages = cls._validated(value)
        current = cls._stage(stages, stage)
        canonical_job_id = cls._canonical_id(job_id, field_name="job_id")
        if (
            current.status != "running"
            or current.latest_job_id != canonical_job_id
        ):
            raise IllustrationRunStateError(
                f"Stage {stage} does not own this running attempt"
            )
        status = {
            "candidate_ready": "awaiting_selection",
            "failed": "failed",
            "cancelled": "cancelled",
        }[outcome]
        return cls._replace_stage(
            stages,
            stage,
            current.model_copy(update={"status": status}),
        )

    @classmethod
    def select_candidate(
        cls,
        value: IllustrationStagesProjection | dict[str, Any],
        *,
        stage: str,
        asset_id: str | ObjectId,
    ) -> IllustrationStagesProjection:
        stages = cls._validated(value)
        current = cls._stage(stages, stage)
        if current.status not in {
            "awaiting_selection",
            "selected",
            "failed",
            "cancelled",
        }:
            raise IllustrationRunStateError(
                f"Stage {stage} cannot select from {current.status}"
            )
        return cls._replace_stage(
            stages,
            stage,
            current.model_copy(
                update={
                    "status": "selected",
                    "selected_asset_id": cls._canonical_id(
                        asset_id,
                        field_name="asset_id",
                    ),
                }
            ),
        )

    @classmethod
    def advance(
        cls,
        value: IllustrationStagesProjection | dict[str, Any],
        *,
        stage: str,
    ) -> IllustrationStagesProjection:
        stages = cls._validated(value)
        current = cls._stage(stages, stage)
        if current.selected_asset_id is None:
            raise IllustrationRunStateError(
                f"Stage {stage} requires a selected candidate before advance"
            )
        target_name = {
            "compose": "identity_edit",
            "identity_edit": "refine",
        }.get(stage)
        if target_name is None:
            raise IllustrationRunStateError(
                f"Stage {stage} has no downstream stage"
            )
        target = cls._stage(stages, target_name)
        if target.status == "skipped":
            raise IllustrationRunStateError(
                f"Stage {target_name} is not configured"
            )
        if target.status != "locked":
            raise IllustrationRunStateError(
                f"Stage {target_name} is already unlocked"
            )
        return cls._replace_stage(
            stages,
            target_name,
            target.model_copy(update={"status": "ready"}),
        )

    @classmethod
    def final_asset_id(
        cls,
        value: IllustrationStagesProjection | dict[str, Any],
        *,
        kind: Literal["quick", "consistency"],
    ) -> str:
        stages = cls._validated(value)
        selected = (
            stages.compose.selected_asset_id
            if kind == "quick"
            else (
                stages.refine.selected_asset_id
                or stages.identity_edit.selected_asset_id
            )
        )
        if selected is None:
            required_stage = "compose" if kind == "quick" else "identity_edit"
            raise IllustrationRunStateError(
                f"Pipeline requires a selected {required_stage} candidate"
            )
        return selected


class IllustrationRunService:
    def __init__(
        self,
        *,
        repository: IllustrationRunRepository | None = None,
        briefs: IllustrationBriefRepository | None = None,
        assets: ImageAssetRepository | None = None,
        visual_profiles: CharacterVisualProfileService | None = None,
        pipeline_config_loader: Callable[[], ImageProvidersConfig] | None = None,
    ) -> None:
        self._repository = repository or illustration_run_repo
        self._briefs = briefs or illustration_brief_repo
        self._assets = assets or image_asset_repo
        self._visual_profiles = visual_profiles or CharacterVisualProfileService()
        self._pipeline_config_loader = (
            pipeline_config_loader or get_image_providers_config
        )

    @staticmethod
    def project_document(
        document: dict[str, Any],
    ) -> IllustrationRunProjection:
        def optional_id(value: Any) -> str | None:
            return str(value) if value is not None else None

        reference = dict(document["reference_snapshot"])
        reference["character_card_id"] = str(reference["character_card_id"])
        reference["asset_id"] = str(reference["asset_id"])
        stages = {
            name: {
                **dict(document["stages"][name]),
                "selected_asset_id": optional_id(
                    document["stages"][name].get("selected_asset_id")
                ),
                "latest_job_id": optional_id(
                    document["stages"][name].get("latest_job_id")
                ),
            }
            for name in ("compose", "identity_edit", "refine")
        }
        return IllustrationRunProjection(
            run_id=str(document["_id"]),
            owner_id=str(document["owner_id"]),
            novel_id=str(document["novel_id"]),
            chapter_id=str(document["chapter_id"]),
            illustration_brief_id=str(document["illustration_brief_id"]),
            pipeline_snapshot=document["pipeline_snapshot"],
            reference_snapshot=reference,
            external_adapter_snapshot=document.get("external_adapter_snapshot"),
            parent_run_id=optional_id(document.get("parent_run_id")),
            branch_from_asset_id=optional_id(document.get("branch_from_asset_id")),
            stages=stages,
            status=document["status"],
            final_asset_id=optional_id(document.get("final_asset_id")),
            revision=int(document["revision"]),
            created_at=_as_utc(document.get("created_at")),
            updated_at=_as_utc(document.get("updated_at")),
        )

    # Backward-compatible private alias for the existing internal call sites.
    _project = project_document

    @staticmethod
    def _stage_providers(profile: Any) -> dict[str, str]:
        providers = {"compose": str(profile.compose_provider)}
        if isinstance(profile, ConsistencyIllustrationPipelineProfile):
            providers["identity_edit"] = str(profile.identity_edit_provider)
            if profile.refine_provider is not None:
                providers["refine"] = str(profile.refine_provider)
        return providers

    @staticmethod
    def _initial_stages(
        kind: Literal["quick", "consistency"],
        *,
        has_refine: bool,
    ) -> dict[str, Any]:
        return IllustrationRunStateMachine.initial_stages(
            kind=kind,
            has_refine=has_refine,
        ).model_dump(mode="python")

    async def create_run(
        self,
        *,
        owner_id: str | ObjectId,
        novel_id: str | ObjectId,
        chapter_id: str | ObjectId,
        brief_id: str | ObjectId,
        request: IllustrationRunCreate,
    ) -> IllustrationRunProjection:
        owner = _canonical_object_id(owner_id, field_name="owner_id")
        novel = _canonical_object_id(novel_id, field_name="novel_id")
        chapter = _canonical_object_id(chapter_id, field_name="chapter_id")
        brief_key = _canonical_object_id(brief_id, field_name="brief_id")
        reference_asset_id = _canonical_object_id(
            request.reference_asset_id,
            field_name="reference_asset_id",
        )
        brief = await self._briefs.get_owned(
            owner_id=owner,
            novel_id=novel,
            chapter_id=chapter,
            brief_id=brief_key,
        )
        if brief is None or brief.get("status") != "active":
            raise NotFoundError("Active illustration brief was not found")
        reference_card_id = brief.get("default_reference_character_card_id")
        if reference_card_id is None:
            raise ValueError(
                "Illustration brief requires a default reference character"
            )
        config = self._pipeline_config_loader()
        pipeline_alias = (
            request.pipeline_alias
            or brief.get("default_pipeline_alias")
            or config.default_scene_pipeline
        )
        if not pipeline_alias or pipeline_alias not in config.pipelines:
            raise ValueError("A configured illustration pipeline is required")
        profile = config.pipelines[pipeline_alias]
        visual_profile = await self._visual_profiles.get(
            owner_id=owner,
            novel_id=novel,
            character_card_id=reference_card_id,
        )
        if all(
            reference.asset_id != str(reference_asset_id)
            for reference in visual_profile.references
        ):
            raise ValueError(
                "Reference asset must be selected from the character visual profile"
            )
        anchor = visual_profile.appearance_anchor
        if anchor is None or not anchor.descriptor.strip():
            raise ValueError("Reference character requires an appearance descriptor")
        asset = await self._assets.get_owned_subject_asset(
            owner_id=owner,
            novel_id=novel,
            asset_id=reference_asset_id,
            subject_kind="character_portrait",
            subject_id=str(reference_card_id),
        )
        content_hash = str((asset or {}).get("content_hash") or "")
        if asset is None or _SHA256.fullmatch(content_hash) is None:
            raise ValueError("Reference asset is missing a valid managed SHA-256")
        if await self._repository.get_active_for_brief(
            owner_id=owner,
            novel_id=novel,
            illustration_brief_id=brief_key,
        ) is not None:
            raise IllustrationRunActiveConflict(
                "Illustration brief already has an active run"
            )
        parent_run_id = _optional_object_id(
            request.parent_run_id,
            field_name="parent_run_id",
        )
        branch_from_asset_id = _optional_object_id(
            request.branch_from_asset_id,
            field_name="branch_from_asset_id",
        )
        if parent_run_id is not None and branch_from_asset_id is not None:
            parent = await self._repository.get_owned(
                owner_id=owner,
                novel_id=novel,
                run_id=parent_run_id,
            )
            if (
                parent is None
                or parent.get("chapter_id") != chapter
                or parent.get("illustration_brief_id") != brief_key
            ):
                raise ValueError(
                    "Parent run must belong to the same illustration brief"
                )
            if parent.get("status") not in TERMINAL_ILLUSTRATION_RUN_STATUSES:
                raise ValueError(
                    "Parent run must be explicitly ended before branching"
                )
            selected_assets = {
                stage.get("selected_asset_id")
                for stage in (parent.get("stages") or {}).values()
                if stage.get("selected_asset_id") is not None
            }
            if parent.get("final_asset_id") is not None:
                selected_assets.add(parent["final_asset_id"])
            if branch_from_asset_id not in selected_assets:
                raise ValueError(
                    "Branch source must be a selected parent-run asset"
                )
        stage_providers = self._stage_providers(profile)
        document = {
            "owner_id": owner,
            "novel_id": novel,
            "chapter_id": chapter,
            "illustration_brief_id": brief_key,
            "pipeline_snapshot": {
                "alias": pipeline_alias,
                "revision": compute_image_pipeline_revision(
                    config,
                    pipeline_alias,
                ),
                "kind": profile.kind,
                "effective_stage_providers": stage_providers,
            },
            "reference_snapshot": {
                "character_card_id": ObjectId(reference_card_id),
                "asset_id": reference_asset_id,
                "asset_sha256": content_hash,
                "descriptor": anchor.descriptor,
                "appearance_anchor_sha256": _canonical_hash(
                    anchor.model_dump(mode="json")
                ),
            },
            "external_adapter_snapshot": (
                visual_profile.external_adapter.model_dump(mode="python")
                if visual_profile.external_adapter is not None
                else None
            ),
            "parent_run_id": parent_run_id,
            "branch_from_asset_id": branch_from_asset_id,
            "stages": self._initial_stages(
                profile.kind,
                has_refine="refine" in stage_providers,
            ),
            "final_asset_id": None,
        }
        try:
            stored = await self._repository.create_active(document)
        except DuplicateKeyError as error:
            raise IllustrationRunActiveConflict(
                "Illustration brief already has an active run"
            ) from error
        return self._project(stored)

    async def preserve_for_branch(
        self,
        *,
        owner_id: str | ObjectId,
        novel_id: str | ObjectId,
        run_id: str | ObjectId,
        branch_from_asset_id: str | ObjectId,
        expected_revision: int,
    ) -> IllustrationRunProjection:
        owner = _canonical_object_id(owner_id, field_name="owner_id")
        novel = _canonical_object_id(novel_id, field_name="novel_id")
        run_key = _canonical_object_id(run_id, field_name="run_id")
        branch_asset = _canonical_object_id(
            branch_from_asset_id,
            field_name="branch_from_asset_id",
        )
        current = await self._repository.get_owned(
            owner_id=owner,
            novel_id=novel,
            run_id=run_key,
        )
        if current is None:
            raise NotFoundError("Illustration run was not found")
        if (
            int(current.get("revision") or 0) != expected_revision
            or current.get("status")
            in TERMINAL_ILLUSTRATION_RUN_STATUSES
        ):
            raise IllustrationRunRevisionConflict(
                "Illustration run revision is stale or already terminal"
            )
        selected_assets = {
            stage.get("selected_asset_id")
            for stage in (current.get("stages") or {}).values()
            if stage.get("selected_asset_id") is not None
        }
        if current.get("final_asset_id") is not None:
            selected_assets.add(current["final_asset_id"])
        if branch_asset not in selected_assets:
            raise ValueError(
                "Branch source must be a selected run candidate"
            )
        stored = await self._repository.update_if_revision(
            owner_id=owner,
            novel_id=novel,
            run_id=run_key,
            expected_revision=expected_revision,
            changes={"status": "branched"},
        )
        if stored is None:
            raise IllustrationRunRevisionConflict(
                "Illustration run revision is stale or already terminal"
            )
        return self._project(stored)

    async def get_run(
        self,
        *,
        owner_id: str | ObjectId,
        novel_id: str | ObjectId,
        run_id: str | ObjectId,
    ) -> IllustrationRunProjection:
        owner = _canonical_object_id(owner_id, field_name="owner_id")
        novel = _canonical_object_id(novel_id, field_name="novel_id")
        run = _canonical_object_id(run_id, field_name="run_id")
        document = await self._repository.get_owned(
            owner_id=owner,
            novel_id=novel,
            run_id=run,
        )
        if document is None:
            raise NotFoundError("Illustration run was not found")
        return self._project(document)


illustration_run_service = IllustrationRunService()


__all__ = [
    "IllustrationRunActiveConflict",
    "IllustrationRunRevisionConflict",
    "IllustrationRunStateError",
    "IllustrationRunStateMachine",
    "IllustrationRunCreate",
    "IllustrationRunProjection",
    "IllustrationRunService",
    "illustration_run_service",
]
