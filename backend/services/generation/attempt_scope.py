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
    ) -> None:
        self.job_id = str(job_id)
        self.chapter_id = str(chapter_id)
        self.step_id = str(step_id)
        self.repo = repo
        self._claims: dict[str, tuple[str, str]] = {}
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
        attempt_id = await self.repo.claim_attempt(
            self.job_id,
            self.chapter_id,
            self.step_id,
            phase,
            provider_alias,
        )
        self._claims[attempt_id] = (provider_alias, phase)
        return attempt_id

    async def account(self, attempt_id: str, usage: TokenUsage) -> None:
        if attempt_id in self._attempts:
            return
        provider_alias, phase = self._claims[attempt_id]
        await self.repo.account_attempt(self.job_id, attempt_id, usage)
        self._attempts[attempt_id] = AttemptUsage(
            attempt_id=attempt_id,
            provider_alias=provider_alias,
            phase=phase,
            usage=usage.model_copy(),
        )

    async def mark_uncertain(self, attempt_id: str, reason: str) -> None:
        self._uncertain.add(attempt_id)
        await self.repo.mark_attempt_uncertain(self.job_id, attempt_id, reason)
