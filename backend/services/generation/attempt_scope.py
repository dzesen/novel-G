"""批量生成使用的持久化 Provider attempt 作用域。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from backend.db.repositories.generation_job_repository import (
    GenerationJobRepository,
    generation_job_repo,
)
from backend.llm.models import TokenUsage
from backend.services.llm.generation_runtime import AttemptUsage


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
        self._restore_attempt_evidence(existing_attempt_slots)

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
            self._claims[attempt_id] = (provider_alias, phase)
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
            state = str(slot.get("state") or "")
            if state == "uncertain":
                self._uncertain.add(attempt_id)
            if state != "accounted":
                continue
            raw_usage = slot.get("usage")
            if not isinstance(raw_usage, Mapping):
                raise ValueError("persisted Provider attempt usage is invalid")
            usage = TokenUsage.model_validate(dict(raw_usage))
            self._attempts[attempt_id] = AttemptUsage(
                attempt_id=attempt_id,
                provider_alias=provider_alias,
                phase=phase,
                usage=usage,
            )

    @property
    def attempts(self) -> tuple[AttemptUsage, ...]:
        return tuple(self._attempts.values())

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
        )
        self._claims[attempt_id] = (provider_alias, phase)
        self._conservative_tokens[attempt_id] = conservative_tokens
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

    async def mark_uncertain(self, attempt_id: str, reason: str) -> None:
        self._uncertain.add(attempt_id)
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
        await self.repo.release_attempt_budget(
            self.job_id,
            attempt_id,
            conservative_tokens=conservative_tokens,
            reason=reason,
        )
