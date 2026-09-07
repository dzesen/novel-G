"""GenerationRuntime adapter for a persisted blueprint attempt ledger."""
from __future__ import annotations

from backend.db.repositories.blueprint_run_repository import BlueprintRunRepository
from backend.llm.models import TokenUsage
from backend.services.llm.generation_runtime import AttemptUsage


class BlueprintAttemptScope:
    def __init__(self, repo: BlueprintRunRepository, run: dict):
        self.repo = repo
        self.args = (str(run["_id"]), str(run["owner_id"]), run["lease"]["token"])
        self._claims = {}
        self._attempts = {}
        self._uncertain = set()

    @property
    def attempts(self):
        return tuple(self._attempts.values())

    @property
    def claimed_attempt_ids(self):
        return tuple(self._claims)

    @property
    def uncertain_attempt_ids(self):
        return tuple(self._uncertain)

    async def claim(self, provider_alias: str, phase: str) -> str:
        return await self.claim_with_budget(provider_alias, phase, None)

    async def claim_with_budget(self, provider_alias: str, phase: str, conservative_tokens: int | None) -> str:
        attempt_id = await self.repo.claim(*self.args, provider_alias=provider_alias,
            phase=phase, conservative_tokens=conservative_tokens)
        self._claims[attempt_id] = (provider_alias, phase, conservative_tokens)
        return attempt_id

    def _read_receipt(self, run: dict, attempt_id: str) -> None:
        receipt = run["attempts"][attempt_id]
        provider, phase, _ = self._claims[attempt_id]
        state = receipt["state"]
        if state == "released":
            self._claims.pop(attempt_id, None)
            return
        if state == "uncertain":
            self._uncertain.add(attempt_id)
        if state in {"accounted", "uncertain"}:
            self._attempts[attempt_id] = AttemptUsage(
                attempt_id, provider, phase, TokenUsage.model_validate(receipt["usage"]), state=state,
            )

    async def account(self, attempt_id: str, usage: TokenUsage) -> None:
        # Sync the immutable persisted receipt even when this is a repeated call.
        run = await self.repo.settle(*self.args, attempt_id, usage=usage)
        self._read_receipt(run, attempt_id)

    async def mark_uncertain(self, attempt_id: str, reason: str) -> None:
        del reason
        run = await self.repo.settle(*self.args, attempt_id, uncertain=True)
        self._read_receipt(run, attempt_id)

    async def release_pre_dispatch(self, attempt_id: str, reason: str) -> None:
        del reason
        run = await self.repo.settle(*self.args, attempt_id, released=True)
        self._read_receipt(run, attempt_id)
