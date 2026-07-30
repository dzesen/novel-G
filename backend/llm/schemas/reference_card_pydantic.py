"""Strict model output schema for AI-assisted reference-card curation."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, create_model, model_validator
from backend.services.novel.character_profile import CharacterProfileSchema

ReferenceCardType = Literal["character", "location", "item", "rule", "lore"]


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


class LoreDetailsSchema(_StrictModel):
    category: str = Field(default="", max_length=120)
    era: str = Field(default="", max_length=200)
    background: str = Field(default="", max_length=500)
    story_relevance: str = Field(default="", max_length=300)
    related_entities: str = Field(default="", max_length=300)
    uncertainties: str = Field(default="", max_length=300)


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


class LoreCandidateSchema(_CardCandidateBase):
    details: LoreDetailsSchema = Field(default_factory=LoreDetailsSchema)


class ReferenceCardCandidatesSchema(_StrictModel):
    characters: list[CharacterCandidateSchema] = Field(min_length=2, max_length=10)
    locations: list[LocationCandidateSchema] = Field(min_length=1, max_length=8)
    items: list[ItemCandidateSchema] = Field(default_factory=list, max_length=6)
    rules: list[RuleCandidateSchema] = Field(default_factory=list, max_length=6)
    lores: list[LoreCandidateSchema] = Field(default_factory=list, max_length=6)

    @model_validator(mode="after")
    def keep_existing_optional_candidate_budget(self):
        if len(self.items) + len(self.rules) + len(self.lores) > 12:
            raise ValueError(
                "items, rules and lores may contain at most 12 candidates combined"
            )
        return self


_REFERENCE_CARD_TYPES: tuple[ReferenceCardType, ...] = (
    "character",
    "location",
    "item",
    "rule",
    "lore",
)
_CANDIDATE_FIELDS = {
    "character": ("characters", CharacterCandidateSchema, 2, 10),
    "location": ("locations", LocationCandidateSchema, 1, 8),
    "item": ("items", ItemCandidateSchema, 1, 6),
    "rule": ("rules", RuleCandidateSchema, 1, 6),
    "lore": ("lores", LoreCandidateSchema, 1, 6),
}


@lru_cache(maxsize=31)
def reference_card_candidates_schema_for_types(
    requested_card_types: tuple[ReferenceCardType, ...],
) -> type[ReferenceCardCandidatesSchema]:
    """Build a strict output schema for one explicit non-empty type selection."""

    if not requested_card_types:
        raise ValueError("At least one reference-card type must be selected")
    if len(requested_card_types) != len(set(requested_card_types)):
        raise ValueError("Reference-card types must be unique")
    unknown = set(requested_card_types).difference(_REFERENCE_CARD_TYPES)
    if unknown:
        raise ValueError(f"Unsupported reference-card types: {sorted(unknown)}")

    selected = set(requested_card_types)
    canonical = tuple(
        card_type for card_type in _REFERENCE_CARD_TYPES if card_type in selected
    )
    field_definitions = {}
    for card_type in _REFERENCE_CARD_TYPES:
        group, candidate_schema, selected_minimum, selected_maximum = (
            _CANDIDATE_FIELDS[card_type]
        )
        is_selected = card_type in selected
        field_definitions[group] = (
            list[candidate_schema],
            Field(
                default_factory=list,
                min_length=selected_minimum if is_selected else 0,
                max_length=selected_maximum if is_selected else 0,
            ),
        )

    selection_name = "".join(card_type.title() for card_type in canonical)
    return create_model(
        f"ReferenceCardCandidates{selection_name}Schema",
        __base__=ReferenceCardCandidatesSchema,
        __module__=__name__,
        **field_definitions,
    )
