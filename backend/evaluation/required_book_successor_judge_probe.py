"""Closed evidence contract for the successor independent-Judge probe.

The real probe is a separately authorized, synthetic-only Provider call.  This
module does not dispatch it.  It defines the receipt that a later probe runner
must produce and the exact checks the three-chapter readiness performs before
it may trust that receipt.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_CEILING
import hashlib
from importlib import metadata as importlib_metadata
import json
from typing import Any, Literal, Mapping
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.services.generation.required_chapter_review_job import (
    RequiredGenerationPlanSnapshot,
)
from backend.services.generation.independent_outline_review import (
    OutlineReviewSnapshot,
)
from backend.services.generation.required_book_successor_planning import (
    build_required_book_successor_plan_bundle,
)
from backend.services.llm.generation_runtime import (
    GenerationRuntime,
    WorkflowStepTarget,
)


JUDGE_CAPABILITY_PROBE_PROTOCOL = (
    "required-successor-judge-capability-probe-r1"
)
JUDGE_CAPABILITY_PROBE_EXECUTION_PROTOCOL = (
    "required-successor-judge-capability-probe-execution-r1"
)
JUDGE_PROBE_RECEIPT_VALIDITY_SECONDS = 32 * 24 * 60 * 60
_SHA256 = r"^[0-9a-f]{64}$"
_MAX = 2**63 - 1
_PRICE_QUANTUM = Decimal("0.000001")
_MOONSHOT_CHINA_API_HOST = "api.moonshot.cn"
_KIMI_K2_6_MODEL = "kimi-k2.6"
_KIMI_K2_6_REASONING_POLICY = "kimi_k2_6_thinking_enabled"
_KIMI_K2_6_NON_THINKING_POLICY = "kimi_k2_6_thinking_disabled"
_KIMI_K2_6_REASONING_POLICIES = frozenset(
    {
        _KIMI_K2_6_REASONING_POLICY,
        _KIMI_K2_6_NON_THINKING_POLICY,
    }
)
_MOONSHOT_CHINA_STANDARD_RETENTION = (
    "moonshot_china_standard_service"
)

JUDGE_PROBE_COST_AUTHORIZATION_CODE = (
    "successor_judge_probe_cost_upper_bound"
)
JUDGE_PROBE_DISCLOSURE_AUTHORIZATION_CODE = (
    "successor_judge_probe_synthetic_external_disclosure"
)
REQUIRED_JUDGE_PROBE_AUTHORIZATION_CODES = (
    JUDGE_PROBE_COST_AUTHORIZATION_CODE,
    JUDGE_PROBE_DISCLOSURE_AUTHORIZATION_CODE,
)

_SYNTHETIC_SAMPLE_BLUEPRINT = {
    "schema_version": "required_judge_probe_sample.v1",
    "language": "zh-CN",
    "scene_contract_version": "scene_transition_contract.v2",
    "chapter_count": 1,
    "scene_count": 2,
    "contains_user_material": False,
    "expected_semantics": "complete_and_consistent",
    "required_checks": (
        "provider_json_syntax",
        "local_schema",
        "exact_anchor_resolution",
        "local_v4_evidence",
    ),
}

_SYNTHETIC_SCENE_1 = (
    "冷雨停在旧站台外。林舟认出失散多年的苏遥，两人各自取出保留的半枚"
    "蓝色车票，拼合后的编号完全一致，他们终于确认了彼此的身份。"
)
_SYNTHETIC_SCENE_2 = (
    "远处警笛逼近。苏遥把藏在袖口的铜钥匙交给林舟，说明仓库里留有撤离"
    "路线；林舟收好钥匙，决定立刻与她离开站台，共同前往仓库。"
)
_SYNTHETIC_PROSE = f"{_SYNTHETIC_SCENE_1}\n\n{_SYNTHETIC_SCENE_2}"
_SYNTHETIC_AUTHORIZED_CONTEXT = (
    "合成事实：林舟与苏遥幼年失散，各自保存同一张蓝色车票的一半；"
    "铜钥匙可以打开旧站台外的仓库。"
)


def _synthetic_scene(
    *,
    ordinal: int,
    summary: str,
    purpose: str,
    before: str,
    after: str,
) -> dict[str, Any]:
    scene_id = f"scene-{ordinal}"
    return {
        "contract_version": "scene_transition_contract.v2",
        "scene_id": scene_id,
        "summary": summary,
        "purpose": purpose,
        "preconditions": [
            {
                "condition_id": f"{scene_id}-pre-1",
                "description": before,
            }
        ],
        "beats": [
            {
                "beat_id": f"{scene_id}-beat-1",
                "description": summary,
                "expected_transition": purpose,
                "required": True,
            }
        ],
        "postconditions": [
            {
                "condition_id": f"{scene_id}-post-1",
                "description": after,
            }
        ],
        "forbidden_conditions": [],
        "narrative_delta": [
            {
                "delta_id": f"{scene_id}-delta-1",
                "dimension": "relationship" if ordinal == 1 else "goal",
                "before": before,
                "after": after,
            }
        ],
        "event_key": f"probe.scene-{ordinal}",
        "repetition_policy": "forbid",
        "word_budget": {"min": 40, "target": 60, "max": 100},
    }


_SYNTHETIC_OUTLINE = {
    "scene_contract_version": "scene_transition_contract.v2",
    "scenes": [
        _synthetic_scene(
            ordinal=1,
            summary="林舟与苏遥用两枚半票确认彼此身份",
            purpose="完成失散同伴的相认",
            before="两人尚不能确认彼此身份",
            after="两人确认身份并恢复信任",
        ),
        _synthetic_scene(
            ordinal=2,
            summary="苏遥交出仓库钥匙，两人共同离开站台",
            purpose="形成前往仓库的共同目标",
            before="两人尚无共同撤离路线",
            after="两人决定携钥匙共同前往仓库",
        ),
    ],
    "core_conflict": "警笛逼近，两人必须在暴露前确认身份并撤离",
    "ending_hook": "仓库中的撤离路线是否仍然安全",
    "target_word_count": 120,
}


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _jsonable(value.model_dump(mode="python"))
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Mapping):
        return {
            str(key): _jsonable(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def required_judge_probe_digest(value: Any) -> str:
    encoded = json.dumps(
        _jsonable(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def required_judge_generation_plan_digest(
    plan: RequiredGenerationPlanSnapshot,
) -> str:
    if not isinstance(plan, RequiredGenerationPlanSnapshot):
        raise ValueError("required_judge_probe_plan_invalid")
    return required_judge_probe_digest(plan.model_dump(mode="json"))


def required_judge_probe_snapshot() -> OutlineReviewSnapshot:
    """Return the exact synthetic material covered by probe authorization."""

    return OutlineReviewSnapshot.create(
        source_run_id="f" * 24,
        source_run_revision=1,
        source_content_digest=hashlib.sha256(
            _SYNTHETIC_PROSE.encode("utf-8")
        ).hexdigest(),
        prose=_SYNTHETIC_PROSE,
        outline=_SYNTHETIC_OUTLINE,
        authorized_context=_SYNTHETIC_AUTHORIZED_CONTEXT,
        scene_ranges=(
            {
                "scene_id": "scene-1",
                "start": 0,
                "end": len(_SYNTHETIC_SCENE_1),
            },
            {
                "scene_id": "scene-2",
                "start": len(_SYNTHETIC_SCENE_1) + 2,
                "end": len(_SYNTHETIC_PROSE),
            },
        ),
    )


def required_judge_probe_sample_digest() -> str:
    snapshot = required_judge_probe_snapshot()
    return required_judge_probe_digest(
        {
            "blueprint": _SYNTHETIC_SAMPLE_BLUEPRINT,
            "source_run_id": snapshot.source_run_id,
            "source_run_revision": snapshot.source_run_revision,
            "source_content_digest": snapshot.source_content_digest,
            "outline_digest": required_judge_probe_digest(
                json.loads(snapshot.outline_json)
            ),
            "authorized_context_digest": hashlib.sha256(
                snapshot.authorized_context.encode("utf-8")
            ).hexdigest(),
            "view_digest": snapshot.view_digest,
        }
    )


class _Closed(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        hide_input_in_errors=True,
    )


class RequiredJudgeProbePricing(_Closed):
    schema_version: Literal["required_judge_probe_pricing.v1"] = (
        "required_judge_probe_pricing.v1"
    )
    provider_alias: str = Field(min_length=1, max_length=160)
    provider_model: str = Field(min_length=1, max_length=240)
    currency: str = Field(min_length=1, max_length=16)
    input_cache_miss_per_million: Decimal = Field(ge=0)
    output_per_million: Decimal = Field(ge=0)
    basis: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_pricing(self) -> "RequiredJudgeProbePricing":
        if (
            self.provider_alias != self.provider_alias.strip()
            or self.provider_model != self.provider_model.strip()
            or self.currency != self.currency.strip().upper()
            or self.basis != self.basis.strip()
        ):
            raise ValueError("required_judge_probe_pricing_invalid")
        return self


class RequiredJudgeCapabilityProbeAuthorization(_Closed):
    schema_version: Literal[
        "required_judge_capability_probe_authorization.v1"
    ] = "required_judge_capability_probe_authorization.v1"
    protocol_revision: Literal[
        "required-successor-judge-capability-probe-r1"
    ] = JUDGE_CAPABILITY_PROBE_PROTOCOL
    execution_protocol_revision: Literal[
        "required-successor-judge-capability-probe-execution-r1"
    ] = JUDGE_CAPABILITY_PROBE_EXECUTION_PROTOCOL
    contract_digest: str = Field(pattern=_SHA256)
    authorization_revision: int = Field(ge=1, le=_MAX)
    created_at: datetime
    deadline_at: datetime
    generation_plan: RequiredGenerationPlanSnapshot
    writer_model: str = Field(min_length=1, max_length=240)
    provider_type: Literal["openai", "gemini", "claude"]
    provider_base_url: str = Field(min_length=1, max_length=500)
    sdk_package: str = Field(min_length=1, max_length=120)
    sdk_version: str = Field(min_length=1, max_length=120)
    request_reasoning_policy: Literal[
        "provider_default",
        "openai_default_medium",
        "kimi_k2_6_thinking_enabled",
        "kimi_k2_6_thinking_disabled",
        "not_applicable",
    ]
    declared_data_retention_tier: Literal[
        "default",
        "zero_data_retention",
        "modified_abuse_monitoring",
        "paid_service",
        "moonshot_china_standard_service",
    ]
    credential_configured: Literal[True] = True
    review_contract_digest: str = Field(pattern=_SHA256)
    review_input_token_bound: int = Field(ge=1, le=_MAX)
    review_max_response_bytes: int = Field(ge=1, le=_MAX)
    synthetic_sample_digest: str = Field(pattern=_SHA256)
    maximum_provider_attempts: int = Field(ge=1, le=2)
    maximum_input_tokens: int = Field(ge=1, le=_MAX)
    maximum_output_tokens: int = Field(ge=1, le=_MAX)
    maximum_total_tokens: int = Field(ge=2, le=_MAX)
    maximum_serial_seconds: int = Field(ge=1, le=_MAX)
    receipt_validity_seconds: Literal[2764800] = (
        JUDGE_PROBE_RECEIPT_VALIDITY_SECONDS
    )
    pricing: RequiredJudgeProbePricing
    cost_upper_bound: Decimal = Field(ge=0)
    required_authorization_codes: tuple[str, str] = (
        REQUIRED_JUDGE_PROBE_AUTHORIZATION_CODES
    )
    synthetic_material_only: Literal[True] = True
    contains_user_material: Literal[False] = False
    provider_dispatch_allowed_by_readiness_alone: Literal[False] = False

    @model_validator(mode="after")
    def validate_authorization(
        self,
    ) -> "RequiredJudgeCapabilityProbeAuthorization":
        plan = self.generation_plan
        expected_cost = _probe_cost_upper_bound(
            input_tokens=self.maximum_input_tokens,
            output_tokens=self.maximum_output_tokens,
            pricing=self.pricing,
        )
        endpoint = urlsplit(self.provider_base_url)
        expected_sdk = {
            "openai": "openai",
            "gemini": "google-genai",
            "claude": "anthropic",
        }[self.provider_type]
        provider_model = plan.provider_model.strip().lower()
        provider_host = (endpoint.hostname or "").lower()
        moonshot_or_kimi = (
            provider_host in {
                _MOONSHOT_CHINA_API_HOST,
                "api.moonshot.ai",
            }
            or provider_model.startswith("kimi-")
        )
        kimi_reasoning_policy = {
            "enabled": _KIMI_K2_6_REASONING_POLICY,
            "disabled": _KIMI_K2_6_NON_THINKING_POLICY,
        }.get(plan.thinking_mode)
        if moonshot_or_kimi and (
            self.provider_type != "openai"
            or provider_host != _MOONSHOT_CHINA_API_HOST
            or endpoint.port not in {None, 443}
            or endpoint.path.rstrip("/") not in {"", "/v1"}
            or provider_model != _KIMI_K2_6_MODEL
            or plan.structured_output_mode != "json_object"
            or kimi_reasoning_policy is None
            or self.request_reasoning_policy != kimi_reasoning_policy
            or self.declared_data_retention_tier
            != _MOONSHOT_CHINA_STANDARD_RETENTION
        ):
            raise ValueError("required_judge_probe_kimi_contract_invalid")
        if (
            self.created_at.tzinfo is None
            or self.deadline_at.tzinfo is None
            or self.deadline_at
            < self.created_at + timedelta(seconds=self.maximum_serial_seconds)
            or plan.call_kind != "structured"
            or plan.structured_output_mode
            not in {"prompt_json", "json_object"}
            or plan.reviewer_alias is not None
            or self.provider_base_url != self.provider_base_url.rstrip("/")
            or endpoint.scheme != "https"
            or not endpoint.hostname
            or endpoint.username is not None
            or endpoint.password is not None
            or bool(endpoint.query)
            or bool(endpoint.fragment)
            or self.sdk_package != expected_sdk
            or self.sdk_version != self.sdk_version.strip()
            or (
                self.provider_type == "openai"
                and plan.provider_model.lower().startswith("gpt-5.6-")
                and self.request_reasoning_policy
                != "openai_default_medium"
            )
            or (
                self.provider_type == "gemini"
                and self.declared_data_retention_tier != "paid_service"
            )
            or (
                not moonshot_or_kimi
                and (
                    self.request_reasoning_policy
                    in _KIMI_K2_6_REASONING_POLICIES
                    or self.declared_data_retention_tier
                    == _MOONSHOT_CHINA_STANDARD_RETENTION
                )
            )
            or self.maximum_provider_attempts != plan.max_semantic_attempts
            or self.maximum_input_tokens
            != self.review_input_token_bound * plan.max_semantic_attempts
            or self.maximum_output_tokens
            != plan.max_output_tokens * plan.max_semantic_attempts
            or self.maximum_total_tokens
            != self.maximum_input_tokens + self.maximum_output_tokens
            or self.maximum_serial_seconds
            != plan.timeout_seconds * plan.max_semantic_attempts
            or self.pricing.provider_alias != plan.provider_alias
            or self.pricing.provider_model != plan.provider_model
            or self.cost_upper_bound != expected_cost
            or self.required_authorization_codes
            != REQUIRED_JUDGE_PROBE_AUTHORIZATION_CODES
            or self.synthetic_sample_digest
            != required_judge_probe_sample_digest()
        ):
            raise ValueError("required_judge_probe_authorization_invalid")
        identity = self.model_dump(
            mode="python",
            exclude={"contract_digest"},
        )
        if required_judge_probe_digest(identity) != self.contract_digest:
            raise ValueError("required_judge_probe_authorization_changed")
        return self


def _probe_cost_upper_bound(
    *,
    input_tokens: int,
    output_tokens: int,
    pricing: RequiredJudgeProbePricing,
) -> Decimal:
    amount = (
        Decimal(input_tokens) * pricing.input_cache_miss_per_million
        + Decimal(output_tokens) * pricing.output_per_million
    ) / Decimal(1_000_000)
    return amount.quantize(_PRICE_QUANTUM, rounding=ROUND_CEILING)


def _optional_decimal_matches(value: Any, expected: str) -> bool:
    if value is None:
        return True
    if isinstance(value, bool):
        return False
    try:
        return Decimal(str(value)) == Decimal(expected)
    except (InvalidOperation, TypeError, ValueError):
        return False


def _readonly_runtime(config: Mapping[str, Any]) -> GenerationRuntime:
    frozen = deepcopy(dict(config))

    def forbidden_adapter(_alias: str, _timeout: int | None) -> Any:
        raise AssertionError(
            "Judge probe readiness constructed a Provider adapter"
        )

    return GenerationRuntime(
        config_supplier=lambda: deepcopy(frozen),
        adapter_factory=forbidden_adapter,
    )


def build_required_judge_capability_probe_authorization(
    *,
    config: Mapping[str, Any],
    writer_provider_alias: str,
    judge_provider_alias: str,
    pricing: RequiredJudgeProbePricing,
    review_max_response_bytes: int,
    authorization_revision: int,
    created_at: datetime,
    deadline_at: datetime,
    request_reasoning_policy: Literal[
        "provider_default",
        "openai_default_medium",
        "kimi_k2_6_thinking_enabled",
        "kimi_k2_6_thinking_disabled",
        "not_applicable",
    ],
    declared_data_retention_tier: Literal[
        "default",
        "zero_data_retention",
        "modified_abuse_monitoring",
        "paid_service",
        "moonshot_china_standard_service",
    ],
    review_input_token_bound: int | None = None,
    runtime: GenerationRuntime | None = None,
) -> RequiredJudgeCapabilityProbeAuthorization:
    selected_runtime = runtime or _readonly_runtime(config)
    bundle = build_required_book_successor_plan_bundle(
        selected_runtime,
        writer_provider_alias=writer_provider_alias,
        judge_provider_alias=judge_provider_alias,
        state_provider_alias=writer_provider_alias,
        review_input_token_bound=review_input_token_bound,
        review_max_response_bytes=review_max_response_bytes,
    )
    review = bundle.chapter_review.review
    target = review.generation.target
    if not isinstance(target, WorkflowStepTarget):
        raise ValueError("required_judge_probe_target_invalid")
    providers = config.get("llm", {}).get("providers", {})
    provider = (
        providers.get(judge_provider_alias)
        if isinstance(providers, Mapping)
        else None
    )
    if not isinstance(provider, Mapping):
        raise ValueError("required_judge_probe_provider_invalid")
    provider_type = str(provider.get("type") or "openai").strip().lower()
    sdk_package = {
        "openai": "openai",
        "gemini": "google-genai",
        "claude": "anthropic",
    }.get(provider_type)
    base_url = str(provider.get("base_url") or "").strip().rstrip("/")
    endpoint = urlsplit(base_url)
    is_kimi_k2_6 = (
        provider_type == "openai"
        and (endpoint.hostname or "").lower() == _MOONSHOT_CHINA_API_HOST
        and str(provider.get("default_model") or "").strip().lower()
        == _KIMI_K2_6_MODEL
    )
    if is_kimi_k2_6 and not all(
        (
            _optional_decimal_matches(provider.get("temperature"), "1.0"),
            _optional_decimal_matches(provider.get("top_p"), "0.95"),
            _optional_decimal_matches(provider.get("presence_penalty"), "0"),
            _optional_decimal_matches(provider.get("frequency_penalty"), "0"),
        )
    ):
        raise ValueError(
            "required_judge_probe_kimi_request_parameters_invalid"
        )
    if (
        sdk_package is None
        or not base_url
        or not str(provider.get("api_key") or "").strip()
    ):
        raise ValueError("required_judge_probe_provider_invalid")
    try:
        sdk_version = importlib_metadata.version(sdk_package)
    except importlib_metadata.PackageNotFoundError as exc:
        raise ValueError("required_judge_probe_sdk_missing") from exc
    plan = RequiredGenerationPlanSnapshot.freeze(
        review.generation,
        call_kind="structured",
        expected_target=(target.workflow_name, target.step_name),
    )
    identity = {
        "schema_version": (
            "required_judge_capability_probe_authorization.v1"
        ),
        "protocol_revision": JUDGE_CAPABILITY_PROBE_PROTOCOL,
        "execution_protocol_revision": (
            JUDGE_CAPABILITY_PROBE_EXECUTION_PROTOCOL
        ),
        "authorization_revision": authorization_revision,
        "created_at": created_at,
        "deadline_at": deadline_at,
        "generation_plan": plan,
        "writer_model": review.writer_model,
        "provider_type": provider_type,
        "provider_base_url": base_url,
        "sdk_package": sdk_package,
        "sdk_version": sdk_version,
        "request_reasoning_policy": request_reasoning_policy,
        "declared_data_retention_tier": declared_data_retention_tier,
        "credential_configured": True,
        "review_contract_digest": review.contract_digest,
        "review_input_token_bound": review.input_token_bound,
        "review_max_response_bytes": review.max_response_bytes,
        "synthetic_sample_digest": required_judge_probe_sample_digest(),
        "maximum_provider_attempts": plan.max_semantic_attempts,
        "maximum_input_tokens": (
            review.input_token_bound * plan.max_semantic_attempts
        ),
        "maximum_output_tokens": (
            plan.max_output_tokens * plan.max_semantic_attempts
        ),
        "maximum_total_tokens": review.max_total_tokens,
        "maximum_serial_seconds": (
            plan.timeout_seconds * plan.max_semantic_attempts
        ),
        "receipt_validity_seconds": JUDGE_PROBE_RECEIPT_VALIDITY_SECONDS,
        "pricing": pricing,
        "cost_upper_bound": _probe_cost_upper_bound(
            input_tokens=(
                review.input_token_bound * plan.max_semantic_attempts
            ),
            output_tokens=(
                plan.max_output_tokens * plan.max_semantic_attempts
            ),
            pricing=pricing,
        ),
        "required_authorization_codes": (
            REQUIRED_JUDGE_PROBE_AUTHORIZATION_CODES
        ),
        "synthetic_material_only": True,
        "contains_user_material": False,
        "provider_dispatch_allowed_by_readiness_alone": False,
    }
    return RequiredJudgeCapabilityProbeAuthorization(
        **identity,
        contract_digest=required_judge_probe_digest(identity),
    )


def _probe_readiness_issues(
    authorization: RequiredJudgeCapabilityProbeAuthorization,
) -> list[dict[str, Any]]:
    return [
        {
            "code": JUDGE_PROBE_COST_AUTHORIZATION_CODE,
            "level": "warning_requires_ack",
            "details": {
                "currency": authorization.pricing.currency,
                "amount": str(authorization.cost_upper_bound),
                "maximum_provider_attempts": (
                    authorization.maximum_provider_attempts
                ),
                "maximum_tokens": authorization.maximum_total_tokens,
            },
        },
        {
            "code": JUDGE_PROBE_DISCLOSURE_AUTHORIZATION_CODE,
            "level": "warning_requires_ack",
            "details": {
                "provider_alias": (
                    authorization.generation_plan.provider_alias
                ),
                "provider_model": (
                    authorization.generation_plan.provider_model
                ),
                "provider_type": authorization.provider_type,
                "provider_base_url": authorization.provider_base_url,
                "sdk_package": authorization.sdk_package,
                "sdk_version": authorization.sdk_version,
                "request_reasoning_policy": (
                    authorization.request_reasoning_policy
                ),
                "declared_data_retention_tier": (
                    authorization.declared_data_retention_tier
                ),
                "synthetic_sample_digest": (
                    authorization.synthetic_sample_digest
                ),
                "contains_user_material": False,
            },
        },
    ]


def prepare_required_judge_capability_probe_readiness(
    **kwargs: Any,
) -> dict[str, Any]:
    """Build redacted approval material without constructing an Adapter."""

    authorization = build_required_judge_capability_probe_authorization(
        **kwargs
    )
    issues = _probe_readiness_issues(authorization)
    snapshot = {
        "schema_version": "required_judge_capability_probe_readiness.v1",
        "authorization": authorization.model_dump(mode="json"),
        "issues": issues,
        "safety": {
            "provider_calls": 0,
            "database_reads": False,
            "database_writes": False,
            "adapter_constructions": 0,
            "contains_user_material": False,
            "grants_execution_authority": False,
        },
    }
    readiness = {
        **snapshot,
        "status": "warning_requires_ack",
        "digest": required_judge_probe_digest(snapshot),
    }
    validate_required_judge_capability_probe_readiness(readiness)
    return readiness


def validate_required_judge_capability_probe_readiness(
    readiness: Mapping[str, Any],
) -> RequiredJudgeCapabilityProbeAuthorization:
    if not isinstance(readiness, Mapping):
        raise ValueError("required_judge_probe_readiness_invalid")
    try:
        authorization = (
            RequiredJudgeCapabilityProbeAuthorization.model_validate_json(
                json.dumps(
                    readiness.get("authorization"),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("required_judge_probe_readiness_invalid") from exc
    issues = readiness.get("issues")
    safety = readiness.get("safety")
    snapshot = {
        "schema_version": readiness.get("schema_version"),
        "authorization": deepcopy(readiness.get("authorization")),
        "issues": deepcopy(issues),
        "safety": deepcopy(safety),
    }
    if (
        readiness.get("schema_version")
        != "required_judge_capability_probe_readiness.v1"
        or readiness.get("status") != "warning_requires_ack"
        or issues != _probe_readiness_issues(authorization)
        or safety
        != {
            "provider_calls": 0,
            "database_reads": False,
            "database_writes": False,
            "adapter_constructions": 0,
            "contains_user_material": False,
            "grants_execution_authority": False,
        }
        or readiness.get("digest")
        != required_judge_probe_digest(snapshot)
    ):
        raise ValueError("required_judge_probe_readiness_changed")
    return authorization


class RequiredJudgeCapabilityProbeReceipt(_Closed):
    """Redacted proof that the exact independent-review route worked once."""

    schema_version: Literal[
        "required_judge_capability_probe_receipt.v1"
    ] = "required_judge_capability_probe_receipt.v1"
    protocol_revision: Literal[
        "required-successor-judge-capability-probe-r1"
    ] = JUDGE_CAPABILITY_PROBE_PROTOCOL
    receipt_digest: str = Field(pattern=_SHA256)
    probe_readiness_digest: str = Field(pattern=_SHA256)
    probe_report_sha256: str = Field(pattern=_SHA256)
    synthetic_sample_digest: str = Field(pattern=_SHA256)
    provider_alias: str = Field(min_length=1, max_length=160)
    provider_model: str = Field(min_length=1, max_length=240)
    structured_output_mode: Literal["prompt_json", "json_object"]
    config_revision: str = Field(min_length=1, max_length=240)
    capability_snapshot: str = Field(min_length=1, max_length=240)
    generation_plan_digest: str = Field(pattern=_SHA256)
    review_contract_digest: str = Field(pattern=_SHA256)
    completed_at: datetime
    valid_until: datetime
    accounted_attempt_count: int = Field(ge=1, le=2)
    accounted_attempt_ids_digest: str = Field(pattern=_SHA256)
    uncertain_attempt_count: Literal[0] = 0
    input_tokens: int = Field(ge=1, le=2**63 - 1)
    output_tokens: int = Field(ge=1, le=2**63 - 1)
    total_tokens: int = Field(ge=2, le=2**63 - 1)
    finish_reason: Literal["stop"] = "stop"
    usage_complete: Literal[True] = True
    synthetic_material_only: Literal[True] = True
    contains_user_material: Literal[False] = False
    schema_valid: Literal[True] = True
    anchor_resolution_valid: Literal[True] = True
    local_evidence_valid: Literal[True] = True

    @model_validator(mode="after")
    def validate_receipt(self) -> "RequiredJudgeCapabilityProbeReceipt":
        if (
            self.completed_at.tzinfo is None
            or self.valid_until.tzinfo is None
            or self.valid_until <= self.completed_at
            or self.total_tokens != self.input_tokens + self.output_tokens
            or self.provider_alias != self.provider_alias.strip()
            or self.provider_model != self.provider_model.strip()
        ):
            raise ValueError("required_judge_probe_receipt_invalid")
        identity = self.model_dump(
            mode="python",
            exclude={"receipt_digest"},
        )
        if required_judge_probe_digest(identity) != self.receipt_digest:
            raise ValueError("required_judge_probe_receipt_changed")
        return self

    @classmethod
    def create(
        cls,
        *,
        probe_readiness_digest: str,
        probe_report_sha256: str,
        generation_plan: RequiredGenerationPlanSnapshot,
        review_contract_digest: str,
        completed_at: datetime,
        valid_until: datetime,
        accounted_attempt_count: int,
        accounted_attempt_ids_digest: str,
        input_tokens: int,
        output_tokens: int,
    ) -> "RequiredJudgeCapabilityProbeReceipt":
        identity = {
            "schema_version": "required_judge_capability_probe_receipt.v1",
            "protocol_revision": JUDGE_CAPABILITY_PROBE_PROTOCOL,
            "probe_readiness_digest": probe_readiness_digest,
            "probe_report_sha256": probe_report_sha256,
            "synthetic_sample_digest": required_judge_probe_sample_digest(),
            "provider_alias": generation_plan.provider_alias,
            "provider_model": generation_plan.provider_model,
            "structured_output_mode": (
                generation_plan.structured_output_mode
            ),
            "config_revision": generation_plan.config_revision,
            "capability_snapshot": generation_plan.capability_snapshot,
            "generation_plan_digest": (
                required_judge_generation_plan_digest(generation_plan)
            ),
            "review_contract_digest": review_contract_digest,
            "completed_at": completed_at,
            "valid_until": valid_until,
            "accounted_attempt_count": accounted_attempt_count,
            "accounted_attempt_ids_digest": accounted_attempt_ids_digest,
            "uncertain_attempt_count": 0,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "finish_reason": "stop",
            "usage_complete": True,
            "synthetic_material_only": True,
            "contains_user_material": False,
            "schema_valid": True,
            "anchor_resolution_valid": True,
            "local_evidence_valid": True,
        }
        return cls(
            **identity,
            receipt_digest=required_judge_probe_digest(identity),
        )


def parse_required_judge_capability_probe_receipt(
    value: Any,
) -> RequiredJudgeCapabilityProbeReceipt:
    try:
        return RequiredJudgeCapabilityProbeReceipt.model_validate_json(
            json.dumps(
                _jsonable(value),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("required_judge_probe_receipt_invalid") from exc


def validate_required_judge_capability_probe_receipt(
    value: Any,
    *,
    generation_plan: RequiredGenerationPlanSnapshot,
    review_contract_digest: str,
    review_input_token_bound: int,
    readiness_created_at: datetime,
    readiness_deadline_at: datetime,
) -> RequiredJudgeCapabilityProbeReceipt:
    receipt = parse_required_judge_capability_probe_receipt(value)
    plan = generation_plan
    if (
        not isinstance(plan, RequiredGenerationPlanSnapshot)
        or plan.call_kind != "structured"
        or plan.structured_output_mode
        not in {"prompt_json", "json_object"}
        or receipt.provider_alias != plan.provider_alias
        or receipt.provider_model != plan.provider_model
        or receipt.structured_output_mode != plan.structured_output_mode
        or receipt.config_revision != plan.config_revision
        or receipt.capability_snapshot != plan.capability_snapshot
        or receipt.generation_plan_digest
        != required_judge_generation_plan_digest(plan)
        or receipt.review_contract_digest != review_contract_digest
        or receipt.synthetic_sample_digest
        != required_judge_probe_sample_digest()
        or receipt.accounted_attempt_count > plan.max_semantic_attempts
        or receipt.input_tokens
        > review_input_token_bound * receipt.accounted_attempt_count
        or receipt.output_tokens
        > plan.max_output_tokens * receipt.accounted_attempt_count
        or readiness_created_at.tzinfo is None
        or readiness_deadline_at.tzinfo is None
        or receipt.completed_at > readiness_created_at
        or readiness_deadline_at > receipt.valid_until
    ):
        raise ValueError("required_judge_probe_binding_changed")
    return receipt


__all__ = [
    "JUDGE_CAPABILITY_PROBE_PROTOCOL",
    "JUDGE_CAPABILITY_PROBE_EXECUTION_PROTOCOL",
    "JUDGE_PROBE_RECEIPT_VALIDITY_SECONDS",
    "JUDGE_PROBE_COST_AUTHORIZATION_CODE",
    "JUDGE_PROBE_DISCLOSURE_AUTHORIZATION_CODE",
    "REQUIRED_JUDGE_PROBE_AUTHORIZATION_CODES",
    "RequiredJudgeCapabilityProbeAuthorization",
    "RequiredJudgeCapabilityProbeReceipt",
    "RequiredJudgeProbePricing",
    "build_required_judge_capability_probe_authorization",
    "parse_required_judge_capability_probe_receipt",
    "prepare_required_judge_capability_probe_readiness",
    "required_judge_generation_plan_digest",
    "required_judge_probe_digest",
    "required_judge_probe_sample_digest",
    "required_judge_probe_snapshot",
    "validate_required_judge_capability_probe_readiness",
    "validate_required_judge_capability_probe_receipt",
]
