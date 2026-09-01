"""Deterministic database identities for one successor acceptance claim.

The host-local stop-loss journal is the authority for at most two real runs.
Every database object created by a run is therefore derived from that immutable
claim instead of from process-local randomness.  Confirmation loss can then
recover the same isolated fixture, outline control Job and root Job without
creating a second paid execution.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.evaluation.required_book_successor_acceptance_ledger import (
    SuccessorAcceptanceRunClaim,
)
from backend.services.generation.required_book_successor import (
    required_book_successor_digest,
)


_SHA256 = r"^[0-9a-f]{64}$"
_OBJECT_ID = r"^[0-9a-f]{24}$"


def _object_id(claim_digest: str, role: str) -> str:
    return required_book_successor_digest({
        "schema_version": "successor_acceptance_object_identity.v1",
        "claim_digest": claim_digest,
        "role": role,
    })[:24]


class SuccessorAcceptanceFixtureIdentity(BaseModel):
    """Closed identity of every durable object owned by one real run."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        hide_input_in_errors=True,
    )

    schema_version: Literal["successor_acceptance_fixture_identity.v1"] = (
        "successor_acceptance_fixture_identity.v1"
    )
    binding_digest: str = Field(pattern=_SHA256)
    claim_digest: str = Field(pattern=_SHA256)
    owner_id: str = Field(pattern=_OBJECT_ID)
    novel_id: str = Field(pattern=_OBJECT_ID)
    volume_id: str = Field(pattern=_OBJECT_ID)
    chapter_ids: tuple[str, str, str]
    outline_job_id: str = Field(pattern=_OBJECT_ID)
    root_job_id: str = Field(pattern=_OBJECT_ID)

    @model_validator(mode="after")
    def validate_identity(self) -> "SuccessorAcceptanceFixtureIdentity":
        expected = {
            "novel_id": _object_id(self.claim_digest, "novel"),
            "volume_id": _object_id(self.claim_digest, "volume:1"),
            "chapter_ids": tuple(
                _object_id(self.claim_digest, f"chapter:{order}")
                for order in range(1, 4)
            ),
            "outline_job_id": _object_id(self.claim_digest, "outline-job"),
            "root_job_id": _object_id(self.claim_digest, "root-job"),
        }
        if (
            self.novel_id != expected["novel_id"]
            or self.volume_id != expected["volume_id"]
            or self.chapter_ids != expected["chapter_ids"]
            or self.outline_job_id != expected["outline_job_id"]
            or self.root_job_id != expected["root_job_id"]
            or len(set((
                self.novel_id,
                self.volume_id,
                *self.chapter_ids,
                self.outline_job_id,
                self.root_job_id,
            ))) != 7
        ):
            raise ValueError("successor_acceptance_fixture_identity_changed")
        identity = self.model_dump(mode="python", exclude={"binding_digest"})
        if required_book_successor_digest(identity) != self.binding_digest:
            raise ValueError("successor_acceptance_fixture_binding_changed")
        return self

    @classmethod
    def from_claim(
        cls,
        claim: SuccessorAcceptanceRunClaim,
    ) -> "SuccessorAcceptanceFixtureIdentity":
        frozen = SuccessorAcceptanceRunClaim.model_validate(
            claim.model_dump(mode="python")
        )
        identity = {
            "schema_version": "successor_acceptance_fixture_identity.v1",
            "claim_digest": frozen.claim_digest,
            "owner_id": frozen.owner_id,
            "novel_id": _object_id(frozen.claim_digest, "novel"),
            "volume_id": _object_id(frozen.claim_digest, "volume:1"),
            "chapter_ids": tuple(
                _object_id(frozen.claim_digest, f"chapter:{order}")
                for order in range(1, 4)
            ),
            "outline_job_id": _object_id(
                frozen.claim_digest,
                "outline-job",
            ),
            "root_job_id": _object_id(frozen.claim_digest, "root-job"),
        }
        return cls(
            **identity,
            binding_digest=required_book_successor_digest(identity),
        )


__all__ = ["SuccessorAcceptanceFixtureIdentity"]
