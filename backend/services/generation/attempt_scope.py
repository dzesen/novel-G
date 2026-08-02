"""批量生成使用的持久化 Provider attempt 作用域。"""

from __future__ import annotations

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
