"""Frozen, zero-cost selection of chapters that need independent review.

A not-reviewed receipt records an authorized omission. It contains no semantic
evidence and cannot stand in for a successful Judge result.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


REVIEW_SELECTION_SCHEMA = "chapter_review_selection.v1"
REVIEW_AUTHORIZATION_SCHEMA = "chapter_review_authorization.v1"
NOT_REVIEWED_SCHEMA = "chapter_not_reviewed.v1"
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
ObjectIdText = Annotated[str, Field(pattern=r"^[0-9a-f]{24}$")]
ReviewEnforcement = Literal["advisory", "strict"]


def review_digest(value: Any) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
    ).encode("utf-8")).hexdigest()


class _ClosedReviewModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, revalidate_instances="always")

    @field_validator("*", mode="before")
    @classmethod
    def tupleize_ids(cls, value: Any, info: Any) -> Any:
        if info.field_name.endswith("_ids") and isinstance(value, list):
            return tuple(value)
        return value


class ChapterReviewSelection(_ClosedReviewModel):
    schema_version: Literal["chapter_review_selection.v1"] = REVIEW_SELECTION_SCHEMA
    mode: Literal["key_chapters", "selected_chapters", "all_chapters", "no_chapters"] = "key_chapters"
    selected_chapter_ids: tuple[ObjectIdText, ...] = Field(default=(), max_length=10_000)
    review_after_prose_repair: Literal[True] = True
    # Absence belongs to an older, strict authorization. Keep its digest exact.
    enforcement: ReviewEnforcement | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def validate_selection(self) -> "ChapterReviewSelection":
        if tuple(sorted(set(self.selected_chapter_ids))) != self.selected_chapter_ids:
            raise ValueError("review chapter IDs must be sorted and unique")
        if self.mode in {"all_chapters", "no_chapters"} and self.selected_chapter_ids:
            raise ValueError("whole-scope review modes cannot carry selected chapter IDs")
        return self


class ChapterReviewAuthorization(_ClosedReviewModel):
    schema_version: Literal["chapter_review_authorization.v1"] = REVIEW_AUTHORIZATION_SCHEMA
    selection: ChapterReviewSelection
    chapter_ids: tuple[ObjectIdText, ...] = Field(max_length=10_000)
    required_chapter_ids: tuple[ObjectIdText, ...] = Field(max_length=10_000)
    digest: Digest

    @model_validator(mode="after")
    def validate_binding(self) -> "ChapterReviewAuthorization":
        for ids in (self.chapter_ids, self.required_chapter_ids):
            if tuple(sorted(set(ids))) != ids:
                raise ValueError("review authorization IDs must be sorted and unique")
        if not set(self.required_chapter_ids).issubset(self.chapter_ids):
            raise ValueError("review requirement is outside the authorized work")
        if not set(self.selection.selected_chapter_ids).issubset(self.required_chapter_ids):
            raise ValueError("author-selected chapters are missing from review")
        if self.selection.mode == "all_chapters" and self.chapter_ids != self.required_chapter_ids:
            raise ValueError("all-chapter review requirement is incomplete")
        if self.selection.mode == "no_chapters" and self.required_chapter_ids:
            raise ValueError("no-chapter review cannot require routine chapter review")
        if self.selection.mode == "selected_chapters" and (
            self.required_chapter_ids != self.selection.selected_chapter_ids
        ):
            raise ValueError("selected-chapter review requirement changed")
        if review_digest(self.model_dump(mode="json", exclude={"digest"})) != self.digest:
            raise ValueError("review authorization digest mismatch")
        return self

    def requires_review(self, chapter_id: str, *, prose_repaired: bool = False) -> bool:
        if chapter_id not in self.chapter_ids:
            raise ValueError("chapter is outside the review authorization")
        return chapter_id in self.required_chapter_ids or prose_repaired


def default_chapter_review_selection() -> ChapterReviewSelection:
    return ChapterReviewSelection(enforcement="advisory")


def review_is_advisory(authorization: ChapterReviewAuthorization | None) -> bool:
    return authorization is not None and authorization.selection.enforcement == "advisory"


def build_chapter_review_authorization(
    chapters: Sequence[Mapping[str, Any]],
    selection: ChapterReviewSelection | Mapping[str, Any],
) -> ChapterReviewAuthorization:
    selection = ChapterReviewSelection.model_validate(selection)
    by_volume: dict[str, list[tuple[int, str]]] = {}
    chapter_ids: set[str] = set()
    for chapter in chapters:
        chapter_id = str(chapter.get("chapter_id") or chapter.get("_id") or "")
        if chapter_id in chapter_ids:
            raise ValueError("duplicate chapter in review work")
        chapter_ids.add(chapter_id)
        order = chapter.get("order_index")
        volume_id = str(chapter.get("volume_id") or "")
        if type(order) is not int or order < 0 or not volume_id:
            raise ValueError("review selection requires a chapter order and volume")
        by_volume.setdefault(volume_id, []).append((order, chapter_id))
    if not set(selection.selected_chapter_ids).issubset(chapter_ids):
        raise ValueError("selected review chapter is outside the requested work")
    required = set(selection.selected_chapter_ids)
    if selection.mode == "all_chapters":
        required = chapter_ids.copy()
    elif selection.mode == "key_chapters":
        for entries in by_volume.values():
            entries.sort()
            if len({order for order, _ in entries}) != len(entries):
                raise ValueError("ambiguous chapter order in review selection")
            required.update((entries[0][1], entries[-1][1]))
    payload = {
        "schema_version": REVIEW_AUTHORIZATION_SCHEMA,
        "selection": selection.model_dump(mode="json"),
        "chapter_ids": sorted(chapter_ids),
        "required_chapter_ids": sorted(required),
    }
    return ChapterReviewAuthorization.model_validate({**payload, "digest": review_digest(payload)})


def review_authorization_from_readiness(
    readiness: Mapping[str, Any],
) -> ChapterReviewAuthorization | None:
    planning = readiness.get("planning")
    if not isinstance(planning, Mapping) or "chapter_review_authorization" not in planning:
        return None  # A historical authorization still requires review of every chapter.
    authorization = ChapterReviewAuthorization.model_validate(planning["chapter_review_authorization"])
    work = readiness.get("work")
    if isinstance(work, Mapping) and isinstance(work.get("chapters"), list):
        expected = build_chapter_review_authorization(work["chapters"], authorization.selection)
        if expected != authorization:
            raise ValueError("review authorization differs from the frozen chapter order")
    elif authorization.selection.mode == "key_chapters":
        raise ValueError("key-chapter review requires its frozen worklist")
    return authorization


class ChapterNotReviewedReceipt(_ClosedReviewModel):
    schema_version: Literal["chapter_not_reviewed.v1"] = NOT_REVIEWED_SCHEMA
    decision: Literal["not_reviewed"] = "not_reviewed"
    reason: Literal["not_requested_by_author", "outside_key_chapters"]
    review_authorization_digest: Digest
    chapter_id: ObjectIdText
    source_prose_run_id: ObjectIdText
    source_prose_run_revision: int = Field(ge=1)
    source_content_digest: Digest
    source_outline_digest: Digest


def build_not_reviewed_receipt(
    authorization: ChapterReviewAuthorization,
    *,
    chapter_id: str,
    source_prose_run_id: str,
    source_prose_run_revision: int,
    source_content_digest: str,
    outline: Mapping[str, Any],
    prose_repaired: bool = False,
) -> ChapterNotReviewedReceipt:
    if authorization.requires_review(chapter_id, prose_repaired=prose_repaired):
        raise ValueError("the frozen authorization requires review of this chapter")
    return ChapterNotReviewedReceipt(
        reason=("outside_key_chapters" if authorization.selection.mode == "key_chapters"
                else "not_requested_by_author"),
        review_authorization_digest=authorization.digest,
        chapter_id=chapter_id,
        source_prose_run_id=source_prose_run_id,
        source_prose_run_revision=source_prose_run_revision,
        source_content_digest=source_content_digest,
        source_outline_digest=review_digest(dict(outline)),
    )


def validate_not_reviewed_receipt(
    value: Any,
    authorization: ChapterReviewAuthorization | None,
    **source: Any,
) -> ChapterNotReviewedReceipt:
    receipt = ChapterNotReviewedReceipt.model_validate(value)
    if authorization is None:
        raise ValueError("historical authorizations require independent review")
    expected = build_not_reviewed_receipt(authorization, **source)
    if receipt != expected:
        raise ValueError("not-reviewed receipt no longer matches its source or authorization")
    return receipt


def not_reviewed_completion_metadata(receipt: ChapterNotReviewedReceipt) -> dict[str, Any]:
    return {
        "review_status": "not_reviewed",
        "outline_contract_digest": receipt.source_outline_digest,
        "quality_debt_status": "not_run",
        "quality_debt_count": 0,
        "quality_debt_sidecar_digest": review_digest({
            "schema_version": "chapter_review_not_run.v1",
            "receipt": receipt.model_dump(mode="json"),
        }),
    }


class ChapterReviewCoverage(_ClosedReviewModel):
    """Certificate projection; a policy omission is never a semantic pass."""

    schema_version: Literal["chapter_review_coverage.v1"] = "chapter_review_coverage.v1"
    status: Literal["passed", "not_reviewed", "advisory"]
    required: bool
    authorization_digest: Digest
    prose_repaired: bool = False
    enforcement: ReviewEnforcement | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def validate_status(self) -> "ChapterReviewCoverage":
        if self.prose_repaired and not self.required:
            raise ValueError("repaired prose requires review")
        if self.required != (self.status != "not_reviewed"):
            raise ValueError("review coverage does not satisfy its frozen requirement")
        if self.status == "advisory" and self.enforcement != "advisory":
            raise ValueError("semantic advice requires explicit advisory authorization")
        return self
