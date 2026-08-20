"""批量生成使用的持久化 Provider attempt 作用域。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from backend.db.repositories.generation_job_repository import (
    GenerationJobRepository,
    generation_job_repo,
)
from backend.llm.models import TokenUsage
from backend.services.generation.candidate_repair_contracts import (
    PreDispatchFenceV1,
)
from backend.services.llm.generation_runtime import AttemptUsage


_PERSISTED_ATTEMPT_STATES = frozenset({
    "claimed",
    "accounted",
    "uncertain",
    "released_pre_dispatch",
    "uncertain_retry_acknowledged",
    "uncertain_skip_acknowledged",
    "uncertain_abort_acknowledged",
})
_EVIDENCE_ATTEMPT_STATES = frozenset({
    "accounted",
    "uncertain",
    "uncertain_retry_acknowledged",
    "uncertain_skip_acknowledged",
    "uncertain_abort_acknowledged",
})
_RECOVERED_OUTCOME_ATTEMPT_STATES = frozenset({
    "accounted",
    "released_pre_dispatch",
    "uncertain_retry_acknowledged",
    "uncertain_skip_acknowledged",
    "uncertain_abort_acknowledged",
})
_MAX_PERSISTED_ATTEMPT_TOKENS = 1_000_000_000
_MAX_PERSISTED_LEDGER_TOKENS = 2**63 - 1


def _strict_persisted_usage(value: object) -> TokenUsage:
    if not isinstance(value, Mapping):
        raise ValueError("persisted Provider attempt usage is invalid")
    fields = ("input_tokens", "output_tokens", "total_tokens")
    if any(
        field not in value
        or type(value[field]) is not int
        or int(value[field]) < 0
        for field in fields
    ):
        raise ValueError("persisted Provider attempt usage is invalid")
    return TokenUsage(**{field: int(value[field]) for field in fields})


def project_persisted_attempt_evidence(
    slots: Sequence[Mapping[str, object]],
    *,
    maximum_entries: int,
) -> tuple[list[dict[str, object]], int]:
    """Strictly project a bounded persisted Job ledger for public recovery."""

    if (
        type(maximum_entries) is not int
        or maximum_entries < 0
        or len(slots) > maximum_entries
    ):
        raise ValueError("persisted Provider attempt ledger exceeds its bound")
    projected: list[dict[str, object]] = []
    seen: set[str] = set()
    total = 0
    for slot in slots:
        if not isinstance(slot, Mapping):
            raise ValueError("persisted Provider attempt ledger is invalid")
        attempt_id = slot.get("attempt_id")
        provider_alias = slot.get("provider_alias")
        phase = slot.get("phase")
        state = slot.get("state")
        if (
            not isinstance(attempt_id, str)
            or not attempt_id
            or len(attempt_id) > 128
            or attempt_id in seen
            or not isinstance(provider_alias, str)
            or not provider_alias
            or len(provider_alias) > 64
            or not isinstance(phase, str)
            or not phase
            or len(phase) > 64
            or state not in _RECOVERED_OUTCOME_ATTEMPT_STATES
        ):
            raise ValueError("persisted Provider attempt identity is invalid")
        seen.add(attempt_id)
        raw_usage = slot.get("usage")
        if raw_usage is None and state != "accounted":
            raw_usage = {
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
            }
        usage = _strict_persisted_usage(raw_usage)
        components = (
            usage.input_tokens,
            usage.output_tokens,
            usage.total_tokens,
        )
        if any(value > _MAX_PERSISTED_ATTEMPT_TOKENS for value in components):
            raise ValueError("persisted Provider attempt usage exceeds its bound")
        if state == "released_pre_dispatch" and any(components):
            raise ValueError("released Provider attempt has paid usage")
        conservative_total = max(
            usage.total_tokens,
            usage.input_tokens + usage.output_tokens,
        )
        if (
            conservative_total > _MAX_PERSISTED_ATTEMPT_TOKENS
            or total > _MAX_PERSISTED_LEDGER_TOKENS - conservative_total
        ):
            raise ValueError("persisted Provider attempt usage exceeds its bound")
        total += conservative_total
        projected.append({
            "attempt_id": attempt_id,
            "provider_alias": provider_alias,
            "phase": phase,
            "state": state,
            "usage": {
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "total_tokens": conservative_total,
            },
        })
    return projected, total


class JobAttemptScope:
    """把 Runtime 的每次付费调用映射到 generation_job 的预留槽。"""

    def __init__(
        self,
        job_id: str,
        chapter_id: str,
        step_id: str,
        *,
        repo: GenerationJobRepository = generation_job_repo,
        confirm_uncertain_retry: bool = False,
        existing_attempt_slots: Sequence[Mapping[str, object]] = (),
    ) -> None:
        self.job_id = str(job_id)
        self.chapter_id = str(chapter_id)
        self.step_id = str(step_id)
        self.repo = repo
        self.confirm_uncertain_retry = bool(confirm_uncertain_retry)
        self._claims: dict[str, tuple[str, str]] = {}
        self._conservative_tokens: dict[str, int | None] = {}
        self._attempts: dict[str, AttemptUsage] = {}
        self._uncertain: set[str] = set()
        self._persisted_states: dict[str, str] = {}
        self._pre_dispatch_fence: PreDispatchFenceV1 | None = None
        self._restore_attempt_evidence(existing_attempt_slots)

    async def bind_pre_dispatch_fence(
        self,
        fence: PreDispatchFenceV1,
    ) -> None:
        """Fence future claims to the current durable receipt lease."""
        if not isinstance(fence, PreDispatchFenceV1):
            raise ValueError("pre-dispatch fence contract is required")
        if fence.step_id != self.step_id:
            raise ValueError("pre-dispatch fence step does not match the scope")
        await self.repo.bind_pre_dispatch_fence(
            self.job_id,
            self.chapter_id,
            fence,
        )
        self._pre_dispatch_fence = fence

    def _restore_attempt_evidence(
        self,
        slots: Sequence[Mapping[str, object]],
    ) -> None:
        for slot in slots:
            if (
                str(slot.get("chapter_id") or "") != self.chapter_id
                or str(slot.get("step_id") or "") != self.step_id
            ):
                continue
            attempt_id = str(slot.get("attempt_id") or "")
            provider_alias = str(slot.get("provider_alias") or "")
            phase = str(slot.get("phase") or "")
            if not attempt_id or not provider_alias or not phase:
                raise ValueError("persisted Provider attempt identity is invalid")
            if attempt_id in self._claims:
                raise ValueError("persisted Provider attempt identity is duplicated")
            state = slot.get("state")
            if not isinstance(state, str) or state not in _PERSISTED_ATTEMPT_STATES:
                raise ValueError("persisted Provider attempt state is invalid")
            self._claims[attempt_id] = (provider_alias, phase)
            self._persisted_states[attempt_id] = state
            raw_bound = slot.get("conservative_tokens")
            if raw_bound is None:
                self._conservative_tokens[attempt_id] = None
            elif (
                isinstance(raw_bound, bool)
                or not isinstance(raw_bound, int)
                or raw_bound < 0
            ):
                raise ValueError("persisted Provider attempt bound is invalid")
            else:
                self._conservative_tokens[attempt_id] = raw_bound
            if state == "uncertain":
                self._uncertain.add(attempt_id)
            if state != "accounted":
                continue
            usage = _strict_persisted_usage(slot.get("usage"))
            self._attempts[attempt_id] = AttemptUsage(
                attempt_id=attempt_id,
                provider_alias=provider_alias,
                phase=phase,
                usage=usage,
            )

    @property
    def attempts(self) -> tuple[AttemptUsage, ...]:
        evidence: list[AttemptUsage] = []
        for attempt_id, (provider_alias, phase) in self._claims.items():
            state = self._persisted_states.get(attempt_id)
            accounted = self._attempts.get(attempt_id)
            if accounted is not None:
                evidence.append(accounted)
                continue
            if state not in _EVIDENCE_ATTEMPT_STATES:
                continue
            conservative_tokens = self._conservative_tokens.get(attempt_id)
            if (
                conservative_tokens is not None
                and (
                    type(conservative_tokens) is not int
                    or conservative_tokens < 0
                )
            ):
                raise ValueError("persisted Provider attempt bound is invalid")
            projected_state = {
                "uncertain_retry_acknowledged": "resolved_retry",
                "uncertain_skip_acknowledged": "resolved_skip",
                "uncertain_abort_acknowledged": "resolved_abort",
            }.get(state, state or "uncertain")
            evidence.append(AttemptUsage(
                attempt_id=attempt_id,
                provider_alias=provider_alias,
                phase=phase,
                usage=TokenUsage(total_tokens=conservative_tokens or 0),
                state=projected_state,
            ))
        return tuple(evidence)

    @property
    def claimed_attempt_ids(self) -> tuple[str, ...]:
        return tuple(self._claims)

    @property
    def uncertain_attempt_ids(self) -> tuple[str, ...]:
        return tuple(self._uncertain)

    async def claim(self, provider_alias: str, phase: str) -> str:
        return await self.claim_with_budget(provider_alias, phase, None)

    async def claim_with_budget(
        self,
        provider_alias: str,
        phase: str,
        conservative_tokens: int | None,
    ) -> str:
        attempt_id = await self.repo.claim_attempt_with_budget(
            self.job_id,
            self.chapter_id,
            self.step_id,
            phase,
            provider_alias,
            conservative_tokens,
            pre_dispatch_fence=self._pre_dispatch_fence,
        )
        self._claims[attempt_id] = (provider_alias, phase)
        self._conservative_tokens[attempt_id] = conservative_tokens
        self._persisted_states[attempt_id] = "claimed"
        return attempt_id

    async def account(self, attempt_id: str, usage: TokenUsage) -> None:
        if attempt_id in self._attempts:
            return
        provider_alias, phase = self._claims[attempt_id]
        conservative_tokens = self._conservative_tokens.get(attempt_id)
        effective_usage = usage.model_copy()
        if (
            conservative_tokens is not None
            and not (
                usage.total_tokens
                or usage.input_tokens
                or usage.output_tokens
            )
        ):
            effective_usage = TokenUsage(total_tokens=conservative_tokens)
        await self.repo.settle_attempt_budget(
            self.job_id,
            attempt_id,
            effective_usage,
            conservative_tokens=conservative_tokens,
        )
        self._attempts[attempt_id] = AttemptUsage(
            attempt_id=attempt_id,
            provider_alias=provider_alias,
            phase=phase,
            usage=effective_usage,
        )
        self._persisted_states[attempt_id] = "accounted"

    async def mark_uncertain(self, attempt_id: str, reason: str) -> None:
        self._uncertain.add(attempt_id)
        self._persisted_states[attempt_id] = "uncertain"
        conservative_tokens = self._conservative_tokens.get(attempt_id)
        if conservative_tokens is None:
            await self.repo.mark_attempt_uncertain(self.job_id, attempt_id, reason)
            return
        await self.repo.mark_attempt_uncertain_with_budget(
            self.job_id,
            attempt_id,
            reason,
        )

    async def release_pre_dispatch(self, attempt_id: str, reason: str) -> None:
        conservative_tokens = self._conservative_tokens.get(attempt_id)
        if conservative_tokens is None:
            return
        self._persisted_states[attempt_id] = "released_pre_dispatch"
        await self.repo.release_attempt_budget(
            self.job_id,
            attempt_id,
            conservative_tokens=conservative_tokens,
            reason=reason,
        )
