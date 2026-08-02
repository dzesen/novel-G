"""Persisted per-call accounting for an interactive ``ProseRun``.

Unlike a batch job, a prose run keeps one active reservation and aggregates old
uncertain amounts.  That preserves a strict budget without adding an unbounded
manual-continuation attempt history to the draft document.
"""
from __future__ import annotations

from backend.db.repositories.prose_run_repository import (
    ProseRunRepository,
    StaleProseRun,
    prose_run_repo,
)
from backend.llm.models import TokenUsage
from backend.services.llm.generation_runtime import AttemptUsage


class ProseRunAttemptScope:
    def __init__(
        self,
        *,
        run_id: str,
        owner_id: str,
        lease_token: str,
        repo: ProseRunRepository = prose_run_repo,
    ) -> None:
        self.run_id = str(run_id)
        self.owner_id = str(owner_id)
        self.lease_token = str(lease_token)
        self.repo = repo
        self._claims: dict[str, tuple[str, str]] = {}
        self._conservative_tokens: dict[str, int | None] = {}
        self._attempts: dict[str, AttemptUsage] = {}
        self._uncertain: set[str] = set()

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
        attempt_id = await self.repo.claim_call_budget(
            run_id=self.run_id,
            owner_id=self.owner_id,
            lease_token=self.lease_token,
            provider_alias=provider_alias,
            phase=phase,
            conservative_tokens=conservative_tokens,
        )
        self._claims[attempt_id] = (str(provider_alias), str(phase))
        self._conservative_tokens[attempt_id] = conservative_tokens
        return attempt_id

    async def account(self, attempt_id: str, usage: TokenUsage) -> None:
        if attempt_id in self._attempts or attempt_id in self._uncertain:
            return
        provider_alias, phase = self._claims[attempt_id]
        conservative_tokens = self._conservative_tokens.get(attempt_id)
        has_usage = bool(
            usage.total_tokens or usage.input_tokens or usage.output_tokens
        )
        if conservative_tokens is None and not has_usage:
            # There was a successful response but no auditable bill.  Do not
            # write a false zero cost; keep the in-flight slot uncertain.
            await self.mark_uncertain(
                attempt_id,
                "Provider completed without usage and no conservative bound",
            )
            return
        effective_usage = usage.model_copy()
        if conservative_tokens is not None and not has_usage:
            effective_usage = TokenUsage(total_tokens=int(conservative_tokens))
        settled = await self.repo.settle_call_budget(
            run_id=self.run_id,
            owner_id=self.owner_id,
            lease_token=self.lease_token,
            attempt_id=attempt_id,
            usage=effective_usage,
            conservative_tokens=conservative_tokens,
        )
        if not settled:
            raise StaleProseRun("正文 Provider 调用的预算结算已失效")
        self._attempts[attempt_id] = AttemptUsage(
            attempt_id=attempt_id,
            provider_alias=provider_alias,
            phase=phase,
            usage=effective_usage,
        )

    async def mark_uncertain(self, attempt_id: str, reason: str) -> None:
        if attempt_id in self._uncertain:
            return
        marked = await self.repo.mark_call_budget_uncertain(
            run_id=self.run_id,
            owner_id=self.owner_id,
            lease_token=self.lease_token,
            attempt_id=attempt_id,
            reason=reason,
        )
        if not marked:
            raise StaleProseRun("正文 Provider 调用的不确定状态已失效")
        self._uncertain.add(attempt_id)

    async def release_pre_dispatch(self, attempt_id: str, reason: str) -> None:
        conservative_tokens = self._conservative_tokens.get(attempt_id)
        released = await self.repo.release_call_budget_pre_dispatch(
            run_id=self.run_id,
            owner_id=self.owner_id,
            lease_token=self.lease_token,
            attempt_id=attempt_id,
            conservative_tokens=conservative_tokens,
            reason=reason,
        )
        if not released:
            raise StaleProseRun("正文 Provider 调用的预算释放已失效")
