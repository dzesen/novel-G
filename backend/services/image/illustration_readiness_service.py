"""Read-only deployment readiness for frozen staged illustration runs.

The service deliberately has no image submission, upload, polling, job creation,
or configuration mutation seam. It verifies the entire frozen consistency chain
before a user explicitly starts a paid or local-GPU stage.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from typing import Any, Literal, Protocol

from bson import ObjectId
from pydantic import BaseModel, ConfigDict, Field

from backend.config.config import CONFIG_PATH
from backend.config.image_providers import (
    ComfyUIImageProviderConfig,
    ConsistencyIllustrationPipelineProfile,
    ImagePipelineStageQualityEvidence,
    ImageProvidersConfig,
    WorkflowDependencies,
    compute_image_pipeline_quality_fingerprint,
    compute_image_pipeline_revision,
    compute_image_provider_revision,
    get_image_providers_config,
)
from backend.db.errors import InvalidIdError, NotFoundError
from backend.db.repositories.illustration_run_repository import (
    IllustrationRunRepository,
    illustration_run_repo,
)
from backend.db.repositories.image_asset_repository import (
    ImageAssetRepository,
    image_asset_repo,
)
from backend.db.repositories.image_job_repository import (
    ImageJobRepository,
    image_job_repo,
)
from backend.db.utils import to_object_id
from backend.services.image.managed_assets import ManagedImageAssetService
from backend.services.image.provider_test_service import (
    ImageProviderDependencyCheck,
    ImageProviderTestRequest,
    ImageProviderTestResponse,
    test_image_provider_connection,
)


_SHA256 = re.compile(r"^(?:sha256:)?[0-9a-f]{64}$")
_STAGE_SLOTS: dict[str, dict[str, bool]] = {
    "compose": {
        "positive_prompt": False,
        "negative_prompt": False,
        "seed": False,
        "batch_size": False,
        "reference_image": True,
    },
    "identity_edit": {
        "base_image": True,
        "identity_reference": True,
        "edit_instruction": False,
        "must_preserve": False,
        "allowed_changes": False,
        "seed": False,
    },
    "refine": {
        "base_image": True,
        "goal": False,
        "strength": False,
        "supplemental_instruction": False,
        "seed": False,
    },
}


class _AssetReader(Protocol):
    async def read_owned_asset(self, *, owner_id: str, asset_id: str) -> bytes: ...


class _JobHistory(Protocol):
    async def median_completed_seconds(
        self,
        *,
        provider_alias: str,
        usage: str,
    ) -> int | None: ...


ProviderProbe = Callable[
    ...,
    Awaitable[ImageProviderTestResponse],
]


class IllustrationReadinessIssue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str = Field(min_length=1, max_length=120)
    message: str = Field(min_length=1, max_length=2_000)
    stage: Literal["compose", "identity_edit", "refine"] | None = None


class IllustrationStageReadiness(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    stage: Literal["compose", "identity_edit", "refine"]
    provider_alias: str
    status: Literal["passed", "blocked"]
    summary: str
    template_revision: str = ""
    effective_definition_hash: str = ""
    runtime_fingerprint_hash: str = ""
    queue_running: int = Field(default=0, ge=0)
    queue_pending: int = Field(default=0, ge=0)
    max_concurrency: int = Field(ge=1)
    median_completed_seconds: int | None = Field(default=None, ge=1)
    fallback_timeout_seconds: int = Field(ge=1)
    dependency_checks: tuple[ImageProviderDependencyCheck, ...] = ()


class IllustrationQualityReadiness(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["accepted", "experimental", "drifted"]
    fingerprint: str = ""
    reason: str


class IllustrationReadinessReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    run_revision: int = Field(ge=1)
    pipeline_alias: str
    pipeline_revision: str
    pipeline_kind: Literal["consistency"] = "consistency"
    status: Literal["passed", "blocked"]
    readiness_digest: str
    required_stages: tuple[
        Literal["compose", "identity_edit", "refine"],
        ...,
    ]
    optional_refine: bool
    max_provider_calls: int = Field(ge=2, le=3)
    reference_verified: bool
    stages: tuple[IllustrationStageReadiness, ...]
    issues: tuple[IllustrationReadinessIssue, ...]
    quality: IllustrationQualityReadiness


def _canonical_object_id(value: Any, *, field_name: str) -> ObjectId:
    if value is None or not str(value).strip():
        raise InvalidIdError(f"{field_name} must be a valid ObjectId")
    return to_object_id(str(value).strip())


def _canonical_hash(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=str,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _normalized_sha256(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    if _SHA256.fullmatch(normalized) is None:
        return ""
    return normalized.removeprefix("sha256:")


def _stage_aliases(
    profile: ConsistencyIllustrationPipelineProfile,
) -> tuple[tuple[str, str], ...]:
    stages: list[tuple[str, str]] = [
        ("compose", profile.compose_provider),
        ("identity_edit", profile.identity_edit_provider),
    ]
    if profile.refine_provider:
        stages.append(("refine", profile.refine_provider))
    return tuple(stages)


def _with_external_lora_dependency(
    provider: ComfyUIImageProviderConfig,
    lora_name: str,
) -> ComfyUIImageProviderConfig:
    dependencies = provider.workflow.dependencies
    loras = list(dict.fromkeys([*dependencies.loras, lora_name]))
    workflow = provider.workflow.model_copy(
        update={
            "dependencies": WorkflowDependencies(
                node_types=list(dependencies.node_types),
                checkpoints=list(dependencies.checkpoints),
                loras=loras,
            )
        }
    )
    return provider.model_copy(update={"workflow": workflow})


class IllustrationReadinessService:
    def __init__(
        self,
        *,
        runs: IllustrationRunRepository | None = None,
        assets: ImageAssetRepository | None = None,
        asset_reader: _AssetReader | None = None,
        jobs: _JobHistory | None = None,
        pipeline_config_loader: Callable[[], ImageProvidersConfig] | None = None,
        provider_probe: ProviderProbe | None = None,
    ) -> None:
        self._runs = runs or illustration_run_repo
        self._assets = assets or image_asset_repo
        self._asset_reader = asset_reader or ManagedImageAssetService()
        self._jobs = jobs or image_job_repo
        self._pipeline_config_loader = (
            pipeline_config_loader or get_image_providers_config
        )
        self._provider_probe = provider_probe

    async def _probe(
        self,
        *,
        alias: str,
        provider: ComfyUIImageProviderConfig,
    ) -> ImageProviderTestResponse:
        if self._provider_probe is not None:
            return await self._provider_probe(alias=alias, provider=provider)
        return await test_image_provider_connection(
            ImageProviderTestRequest(alias=alias, provider=provider),
            {},
            template_root=CONFIG_PATH.resolve().parent,
        )

    @staticmethod
    def _validate_stage_contract(
        *,
        stage: str,
        provider: ComfyUIImageProviderConfig,
        external_lora_name: str,
    ) -> list[IllustrationReadinessIssue]:
        issues: list[IllustrationReadinessIssue] = []
        required_slots = dict(_STAGE_SLOTS[stage])
        if stage == "compose" and external_lora_name:
            required_slots.update({"lora_name": False, "lora_strength": False})
        for slot, upload_required in required_slots.items():
            binding = provider.workflow.bindings.get(slot)
            if binding is None or not binding.required:
                issues.append(
                    IllustrationReadinessIssue(
                        code="missing_required_binding",
                        message=f"{stage} requires semantic slot {slot}",
                        stage=stage,
                    )
                )
                continue
            if upload_required and not binding.upload:
                issues.append(
                    IllustrationReadinessIssue(
                        code="invalid_upload_binding",
                        message=f"{stage}.{slot} must be an upload binding",
                        stage=stage,
                    )
                )
        if len(provider.workflow.outputs) != 1:
            issues.append(
                IllustrationReadinessIssue(
                    code="invalid_output_contract",
                    message=f"{stage} must declare exactly one image output",
                    stage=stage,
                )
            )
        return issues

    async def inspect(
        self,
        *,
        owner_id: str | ObjectId,
        novel_id: str | ObjectId,
        run_id: str | ObjectId,
    ) -> IllustrationReadinessReport:
        owner = _canonical_object_id(owner_id, field_name="owner_id")
        novel = _canonical_object_id(novel_id, field_name="novel_id")
        run_key = _canonical_object_id(run_id, field_name="run_id")
        run = await self._runs.get_owned(
            owner_id=owner,
            novel_id=novel,
            run_id=run_key,
        )
        if run is None:
            raise NotFoundError("Illustration run was not found")

        issues: list[IllustrationReadinessIssue] = []
        pipeline_snapshot = dict(run.get("pipeline_snapshot") or {})
        pipeline_alias = str(pipeline_snapshot.get("alias") or "")
        frozen_revision = str(pipeline_snapshot.get("revision") or "")
        config = self._pipeline_config_loader()
        profile = config.pipelines.get(pipeline_alias)
        if not isinstance(profile, ConsistencyIllustrationPipelineProfile):
            raise ValueError("Readiness is only available for consistency runs")
        stage_aliases = _stage_aliases(profile)
        required_stages = tuple(stage for stage, _alias in stage_aliases)

        try:
            current_revision = compute_image_pipeline_revision(config, pipeline_alias)
        except ValueError as error:
            current_revision = ""
            issues.append(
                IllustrationReadinessIssue(
                    code="pipeline_invalid",
                    message=str(error),
                )
            )
        if frozen_revision != current_revision:
            issues.append(
                IllustrationReadinessIssue(
                    code="pipeline_revision_drift",
                    message="The active pipeline no longer matches the frozen run",
                )
            )
        frozen_aliases = dict(
            pipeline_snapshot.get("effective_stage_providers") or {}
        )
        expected_aliases = dict(stage_aliases)
        if frozen_aliases != expected_aliases:
            issues.append(
                IllustrationReadinessIssue(
                    code="pipeline_provider_drift",
                    message="The effective stage providers differ from the frozen run",
                )
            )
        if run.get("status") != "active":
            issues.append(
                IllustrationReadinessIssue(
                    code="run_not_active",
                    message="Only an active illustration run can start a stage",
                )
            )

        reference = dict(run.get("reference_snapshot") or {})
        reference_asset_id = _canonical_object_id(
            reference.get("asset_id"),
            field_name="reference_asset_id",
        )
        character_card_id = _canonical_object_id(
            reference.get("character_card_id"),
            field_name="character_card_id",
        )
        expected_reference_hash = _normalized_sha256(
            reference.get("asset_sha256")
        )
        reference_asset = await self._assets.get_owned_subject_asset(
            owner_id=owner,
            novel_id=novel,
            asset_id=reference_asset_id,
            subject_kind="character_portrait",
            subject_id=str(character_card_id),
        )
        reference_verified = False
        if reference_asset is None:
            issues.append(
                IllustrationReadinessIssue(
                    code="reference_not_found",
                    message="The frozen character reference is not owner-visible",
                )
            )
        else:
            stored_hash = _normalized_sha256(reference_asset.get("content_hash"))
            try:
                reference_bytes = await self._asset_reader.read_owned_asset(
                    owner_id=str(owner),
                    asset_id=str(reference_asset_id),
                )
                actual_hash = hashlib.sha256(reference_bytes).hexdigest()
            except Exception:
                actual_hash = ""
            reference_verified = bool(
                expected_reference_hash
                and expected_reference_hash == stored_hash == actual_hash
            )
            if not reference_verified:
                issues.append(
                    IllustrationReadinessIssue(
                        code="reference_hash_mismatch",
                        message=(
                            "The frozen reference bytes no longer match their SHA-256"
                        ),
                    )
                )

        external_adapter = dict(run.get("external_adapter_snapshot") or {})
        external_lora_name = str(external_adapter.get("lora_name") or "").strip()
        stage_reports: list[IllustrationStageReadiness] = []
        quality_evidence: dict[str, ImagePipelineStageQualityEvidence] = {}
        for stage, provider_alias in stage_aliases:
            issue_count_before = len(issues)
            configured_provider = config.providers.get(provider_alias)
            if not isinstance(configured_provider, ComfyUIImageProviderConfig):
                issues.append(
                    IllustrationReadinessIssue(
                        code="unsupported_provider",
                        message=f"{stage} requires an enabled ComfyUI provider",
                        stage=stage,
                    )
                )
                continue
            provider: ComfyUIImageProviderConfig = configured_provider
            if not provider.enabled:
                issues.append(
                    IllustrationReadinessIssue(
                        code="provider_disabled",
                        message=f"Provider {provider_alias} is disabled",
                        stage=stage,
                    )
                )
            if (
                stage == "compose"
                and external_lora_name
                and external_lora_name not in provider.workflow.dependencies.loras
            ):
                issues.append(
                    IllustrationReadinessIssue(
                        code="external_lora_not_declared",
                        message=(
                            "The frozen external LoRA is not declared by compose"
                        ),
                        stage="compose",
                    )
                )
            issues.extend(
                self._validate_stage_contract(
                    stage=stage,
                    provider=provider,
                    external_lora_name=(
                        external_lora_name if stage == "compose" else ""
                    ),
                )
            )
            probe_provider = provider
            if stage == "compose" and external_lora_name:
                probe_provider = _with_external_lora_dependency(
                    provider,
                    external_lora_name,
                )
            try:
                probe = await self._probe(
                    alias=provider_alias,
                    provider=probe_provider,
                )
            except Exception as error:
                probe = ImageProviderTestResponse(
                    alias=provider_alias,
                    provider_type="comfyui",
                    status="failed",
                    summary=str(error) or "Provider readiness probe failed",
                )
            if probe.status != "passed":
                issues.append(
                    IllustrationReadinessIssue(
                        code="provider_not_ready",
                        message=probe.summary,
                        stage=stage,
                    )
                )
            if probe.template_revision != provider.workflow.template_revision:
                issues.append(
                    IllustrationReadinessIssue(
                        code="template_revision_drift",
                        message=(
                            f"{stage} template revision does not match configuration"
                        ),
                        stage=stage,
                    )
                )

            runtime_fingerprint = probe.runtime_fingerprint
            runtime_hash = _canonical_hash(
                runtime_fingerprint.model_dump(mode="json")
            )
            effective_hash = str(probe.effective_graph_hash or "")
            if _SHA256.fullmatch(effective_hash) is not None:
                quality_evidence[stage] = ImagePipelineStageQualityEvidence(
                    provider_alias=provider_alias,
                    provider_revision=compute_image_provider_revision(provider),
                    adapter_id="comfyui",
                    adapter_revision="1",
                    effective_definition_hash=effective_hash,
                    runtime_fingerprint_hash=runtime_hash,
                )
            else:
                issues.append(
                    IllustrationReadinessIssue(
                        code="missing_quality_evidence",
                        message=f"{stage} did not expose an effective graph hash",
                        stage=stage,
                    )
                )
            median_seconds = await self._jobs.median_completed_seconds(
                provider_alias=provider_alias,
                usage="scene_illustration",
            )
            stage_reports.append(
                IllustrationStageReadiness(
                    stage=stage,
                    provider_alias=provider_alias,
                    status=(
                        "passed"
                        if len(issues) == issue_count_before
                        else "blocked"
                    ),
                    summary=probe.summary,
                    template_revision=probe.template_revision,
                    effective_definition_hash=effective_hash,
                    runtime_fingerprint_hash=runtime_hash,
                    queue_running=probe.queue_running,
                    queue_pending=probe.queue_pending,
                    max_concurrency=provider.max_concurrency,
                    median_completed_seconds=median_seconds,
                    fallback_timeout_seconds=provider.timeout_seconds,
                    dependency_checks=probe.dependency_checks,
                )
            )

        quality_fingerprint = ""
        if set(quality_evidence) == set(required_stages):
            quality_fingerprint = compute_image_pipeline_quality_fingerprint(
                config,
                pipeline_alias,
                stage_evidence=quality_evidence,
            )
        quality = IllustrationQualityReadiness(
            status="experimental",
            fingerprint=quality_fingerprint,
            reason="No accepted real-provider quality record matches this fingerprint",
        )
        status: Literal["passed", "blocked"] = "blocked" if issues else "passed"
        digest_payload = {
            "run_id": str(run_key),
            "run_revision": int(run.get("revision") or 0),
            "pipeline_alias": pipeline_alias,
            "pipeline_revision": frozen_revision,
            "required_stages": required_stages,
            "reference_asset_id": str(reference_asset_id),
            "reference_sha256": expected_reference_hash,
            "reference_verified": reference_verified,
            "external_adapter": external_adapter or None,
            "stages": [
                {
                    "stage": item.stage,
                    "provider_alias": item.provider_alias,
                    "status": item.status,
                    "template_revision": item.template_revision,
                    "effective_definition_hash": item.effective_definition_hash,
                    "runtime_fingerprint_hash": item.runtime_fingerprint_hash,
                }
                for item in stage_reports
            ],
            "issues": [
                {"code": issue.code, "stage": issue.stage} for issue in issues
            ],
            "quality": quality.model_dump(mode="json"),
        }
        return IllustrationReadinessReport(
            run_id=str(run_key),
            run_revision=int(run.get("revision") or 0),
            pipeline_alias=pipeline_alias,
            pipeline_revision=frozen_revision,
            status=status,
            readiness_digest=_canonical_hash(digest_payload),
            required_stages=required_stages,
            optional_refine=profile.refine_provider is not None,
            max_provider_calls=len(required_stages),
            reference_verified=reference_verified,
            stages=tuple(stage_reports),
            issues=tuple(issues),
            quality=quality,
        )


illustration_readiness_service = IllustrationReadinessService()


__all__ = [
    "IllustrationQualityReadiness",
    "IllustrationReadinessIssue",
    "IllustrationReadinessReport",
    "IllustrationReadinessService",
    "IllustrationStageReadiness",
    "illustration_readiness_service",
]
