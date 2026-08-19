"""统一的 Provider 目标解析、结构化生成、取消和逐 attempt 用量协议。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import re
from typing import Any, Callable, Literal, Mapping, Protocol, Union
from urllib.parse import urlparse
from uuid import uuid4

from pydantic import BaseModel, ValidationError

from backend.llm.exceptions import (
    LLMSchemaUnsupportedError,
    LLMStructuredValidationError,
)
from backend.llm.models import TokenUsage
from backend.llm.stream_terminal import FinishReason, normalize_finish_reason
from backend.config.workflow_catalog import get_workflow_step_definition
from backend.services.generation.prose_token_bounds import (
    conservative_prompt_input_bound,
    conservative_runtime_token_bound,
    structured_schema_request_payload,
)


class StructuredOutputMode(str, Enum):
    PROMPT_JSON = "prompt_json"
    JSON_OBJECT = "json_object"
    SCHEMA_ENFORCED = "schema_enforced"


STRUCTURED_REQUEST_BUDGET_PROTOCOL = "structured_request_budget.v2"


def _redacted_config_revision(
    config: dict[str, Any],
    *,
    secret_revision_state: dict[str, Any] | None = None,
) -> str:
    """生成不直接依赖密钥原值、仍可结合密钥世代失效的配置摘要。"""
    editable = json.loads(json.dumps(config))
    editable.pop("revision", None)
    providers = editable.get("llm", {}).get("providers", {})
    if isinstance(providers, dict):
        for provider in providers.values():
            if not isinstance(provider, dict):
                continue
            provider.pop("_capability_profile", None)
            provider["api_key"] = bool(provider.get("api_key"))
    payload: dict[str, Any] = {"editable": editable}
    if secret_revision_state is not None:
        payload["secret_revision_state"] = secret_revision_state
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@dataclass(frozen=True)
class WorkflowStepTarget:
    workflow_name: str
    step_name: str


@dataclass(frozen=True)
class ExplicitProviderTarget:
    provider_alias: str
    timeout_seconds: int | None = None


GenerationTarget = Union[WorkflowStepTarget, ExplicitProviderTarget]


@dataclass(frozen=True)
class ResolvedProvider:
    alias: str
    timeout_seconds: int | None
    config: dict[str, Any]


@dataclass(frozen=True)
class PromptPlan:
    native_schema_prompt: str
    prompt_json_prompt: str


@dataclass(frozen=True)
class GenerationPlan:
    target: GenerationTarget
    provider_alias: str
    timeout_seconds: int | None
    mode: StructuredOutputMode
    reviewer_alias: str | None
    config_revision: str
    capability_snapshot: str
    max_semantic_attempts: int
    provider_model: str = ""
    max_output_tokens: int | None = None
    max_context_tokens: int | None = None
    thinking_mode: Literal["enabled", "disabled"] | None = None


class StaleGenerationPlan(RuntimeError):
    """计划生成后配置或能力快照发生变化。"""


class UnsupportedStructuredMode(LLMSchemaUnsupportedError):
    """Adapter 明确报告当前结构化模式不受支持，可安全降级。"""


class ConservativeGenerationBoundExceeded(ValueError):
    """A nested structured call would exceed its caller-frozen reservation."""

    provider_request_not_dispatched = True


@dataclass(frozen=True)
class AttemptUsage:
    attempt_id: str
    provider_alias: str
    phase: str
    usage: TokenUsage
    state: str = "accounted"


class AttemptScope(Protocol):
    async def claim(self, provider_alias: str, phase: str) -> str: ...
    async def account(self, attempt_id: str, usage: TokenUsage) -> None: ...
    async def mark_uncertain(self, attempt_id: str, reason: str) -> None: ...


class InMemoryAttemptScope:
    """单次交互使用的幂等 attempt ledger。"""

    def __init__(self) -> None:
        self._claimed: dict[str, tuple[str, str]] = {}
        self._accounted: dict[str, AttemptUsage] = {}
        self._uncertain: set[str] = set()

    @property
    def attempts(self) -> tuple[AttemptUsage, ...]:
        return tuple(self._accounted.values())

    @property
    def claimed_attempt_ids(self) -> tuple[str, ...]:
        return tuple(self._claimed)

    @property
    def uncertain_attempt_ids(self) -> tuple[str, ...]:
        return tuple(self._uncertain)

    async def claim(self, provider_alias: str, phase: str) -> str:
        attempt_id = uuid4().hex
        self._claimed[attempt_id] = (provider_alias, phase)
        return attempt_id

    async def account(self, attempt_id: str, usage: TokenUsage) -> None:
        if attempt_id in self._accounted:
            return
        provider_alias, phase = self._claimed[attempt_id]
        self._accounted[attempt_id] = AttemptUsage(
            attempt_id=attempt_id,
            provider_alias=provider_alias,
            phase=phase,
            usage=usage.model_copy(),
        )

    async def mark_uncertain(self, attempt_id: str, reason: str) -> None:
        self._uncertain.add(attempt_id)


@dataclass(frozen=True)
class StructuredGenerationResult:
    value: BaseModel
    usage: TokenUsage
    attempts: tuple[AttemptUsage, ...]
    plan: GenerationPlan
    finish_reason: FinishReason
    raw_finish_reason: str


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _mode_for_provider(provider: dict[str, Any]) -> StructuredOutputMode:
    profile = provider.get("_capability_profile")
    cached_mode = profile.get("structured_output") if isinstance(profile, dict) else None
    configured = str(cached_mode or provider.get("structured_output") or "").strip().lower()
    if configured:
        try:
            return StructuredOutputMode(configured)
        except ValueError as error:
            raise ValueError(f"Unknown structured_output mode: {configured}") from error
    return StructuredOutputMode.PROMPT_JSON


def _thinking_mode_for(
    target: GenerationTarget,
    resolved: ResolvedProvider,
) -> Literal["enabled", "disabled"] | None:
    if not isinstance(target, WorkflowStepTarget):
        return None
    definition = get_workflow_step_definition(
        target.workflow_name,
        target.step_name,
    )
    requested = definition.thinking_mode if definition is not None else None
    if requested is None:
        return None
    provider_type = str(resolved.config.get("type") or "openai").strip().lower()
    model = str(resolved.config.get("default_model") or "").strip().lower()
    host = (
        urlparse(str(resolved.config.get("base_url") or "")).hostname or ""
    ).lower()
    if (
        provider_type == "openai"
        and host == "api.deepseek.com"
        and model.startswith("deepseek-v4-")
    ):
        return requested
    return None


class ProviderCatalog:
    """解析显式目标或 workflow→step→global 三层 Provider。"""

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        llm = config.get("llm")
        if not isinstance(llm, dict):
            raise ValueError("llm config is missing")
        self.llm = llm
        providers = llm.get("providers")
        self.providers = providers if isinstance(providers, dict) else {}

    def _require(self, alias: str, layer: str, timeout: int | None) -> ResolvedProvider:
        provider = self.providers.get(alias)
        if not isinstance(provider, dict):
            raise ValueError(f"Provider '{alias}' referenced by {layer} does not exist")
        if not provider.get("enabled"):
            raise ValueError(f"Provider '{alias}' referenced by {layer} is disabled")
        return ResolvedProvider(alias=alias, timeout_seconds=timeout, config=provider)

    def resolve(self, target: GenerationTarget) -> ResolvedProvider:
        if isinstance(target, ExplicitProviderTarget):
            alias = target.provider_alias.strip()
            if not alias:
                raise ValueError("Explicit Provider target must not be empty")
            return self._require(alias, "explicit target", target.timeout_seconds)

        workflows = self.llm.get("workflows")
        workflows = workflows if isinstance(workflows, dict) else {}
        workflow = workflows.get(target.workflow_name)
        workflow = workflow if isinstance(workflow, dict) else {}
        steps = workflow.get("steps")
        steps = steps if isinstance(steps, dict) else {}
        step = steps.get(target.step_name)
        step = step if isinstance(step, dict) else {}
        timeout = _positive_int(step.get("timeout_seconds"))

        step_alias = str(step.get("provider") or "").strip()
        if step_alias:
            return self._require(step_alias, f"workflow step {target.workflow_name}.{target.step_name}", timeout)
        workflow_alias = str(workflow.get("default_provider") or "").strip()
        if workflow_alias:
            return self._require(workflow_alias, f"workflow {target.workflow_name}", timeout)
        global_alias = str(self.llm.get("default_provider") or "").strip()
        if not global_alias:
            raise ValueError(f"No Provider configured for {target.workflow_name}.{target.step_name}")
        return self._require(global_alias, "llm.default_provider", timeout)


_FENCED_JSON = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.IGNORECASE | re.DOTALL)


def _parse_structured_text(raw: str, schema: type[BaseModel]) -> BaseModel:
    match = _FENCED_JSON.match(raw)
    candidate = match.group(1) if match else raw
    return schema.model_validate(json.loads(candidate))


def _add_usage(items: tuple[AttemptUsage, ...]) -> TokenUsage:
    return TokenUsage(
        input_tokens=sum(item.usage.input_tokens for item in items),
        output_tokens=sum(item.usage.output_tokens for item in items),
        total_tokens=sum(item.usage.total_tokens for item in items),
    )


def _usage_snapshot(value: Any) -> TokenUsage:
    """Copy one adapter usage projection without retaining mutable state."""
    if isinstance(value, TokenUsage):
        return value.model_copy()
    if hasattr(value, "model_dump"):
        return TokenUsage.model_validate(value.model_dump())
    if isinstance(value, Mapping):
        return TokenUsage.model_validate(dict(value))
    return TokenUsage()


def _usage_delta(before: TokenUsage, after: TokenUsage) -> TokenUsage:
    """Return only usage produced after the current paid-attempt boundary."""
    return TokenUsage(
        input_tokens=max(0, after.input_tokens - before.input_tokens),
        output_tokens=max(0, after.output_tokens - before.output_tokens),
        total_tokens=max(0, after.total_tokens - before.total_tokens),
    )


def _usage_has_any_value(usage: TokenUsage) -> bool:
    return bool(
        usage.total_tokens or usage.input_tokens or usage.output_tokens
    )


def _usage_is_complete(usage: TokenUsage) -> bool:
    """A zero component is indistinguishable from an omitted Provider field."""
    return bool(
        usage.input_tokens > 0
        and usage.output_tokens > 0
        and usage.total_tokens >= usage.input_tokens + usage.output_tokens
    )


def _conservative_attempt_usage(
    usage: TokenUsage,
    conservative_tokens: int | None,
) -> TokenUsage:
    """Never let a partial Provider receipt understate a frozen attempt."""
    if _usage_is_complete(usage) or conservative_tokens is None:
        return usage
    return TokenUsage(
        input_tokens=max(0, usage.input_tokens),
        output_tokens=max(0, usage.output_tokens),
        total_tokens=max(
            max(0, usage.total_tokens),
            max(0, usage.input_tokens) + max(0, usage.output_tokens),
            int(conservative_tokens),
        ),
    )


class GenerationRuntime:
    """结构化生成的唯一执行入口；调用前计划、每次付费前复核。"""

    def __init__(
        self,
        *,
        config_supplier: Callable[[], dict[str, Any]],
        adapter_factory: Callable[[str, int | None], Any],
        attempt_scope: AttemptScope | None = None,
    ) -> None:
        self._config_supplier = config_supplier
        self._adapter_factory = adapter_factory
        self._attempt_scope = attempt_scope or InMemoryAttemptScope()
        self._last_finish_reason: FinishReason = "unreported"
        self._last_raw_finish_reason = "unreported"

    @property
    def attempts(self) -> tuple[AttemptUsage, ...]:
        return tuple(getattr(self._attempt_scope, "attempts", ()))

    @property
    def claimed_attempt_count(self) -> int:
        claimed = getattr(self._attempt_scope, "claimed_attempt_ids", None)
        if claimed is not None:
            return len(claimed)
        return len(self.attempts) + len(
            getattr(self._attempt_scope, "uncertain_attempt_ids", ())
        )

    @property
    def uncertain_attempt_count(self) -> int:
        return len(getattr(self._attempt_scope, "uncertain_attempt_ids", ()))

    @property
    def usage(self) -> TokenUsage:
        return _add_usage(self.attempts)

    @property
    def last_finish_reason(self) -> FinishReason:
        return self._last_finish_reason

    @property
    def last_raw_finish_reason(self) -> str:
        return self._last_raw_finish_reason

    @staticmethod
    def _revision(config: dict[str, Any]) -> str:
        explicit = str(config.get("revision") or "")
        if explicit:
            return explicit
        # 自定义/测试 Runtime 没有 SecretVersionStore；fallback 只使用脱敏配置。
        # 生产 create_generation_runtime 会注入带私有 HMAC 密钥世代的显式 revision。
        return _redacted_config_revision(config)

    @staticmethod
    def _capability_snapshot(config: dict[str, Any]) -> str:
        providers = config.get("llm", {}).get("providers", {})
        payload = {
            alias: {
                "enabled": provider.get("enabled"),
                "structured_output": provider.get("structured_output"),
                "default_model": provider.get("default_model"),
                "max_tokens": provider.get("max_tokens"),
                "cached": provider.get("_capability_profile"),
            }
            for alias, provider in providers.items()
            if isinstance(provider, dict)
        } if isinstance(providers, dict) else {}
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def plan_structured(self, target: GenerationTarget) -> GenerationPlan:
        config = self._config_supplier()
        resolved = ProviderCatalog(config).resolve(target)
        llm = config.get("llm", {})
        policy = llm.get("format_review") if isinstance(llm, dict) else None
        reviewer: str | None = None
        if isinstance(policy, dict) and policy.get("mode") == "provider":
            reviewer = str(policy.get("provider_alias") or "").strip() or None
            if reviewer:
                ProviderCatalog(config).resolve(ExplicitProviderTarget(reviewer))
        elif isinstance(policy, dict) and policy.get("mode") == "auto":
            candidates = [
                alias for alias, provider in ProviderCatalog(config).providers.items()
                if isinstance(provider, dict)
                and provider.get("enabled")
                and _mode_for_provider(provider) == StructuredOutputMode.SCHEMA_ENFORCED
            ]
            if not candidates:
                raise ValueError("Auto format reviewer has no eligible schema-enforced Provider")
            reviewer = sorted(candidates)[0]
        base_attempts = 3 if _mode_for_provider(resolved.config) == StructuredOutputMode.SCHEMA_ENFORCED else 2
        return GenerationPlan(
            target=target,
            provider_alias=resolved.alias,
            timeout_seconds=resolved.timeout_seconds,
            mode=_mode_for_provider(resolved.config),
            reviewer_alias=reviewer,
            config_revision=self._revision(config),
            capability_snapshot=self._capability_snapshot(config),
            max_semantic_attempts=base_attempts + (1 if reviewer else 0),
            provider_model=str(resolved.config.get("default_model") or ""),
            max_output_tokens=_positive_int(resolved.config.get("max_tokens")),
            max_context_tokens=_positive_int(resolved.config.get("max_context_tokens")),
        )

    def plan_text(self, target: GenerationTarget) -> GenerationPlan:
        """规划纯文本调用；复用相同目标解析与过期校验，不启用 reviewer。"""
        config = self._config_supplier()
        resolved = ProviderCatalog(config).resolve(target)
        return GenerationPlan(
            target=target,
            provider_alias=resolved.alias,
            timeout_seconds=resolved.timeout_seconds,
            mode=_mode_for_provider(resolved.config),
            reviewer_alias=None,
            config_revision=self._revision(config),
            capability_snapshot=self._capability_snapshot(config),
            max_semantic_attempts=1,
            provider_model=str(resolved.config.get("default_model") or ""),
            max_output_tokens=_positive_int(resolved.config.get("max_tokens")),
            max_context_tokens=_positive_int(resolved.config.get("max_context_tokens")),
            thinking_mode=_thinking_mode_for(target, resolved),
        )

    def _validate_plan(self, plan: GenerationPlan) -> None:
        current = self._config_supplier()
        if self._revision(current) != plan.config_revision:
            raise StaleGenerationPlan("Configuration changed after generation planning")
        if self._capability_snapshot(current) != plan.capability_snapshot:
            raise StaleGenerationPlan("Provider capabilities changed after generation planning")

    @staticmethod
    def _conservative_token_bound(
        plan: GenerationPlan,
        prompt: str,
        gen_kwargs: Mapping[str, Any],
        *,
        additional_request_payload: str = "",
    ) -> int | None:
        output_limit = _positive_int(gen_kwargs.get("max_tokens"))
        if output_limit is None:
            output_limit = plan.max_output_tokens
        return conservative_runtime_token_bound(
            output_token_bound=output_limit,
            prompt=prompt,
            system_prompt=str(gen_kwargs.get("system_prompt") or ""),
            additional_request_payload=additional_request_payload,
        )

    async def _claim_paid_attempt(
        self,
        provider_alias: str,
        phase: str,
        conservative_tokens: int | None,
    ) -> str:
        claim_with_budget = getattr(self._attempt_scope, "claim_with_budget", None)
        if callable(claim_with_budget):
            return await claim_with_budget(
                provider_alias,
                phase,
                conservative_tokens,
            )
        return await self._attempt_scope.claim(provider_alias, phase)

    async def _release_pre_dispatch(
        self,
        attempt_id: str,
        reason: str,
    ) -> None:
        release = getattr(self._attempt_scope, "release_pre_dispatch", None)
        if callable(release):
            await release(attempt_id, reason)

    async def _paid_call(
        self,
        plan: GenerationPlan,
        provider: str,
        phase: str,
        adapter: Any,
        call: Callable[[], Any],
        conservative_tokens: int | None,
    ) -> Any:
        self._validate_plan(plan)
        attempt_id = await self._claim_paid_attempt(
            provider,
            phase,
            conservative_tokens,
        )
        total_before = _usage_snapshot(getattr(adapter, "total_usage", None))
        try:
            value = await call()
        except asyncio.CancelledError:
            await self._attempt_scope.mark_uncertain(attempt_id, "request cancelled after dispatch")
            raise
        except Exception as exc:
            if bool(getattr(exc, "provider_request_not_dispatched", False)):
                await self._release_pre_dispatch(attempt_id, str(exc))
                raise
            total_after = _usage_snapshot(
                getattr(adapter, "total_usage", None)
            )
            usage = _usage_delta(total_before, total_after)
            response_is_known = isinstance(
                exc,
                (LLMSchemaUnsupportedError, LLMStructuredValidationError),
            )
            if response_is_known and not _usage_has_any_value(usage):
                # These exceptions are raised only after a concrete Provider
                # response. Generic timeout/network errors deliberately may
                # not reuse a previous call's last_usage projection.
                usage = _usage_snapshot(getattr(adapter, "last_usage", None))
            if _usage_has_any_value(usage) or response_is_known:
                await self._attempt_scope.account(
                    attempt_id,
                    _conservative_attempt_usage(
                        usage,
                        conservative_tokens,
                    ),
                )
            else:
                await self._attempt_scope.mark_uncertain(attempt_id, "request failed without usage")
            raise
        total_after = _usage_snapshot(getattr(adapter, "total_usage", None))
        usage = _usage_delta(total_before, total_after)
        if not _usage_has_any_value(usage):
            usage = _usage_snapshot(getattr(adapter, "last_usage", None))
        await self._attempt_scope.account(
            attempt_id,
            _conservative_attempt_usage(usage, conservative_tokens),
        )
        return value

    async def generate_structured(
        self,
        plan: GenerationPlan,
        schema: type[BaseModel],
        prompts: PromptPlan,
        *,
        max_conservative_input_tokens: int | None = None,
        max_conservative_total_tokens: int | None = None,
        **gen_kwargs: Any,
    ) -> StructuredGenerationResult:
        attempt_offset = len(self.attempts)
        adapter = self._adapter_factory(plan.provider_alias, plan.timeout_seconds)
        terminal_adapter = adapter
        reserved_conservative_tokens = 0
        request_kwargs = dict(gen_kwargs)
        effective_output_tokens = _positive_int(
            request_kwargs.get("max_tokens")
        )
        if effective_output_tokens is None:
            effective_output_tokens = plan.max_output_tokens
            if effective_output_tokens is not None:
                # A logical structured call has one frozen output ceiling.
                # Explicitly pass it so a reviewer with a larger configured
                # default cannot silently expand this call's authorization.
                request_kwargs["max_tokens"] = effective_output_tokens
        if (
            max_conservative_input_tokens is None
            and plan.max_context_tokens is not None
        ):
            max_conservative_input_tokens = int(plan.max_context_tokens)
        if (
            max_conservative_total_tokens is None
            and max_conservative_input_tokens is not None
            and effective_output_tokens is not None
        ):
            max_conservative_total_tokens = int(plan.max_semantic_attempts) * (
                int(max_conservative_input_tokens)
                + int(effective_output_tokens)
            )

        schema_request_payload = structured_schema_request_payload(schema)

        def bounded_reservation(
            prompt: str,
            *,
            includes_native_schema: bool = False,
        ) -> int | None:
            nonlocal reserved_conservative_tokens
            additional_request_payload = (
                schema_request_payload if includes_native_schema else ""
            )
            input_tokens = conservative_prompt_input_bound(
                prompt=prompt,
                system_prompt=str(request_kwargs.get("system_prompt") or ""),
                additional_request_payload=additional_request_payload,
            )
            if (
                max_conservative_input_tokens is not None
                and input_tokens > int(max_conservative_input_tokens)
            ):
                raise ConservativeGenerationBoundExceeded(
                    "structured repair input exceeds the frozen per-attempt bound"
                )
            bound = self._conservative_token_bound(
                plan,
                prompt,
                request_kwargs,
                additional_request_payload=additional_request_payload,
            )
            if bound is None and max_conservative_total_tokens is not None:
                raise ConservativeGenerationBoundExceeded(
                    "structured call has no conservative output bound"
                )
            if bound is not None:
                next_total = reserved_conservative_tokens + int(bound)
                if (
                    max_conservative_total_tokens is not None
                    and next_total > int(max_conservative_total_tokens)
                ):
                    raise ConservativeGenerationBoundExceeded(
                        "structured repair attempts exceed the frozen total bound"
                    )
                reserved_conservative_tokens = next_total
            return bound

        async def primary_call() -> Any:
            if plan.mode == StructuredOutputMode.SCHEMA_ENFORCED:
                return await adapter.generate_structured(
                    prompts.native_schema_prompt,
                    schema,
                    **request_kwargs,
                )
            if plan.mode == StructuredOutputMode.JSON_OBJECT and hasattr(adapter, "generate_json_object"):
                return await adapter.generate_json_object(
                    prompts.prompt_json_prompt,
                    **request_kwargs,
                )
            return await adapter.generate_text(
                prompts.prompt_json_prompt,
                **request_kwargs,
            )

        try:
            primary_prompt = (
                prompts.native_schema_prompt
                if plan.mode == StructuredOutputMode.SCHEMA_ENFORCED
                else prompts.prompt_json_prompt
            )
            produced = await self._paid_call(
                plan, plan.provider_alias, "primary", adapter, primary_call,
                bounded_reservation(
                    primary_prompt,
                    includes_native_schema=(
                        plan.mode == StructuredOutputMode.SCHEMA_ENFORCED
                    ),
                ),
            )
        except LLMStructuredValidationError as error:
            # 调用已产生可计费用量；保留原始内容，进入同 Provider 的唯一纠错尝试。
            produced = error.raw_output
        except (LLMSchemaUnsupportedError, UnsupportedStructuredMode):
            if plan.mode != StructuredOutputMode.SCHEMA_ENFORCED:
                raise

            async def fallback_call() -> Any:
                return await adapter.generate_text(
                    prompts.prompt_json_prompt,
                    **request_kwargs,
                )

            produced = await self._paid_call(
                plan,
                plan.provider_alias,
                "schema_fallback",
                adapter,
                fallback_call,
                bounded_reservation(prompts.prompt_json_prompt),
            )
        try:
            value = produced if isinstance(produced, BaseModel) else _parse_structured_text(str(produced), schema)
        except (ValidationError, ValueError, json.JSONDecodeError) as first_error:
            repair_prompt = (
                "Repair the following output into valid JSON matching this JSON Schema. "
                "Return JSON only.\nSchema:\n"
                f"{json.dumps(schema.model_json_schema(), ensure_ascii=False)}\nOutput:\n{produced}"
            )

            async def repair_call() -> Any:
                return await adapter.generate_text(
                    repair_prompt,
                    **request_kwargs,
                )

            repaired = await self._paid_call(
                plan, plan.provider_alias, "repair", adapter, repair_call,
                bounded_reservation(repair_prompt),
            )
            try:
                value = _parse_structured_text(str(repaired), schema)
            except (ValidationError, ValueError, json.JSONDecodeError):
                if not plan.reviewer_alias:
                    raise ValueError(
                        f"Structured output validation failed after one same-Provider repair: {first_error}"
                    ) from first_error
                reviewer = self._adapter_factory(plan.reviewer_alias, None)
                terminal_adapter = reviewer

                async def review_call() -> Any:
                    return await reviewer.generate_structured(
                        repair_prompt,
                        schema,
                        **request_kwargs,
                    )

                value = await self._paid_call(
                    plan, plan.reviewer_alias, "reviewer", reviewer, review_call,
                    bounded_reservation(
                        repair_prompt,
                        includes_native_schema=True,
                    ),
                )

        attempts = self.attempts[attempt_offset:]
        self._last_finish_reason = normalize_finish_reason(
            getattr(terminal_adapter, "last_finish_reason", None)
        )
        self._last_raw_finish_reason = str(
            getattr(terminal_adapter, "last_raw_finish_reason", None)
            or self._last_finish_reason
        )
        return StructuredGenerationResult(
            value=value,
            usage=_add_usage(attempts),
            attempts=attempts,
            plan=plan,
            finish_reason=self._last_finish_reason,
            raw_finish_reason=self._last_raw_finish_reason,
        )

    async def stream_text(
        self,
        plan: GenerationPlan,
        prompt: str,
        **gen_kwargs: Any,
    ):
        """流式纯文本入口；取消直接传播，流耗尽后立即记账。"""
        self._validate_plan(plan)
        adapter = self._adapter_factory(plan.provider_alias, plan.timeout_seconds)
        request_kwargs = dict(gen_kwargs)
        if plan.thinking_mode is not None:
            metadata = dict(request_kwargs.get("metadata") or {})
            configured = metadata.get("thinking_mode")
            if configured is not None and configured != plan.thinking_mode:
                raise ValueError(
                    "thinking_mode conflicts with the immutable GenerationPlan"
                )
            metadata["thinking_mode"] = plan.thinking_mode
            request_kwargs["metadata"] = metadata
        self._last_finish_reason = "unreported"
        self._last_raw_finish_reason = "unreported"
        attempt_id = await self._claim_paid_attempt(
            plan.provider_alias,
            "text",
            self._conservative_token_bound(plan, prompt, request_kwargs),
        )
        try:
            async for chunk in adapter.stream_text(prompt, **request_kwargs):
                yield chunk
        except asyncio.CancelledError:
            self._last_finish_reason = "cancelled"
            self._last_raw_finish_reason = "cancelled"
            await self._attempt_scope.mark_uncertain(attempt_id, "stream cancelled after dispatch")
            raise
        except Exception as exc:
            if bool(getattr(exc, "provider_request_not_dispatched", False)):
                await self._release_pre_dispatch(attempt_id, str(exc))
                raise
            self._last_finish_reason = "error"
            self._last_raw_finish_reason = "error"
            usage = getattr(adapter, "last_usage", None) or TokenUsage()
            if usage.total_tokens or usage.input_tokens or usage.output_tokens:
                await self._attempt_scope.account(attempt_id, usage)
            else:
                await self._attempt_scope.mark_uncertain(attempt_id, "stream failed without usage")
            raise
        usage = getattr(adapter, "last_usage", None) or TokenUsage()
        await self._attempt_scope.account(attempt_id, usage)
        self._last_finish_reason = normalize_finish_reason(
            getattr(adapter, "last_finish_reason", None)
        )
        self._last_raw_finish_reason = str(
            getattr(adapter, "last_raw_finish_reason", None)
            or self._last_finish_reason
        )


def create_generation_runtime(
    attempt_scope: AttemptScope | None = None,
    *,
    max_provider_retries: int | None = None,
) -> GenerationRuntime:
    """使用当前应用配置和 LLMService 创建一次工作流专用运行时。"""
    from copy import deepcopy
    from pathlib import Path

    from backend.config.capability_cache import FileCapabilityCacheStore
    from backend.config.config import CONFIG_PATH, get_all_config
    from backend.config.lifecycle import FileSecretVersionStore
    from backend.services.llm.llm_service import LLMService

    config_path = Path(CONFIG_PATH)
    secret_store = FileSecretVersionStore(
        config_path.with_name(".config-secret-versions.json")
    )
    capability_store = FileCapabilityCacheStore(
        config_path.with_name(".provider-capabilities.json"),
        key=secret_store.derive_key("provider-capability-cache"),
    )

    def supplied_config() -> dict[str, Any]:
        config = deepcopy(get_all_config(force_reload=True))
        secret_store.sync(config)
        config["revision"] = _redacted_config_revision(
            config,
            secret_revision_state=secret_store.revision_state(),
        )
        providers = config.get("llm", {}).get("providers", {})
        if isinstance(providers, dict):
            for alias, provider in providers.items():
                if not isinstance(provider, dict):
                    continue
                cached = capability_store.get(alias, provider)
                if cached is not None:
                    provider["_capability_profile"] = {
                        **cached.capabilities,
                        "revision": cached.revision,
                    }
        return config

    # A persisted budget scope reserves one real Provider request at a time.
    # Letting an SDK retry internally would create paid attempts which the
    # scope cannot observe or reserve. Legacy/unscoped workflows retain their
    # configured retry behavior; callers can also explicitly request a value.
    effective_max_provider_retries = (
        0
        if max_provider_retries is None
        and callable(getattr(attempt_scope, "claim_with_budget", None))
        else max_provider_retries
    )

    return GenerationRuntime(
        config_supplier=supplied_config,
        adapter_factory=lambda alias, timeout: LLMService(
            provider_name=alias,
            timeout_seconds=timeout,
            max_retries=effective_max_provider_retries,
        ),
        attempt_scope=attempt_scope,
    )


def create_workflow_runtime(
    *,
    max_provider_retries: int | None = None,
) -> GenerationRuntime:
    """Create the single supported workflow execution runtime."""
    return create_generation_runtime(max_provider_retries=max_provider_retries)
