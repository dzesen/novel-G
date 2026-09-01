"""Versioned authority carried by one non-formal successor state request.

The binding is intentionally narrower than ``JobMutationRecoveryBindingV1``:
it can make a deferred state proposal recoverable, but it grants no authority
to accept that proposal or mutate formal chapter state.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


_SHA256 = r"^[0-9a-f]{64}$"
_OBJECT_ID = r"^[0-9a-f]{24}$"
_MAX = 2**63 - 1


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _jsonable(value.model_dump(mode="python"))
    if isinstance(value, Mapping):
        return {
            str(key): _jsonable(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, datetime):
        aware = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return aware.astimezone(timezone.utc).isoformat()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def required_state_digest(value: Any) -> str:
    encoded = json.dumps(
        _jsonable(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class RequiredStateGenerationBinding(BaseModel):
    """Exact, replayable identity for one of at most three state calls."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        hide_input_in_errors=True,
    )

    schema_version: Literal["required_state_generation_binding.v1"] = (
        "required_state_generation_binding.v1"
    )
    binding_digest: str = Field(pattern=_SHA256)
    job_id: str = Field(pattern=_OBJECT_ID)
    owner_id: str = Field(pattern=_OBJECT_ID)
    novel_id: str = Field(pattern=_OBJECT_ID)
    chapter_id: str = Field(pattern=_OBJECT_ID)
    readiness_digest: str = Field(pattern=_SHA256)
    authorization_revision: int = Field(ge=1, le=_MAX)
    expected_narrative_revision: int = Field(ge=0, le=_MAX)
    predecessor_job_id: str = Field(pattern=_OBJECT_ID)
    predecessor_result_digest: str = Field(pattern=_SHA256)
    source_run_id: str = Field(pattern=_OBJECT_ID)
    source_run_revision: int = Field(ge=2, le=_MAX)
    source_content_digest: str = Field(pattern=_SHA256)
    ordinal: int = Field(ge=0, le=2)
    request_digest: str = Field(pattern=_SHA256)
    recovery_key: str = Field(pattern=_SHA256)
    can_accept_formal_state: Literal[False] = False

    @model_validator(mode="after")
    def validate_digests(self) -> "RequiredStateGenerationBinding":
        identity = self.model_dump(
            mode="python",
            exclude={"binding_digest", "recovery_key"},
        )
        if required_state_digest(identity) != self.binding_digest:
            raise ValueError("required_state_generation_binding_changed")
        recovery_identity = {
            "schema_version": "required_state_generation_recovery_key.v1",
            "job_id": self.job_id,
            "readiness_digest": self.readiness_digest,
            "chapter_id": self.chapter_id,
            "ordinal": self.ordinal,
            "request_digest": self.request_digest,
        }
        if required_state_digest(recovery_identity) != self.recovery_key:
            raise ValueError("required_state_generation_recovery_key_changed")
        return self

    @classmethod
    def create(
        cls,
        *,
        job_id: str,
        owner_id: str,
        novel_id: str,
        chapter_id: str,
        readiness_digest: str,
        authorization_revision: int,
        expected_narrative_revision: int,
        predecessor_job_id: str,
        predecessor_result_digest: str,
        source_run_id: str,
        source_run_revision: int,
        source_content_digest: str,
        ordinal: int,
        request_digest: str,
    ) -> "RequiredStateGenerationBinding":
        identity = {
            "schema_version": "required_state_generation_binding.v1",
            "job_id": job_id,
            "owner_id": owner_id,
            "novel_id": novel_id,
            "chapter_id": chapter_id,
            "readiness_digest": readiness_digest,
            "authorization_revision": authorization_revision,
            "expected_narrative_revision": expected_narrative_revision,
            "predecessor_job_id": predecessor_job_id,
            "predecessor_result_digest": predecessor_result_digest,
            "source_run_id": source_run_id,
            "source_run_revision": source_run_revision,
            "source_content_digest": source_content_digest,
            "ordinal": ordinal,
            "request_digest": request_digest,
            "can_accept_formal_state": False,
        }
        recovery_identity = {
            "schema_version": "required_state_generation_recovery_key.v1",
            "job_id": job_id,
            "readiness_digest": readiness_digest,
            "chapter_id": chapter_id,
            "ordinal": ordinal,
            "request_digest": request_digest,
        }
        return cls(
            **identity,
            binding_digest=required_state_digest(identity),
            recovery_key=required_state_digest(recovery_identity),
        )

    def validates_source(
        self,
        *,
        novel_id: str,
        chapter_id: str,
        source_run_id: str,
        source_run_revision: int,
        source_content_digest: str,
    ) -> bool:
        return (
            self.novel_id == str(novel_id)
            and self.chapter_id == str(chapter_id)
            and self.source_run_id == str(source_run_id)
            and self.source_run_revision == source_run_revision
            and self.source_content_digest == str(source_content_digest)
        )
