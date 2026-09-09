"""Source-bound author confirmation, distinct from model completion evidence."""
from __future__ import annotations

from typing import Any, Literal, Mapping

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictInt


class AuthorProseConfirmation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["author_prose_confirmation.v1"] = "author_prose_confirmation.v1"
    owner_id: str = Field(pattern=r"^[0-9a-f]{24}$")
    novel_id: str = Field(pattern=r"^[0-9a-f]{24}$")
    chapter_id: str = Field(pattern=r"^[0-9a-f]{24}$")
    source_run_id: str = Field(pattern=r"^[0-9a-f]{24}$")
    source_run_revision: StrictInt = Field(ge=1)
    content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    outline_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_narrative_revision: StrictInt = Field(ge=0)
    scene_count: StrictInt = Field(ge=1)
    confirmed_at: AwareDatetime


def verify_author_prose_confirmation(
    chapter: Mapping[str, Any],
    *,
    content_digest: str,
    owner_id: str | None = None,
) -> AuthorProseConfirmation:
    """Read an exact author-approved text without treating it as Judge approval."""
    acceptance = chapter.get("prose_acceptance")
    if not isinstance(acceptance, Mapping):
        raise ValueError("正文缺少作者确认记录")
    confirmation = AuthorProseConfirmation.model_validate(acceptance.get("author_confirmation"))
    if (
        acceptance.get("state") != "author_confirmed"
        or acceptance.get("content_origin") != "ai"
        or acceptance.get("accepted_partial") is not False
        or acceptance.get("completion_status") != "complete"
        or acceptance.get("finish_reason") != "stop"
        or acceptance.get("source_run_id") != confirmation.source_run_id
        or acceptance.get("source_run_revision") != confirmation.source_run_revision
        or acceptance.get("content_digest") != content_digest
        or confirmation.content_digest != content_digest
        or confirmation.chapter_id != str(chapter.get("_id") or "")
        or confirmation.novel_id != str(chapter.get("novel_id") or "")
        or (owner_id is not None and confirmation.owner_id != owner_id)
        or acceptance.get("chapter_completion_certificate") is not None
    ):
        raise ValueError("作者确认记录与当前正文不一致")
    return confirmation
