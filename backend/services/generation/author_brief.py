"""Versioned author input, independent of generated summaries and card content."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from backend.novel_scale import ChapterCount, WordsPerChapter
from backend.services.llm.agent_orchestrator import CreativeDirectionSelection
from backend.services.novel.style_controls import StyleControlsSchema


AUTHOR_BRIEF_PROTOCOL = "author_brief.v1"
RequirementText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]


class AuthorConstraints(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    must_keep: tuple[RequirementText, ...] = Field(default=(), max_length=20)
    do_not_change: tuple[RequirementText, ...] = Field(default=(), max_length=20)
    style_boundaries: tuple[RequirementText, ...] = Field(default=(), max_length=20)


class AuthorInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    original_idea: str = Field(min_length=1, max_length=8000)
    creative_direction: CreativeDirectionSelection | None = None
    constraints: AuthorConstraints = Field(default_factory=AuthorConstraints)

    @model_validator(mode="after")
    def validate_input_size(self):
        if not self.original_idea.strip() or len(self.model_dump_json()) > 24_000:
            raise ValueError("Author input must be nonempty and at most 24,000 characters")
        return self


class AuthorBrief(BaseModel):
    """A content-addressed snapshot; a new source or scale means a new revision."""
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    schema_version: Literal["author_brief.v1"] = AUTHOR_BRIEF_PROTOCOL
    author_input: AuthorInput
    number_of_chapters: ChapterCount
    words_per_chapter: WordsPerChapter
    writing_style: str = ""
    narrative_pov: str = ""
    tone: str = ""
    style_controls: StyleControlsSchema | None = None

    @property
    def revision(self) -> str:
        return hashlib.sha256(json.dumps(
            self.model_dump(mode="json"), ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")).hexdigest()

    def to_record(self) -> dict[str, Any]:
        return {**self.model_dump(mode="json"), "revision": self.revision}

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "AuthorBrief":
        fields = dict(record)
        revision = fields.pop("revision", None)
        result = cls.model_validate(fields)
        if revision != result.revision:
            raise ValueError("Author brief revision does not match its content")
        return result

    def to_prompt_text(self) -> str:
        # This is the author's task input, never a reference-card retrieval
        # channel. It contains no card body, keyword lookup or external ID map.
        return (
            "【作者已确认的创作输入】\n"
            "以下同版本输入是各规划阶段共同遵守的故事与表达要求；派生剧情不能替代或撤销它。\n"
            "这些要求不改变结构化输出协议、正式资料 ID 白名单、授权或正文完成闸门。\n"
            + json.dumps(self.to_record(), ensure_ascii=False, sort_keys=True)
        )


def creation_author_brief(request: Any) -> AuthorBrief:
    return AuthorBrief(
        author_input=AuthorInput(
            original_idea=request.user_idea.strip(),
            creative_direction=request.creative_direction,
            constraints=getattr(request, "author_constraints", AuthorConstraints()),
        ),
        number_of_chapters=request.number_of_chapters,
        words_per_chapter=request.words_per_chapter,
    )


def novel_author_brief(novel: Mapping[str, Any]) -> AuthorBrief | None:
    """Use explicit saved input only; never reconstruct it from a summary."""
    source = novel.get("author_input")
    if source is None:
        return None
    chapters, words = novel.get("number_of_chapters"), novel.get("words_per_chapter")
    return AuthorBrief(
        author_input=AuthorInput.model_validate(source),
        number_of_chapters=100 if chapters is None else chapters,
        words_per_chapter=3000 if words is None else words,
        writing_style=str(novel.get("writing_style") or ""),
        narrative_pov=str(novel.get("narrative_pov") or ""),
        tone=str(novel.get("tone") or ""),
        style_controls=novel.get("style_controls"),
    )


def render_author_brief_record(record: Mapping[str, Any] | None) -> str:
    return AuthorBrief.from_record(record).to_prompt_text() if record is not None else ""
