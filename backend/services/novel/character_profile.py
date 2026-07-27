"""Bounded, prompt-safe portrayal fields for character reference cards."""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


Alias = Annotated[str, Field(max_length=80)]
DialogueExample = Annotated[str, Field(max_length=800)]
SceneOpeningExample = Annotated[str, Field(max_length=1000)]

DIALOGUE_EXAMPLE_LIMIT = 12
DIALOGUE_TOTAL_CHARACTER_LIMIT = 4800
SCENE_OPENING_EXAMPLE_LIMIT = 8
SCENE_OPENING_TOTAL_CHARACTER_LIMIT = 5000
PROFILE_TOTAL_CHARACTER_LIMIT = 14_000


def _normalize_text(value: Any) -> str:
    return str(value or "").strip()


def _normalize_text_list(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        return value
    return list(
        dict.fromkeys(
            text
            for item in value
            if (text := _normalize_text(item))
        )
    )


class CharacterProfileSchema(BaseModel):
    """Novel portrayal data only; executable prompt fields are intentionally absent."""

    model_config = ConfigDict(extra="forbid")

    aliases: list[Alias] = Field(default_factory=list, max_length=20)
    portrayal_context: str = Field(default="", max_length=2000)
    dialogue_examples: list[DialogueExample] = Field(
        default_factory=list,
        max_length=DIALOGUE_EXAMPLE_LIMIT,
    )
    scene_opening_examples: list[SceneOpeningExample] = Field(
        default_factory=list,
        max_length=SCENE_OPENING_EXAMPLE_LIMIT,
    )
    portrayal_notes: str = Field(default="", max_length=2000)

    @field_validator(
        "aliases",
        "dialogue_examples",
        "scene_opening_examples",
        mode="before",
    )
    @classmethod
    def normalize_lists(cls, value: Any) -> Any:
        return _normalize_text_list(value)

    @field_validator("portrayal_context", "portrayal_notes", mode="before")
    @classmethod
    def normalize_text(cls, value: Any) -> str:
        return _normalize_text(value)

    @model_validator(mode="after")
    def enforce_total_limits(self) -> "CharacterProfileSchema":
        dialogue_characters = sum(len(item) for item in self.dialogue_examples)
        if dialogue_characters > DIALOGUE_TOTAL_CHARACTER_LIMIT:
            raise ValueError(
                "Character dialogue examples exceed the total character limit"
            )
        opening_characters = sum(len(item) for item in self.scene_opening_examples)
        if opening_characters > SCENE_OPENING_TOTAL_CHARACTER_LIMIT:
            raise ValueError(
                "Character scene-opening examples exceed the total character limit"
            )
        total = (
            sum(len(item) for item in self.aliases)
            + len(self.portrayal_context)
            + dialogue_characters
            + opening_characters
            + len(self.portrayal_notes)
        )
        if total > PROFILE_TOTAL_CHARACTER_LIMIT:
            raise ValueError("Character profile exceeds the total character limit")
        return self


def normalize_character_profile(value: Any) -> dict[str, Any]:
    """Validate and return the stable persistence representation."""

    return CharacterProfileSchema.model_validate(value or {}).model_dump()
