"""Strict model output schema for AI-assisted reference-card curation."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from backend.services.novel.character_profile import CharacterProfileSchema


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CharacterDetailsSchema(_StrictModel):
    role: str = Field(default="", max_length=120)
    motivation: str = Field(default="", max_length=300)
    conflict: str = Field(default="", max_length=300)
    appearance: str = Field(default="", max_length=300)
    personality: str = Field(default="", max_length=300)
    abilities: str = Field(default="", max_length=300)
    relationships: str = Field(default="", max_length=500)


class LocationDetailsSchema(_StrictModel):
    geography: str = Field(default="", max_length=300)
    atmosphere: str = Field(default="", max_length=300)
    function: str = Field(default="", max_length=300)
    story_importance: str = Field(default="", max_length=300)
    dangers: str = Field(default="", max_length=300)


class ItemDetailsSchema(_StrictModel):
    function: str = Field(default="", max_length=300)
    origin: str = Field(default="", max_length=300)
    limitations: str = Field(default="", max_length=300)
    owner: str = Field(default="", max_length=120)


class RuleDetailsSchema(_StrictModel):
    mechanism: str = Field(default="", max_length=500)
    cost: str = Field(default="", max_length=300)
    limitations: str = Field(default="", max_length=300)
    exceptions: str = Field(default="", max_length=300)


class _CardCandidateBase(_StrictModel):
    name: str = Field(min_length=1, max_length=120)
    subtitle: str = Field(default="", max_length=200)
    description: str = Field(default="", max_length=800)
    importance: Literal["main", "sub"] = "sub"
    tags: list[str] = Field(default_factory=list, max_length=8)


class CharacterCandidateSchema(_CardCandidateBase):
    details: CharacterDetailsSchema = Field(default_factory=CharacterDetailsSchema)
    character_profile: CharacterProfileSchema | None = None


class LocationCandidateSchema(_CardCandidateBase):
    details: LocationDetailsSchema = Field(default_factory=LocationDetailsSchema)


class ItemCandidateSchema(_CardCandidateBase):
    details: ItemDetailsSchema = Field(default_factory=ItemDetailsSchema)


class RuleCandidateSchema(_CardCandidateBase):
    details: RuleDetailsSchema = Field(default_factory=RuleDetailsSchema)


class ReferenceCardCandidatesSchema(_StrictModel):
    characters: list[CharacterCandidateSchema] = Field(min_length=2, max_length=10)
    locations: list[LocationCandidateSchema] = Field(min_length=1, max_length=8)
    items: list[ItemCandidateSchema] = Field(default_factory=list, max_length=6)
    rules: list[RuleCandidateSchema] = Field(default_factory=list, max_length=6)
