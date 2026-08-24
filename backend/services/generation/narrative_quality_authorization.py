"""Frozen, zero-paid-call bounds for local narrative repetition signals."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    ValidationError,
    model_validator,
)

from backend.llm.schemas.novel_pydantic import MAX_CHAPTER_OUTLINE_SCENES
from backend.scene_contract_versions import (
    MAX_NARRATIVE_REPETITION_CANDIDATES,
    NARRATIVE_REPETITION_SIGNAL_LAYERS,
    NARRATIVE_REPETITION_SIGNAL_POLICY,
)


NARRATIVE_QUALITY_SIGNAL_AUTHORIZATION_SCHEMA = (
    "narrative_quality_signal_authorization.v1"
)
NARRATIVE_QUALITY_SIGNAL_SCOPE = "current_chapter_scene_pairs"

_NonNegativeInt = Annotated[StrictInt, Field(ge=0)]
_Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class NarrativeQualityDetectionScope(_ClosedModel):
    kind: Literal["current_chapter_scene_pairs"]
    maximum_scenes_per_chapter: Annotated[
        StrictInt,
        Field(ge=0, le=MAX_CHAPTER_OUTLINE_SCENES),
    ]
    maximum_comparison_pairs_per_chapter: _NonNegativeInt
    maximum_comparison_pairs_total: _NonNegativeInt

    @model_validator(mode="after")
    def validate_pair_bound(self) -> "NarrativeQualityDetectionScope":
        maximum_pairs = _pair_count(self.maximum_scenes_per_chapter)
        if self.maximum_comparison_pairs_per_chapter != maximum_pairs:
            raise ValueError("narrative quality per-chapter pair bound changed")
        if self.maximum_comparison_pairs_total < maximum_pairs:
            raise ValueError("narrative quality total pair bound is too small")
        return self


class NarrativeQualitySignalAuthorization(_ClosedModel):
    schema_version: Literal["narrative_quality_signal_authorization.v1"]
    policy_version: Literal["narrative_repetition_signal_policy.v1"]
    eligible_chapter_count: _NonNegativeInt
    eligible_chapter_ids_digest: _Sha256
    eligible_scene_counts_digest: _Sha256
    detection_scope: NarrativeQualityDetectionScope
    layers: tuple[
        Literal[
            "literal_similarity",
            "event_fingerprint",
            "narrative_function",
        ],
        ...,
    ] = Field(min_length=3, max_length=3)
    maximum_candidates_per_chapter: Annotated[
        StrictInt,
        Field(ge=0, le=MAX_NARRATIVE_REPETITION_CANDIDATES),
    ]
    maximum_candidates_total: _NonNegativeInt
    extra_provider_calls_per_chapter: Literal[0]
    extra_provider_calls_total: Literal[0]
    extra_tokens_per_chapter: Literal[0]
    extra_tokens_total: Literal[0]
    uses_existing_adherence_review: StrictBool
    second_judge_enabled: StrictBool

    @model_validator(mode="after")
    def validate_local_sidecar_bounds(
        self,
    ) -> "NarrativeQualitySignalAuthorization":
        if self.layers != NARRATIVE_REPETITION_SIGNAL_LAYERS:
            raise ValueError("narrative quality signal layers changed")
        if (
            self.maximum_candidates_per_chapter
            != MAX_NARRATIVE_REPETITION_CANDIDATES
        ):
            raise ValueError("narrative quality candidate bound changed")
        if self.maximum_candidates_total != (
            self.eligible_chapter_count
            * self.maximum_candidates_per_chapter
        ):
            raise ValueError("narrative quality total candidate bound changed")
        if not self.uses_existing_adherence_review:
            raise ValueError("narrative quality signals require the existing review")
        if self.second_judge_enabled:
            raise ValueError("narrative quality signals cannot add a second Judge")
        if self.eligible_chapter_count == 0 and any(
            (
                self.detection_scope.maximum_scenes_per_chapter,
                self.detection_scope.maximum_comparison_pairs_per_chapter,
                self.detection_scope.maximum_comparison_pairs_total,
                self.maximum_candidates_total,
            )
        ):
            raise ValueError("inactive narrative quality authority is not empty")
        return self


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("narrative quality authorization is not JSON") from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _pair_count(scene_count: int) -> int:
    return scene_count * (scene_count - 1) // 2


def _eligible_scene_bounds(
    chapters: Sequence[Mapping[str, Any]],
) -> tuple[tuple[str, int], ...]:
    result: list[tuple[str, int]] = []
    seen: set[str] = set()
    for chapter in chapters:
        if str(chapter.get("content") or "").strip():
            continue
        chapter_id = str(chapter.get("_id") or "")
        if not chapter_id or chapter_id in seen:
            raise ValueError("narrative quality chapter identity is invalid")
        seen.add(chapter_id)
        outline = chapter.get("outline")
        if not outline:
            scene_count = MAX_CHAPTER_OUTLINE_SCENES
        elif not isinstance(outline, Mapping):
            raise ValueError("narrative quality chapter outline is invalid")
        else:
            scenes = outline.get("scenes")
            if not isinstance(scenes, list):
                raise ValueError("narrative quality scene collection is invalid")
            scene_count = len(scenes)
            if scene_count > MAX_CHAPTER_OUTLINE_SCENES:
                raise ValueError("narrative quality scene bound exceeds outline schema")
        result.append((chapter_id, scene_count))
    return tuple(result)


def build_narrative_quality_signal_authorization(
    chapters: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Freeze deterministic comparison/candidate bounds for eligible chapters."""

    scene_bounds = _eligible_scene_bounds(chapters)
    scene_counts = tuple(scene_count for _chapter_id, scene_count in scene_bounds)
    maximum_scenes = max(scene_counts, default=0)
    projection = NarrativeQualitySignalAuthorization.model_validate({
        "schema_version": NARRATIVE_QUALITY_SIGNAL_AUTHORIZATION_SCHEMA,
        "policy_version": NARRATIVE_REPETITION_SIGNAL_POLICY,
        "eligible_chapter_count": len(scene_bounds),
        "eligible_chapter_ids_digest": _digest(
            [chapter_id for chapter_id, _scene_count in scene_bounds]
        ),
        "eligible_scene_counts_digest": _digest(scene_bounds),
        "detection_scope": {
            "kind": NARRATIVE_QUALITY_SIGNAL_SCOPE,
            "maximum_scenes_per_chapter": maximum_scenes,
            "maximum_comparison_pairs_per_chapter": _pair_count(
                maximum_scenes
            ),
            "maximum_comparison_pairs_total": sum(
                _pair_count(scene_count) for scene_count in scene_counts
            ),
        },
        "layers": NARRATIVE_REPETITION_SIGNAL_LAYERS,
        "maximum_candidates_per_chapter": (
            MAX_NARRATIVE_REPETITION_CANDIDATES
        ),
        "maximum_candidates_total": (
            len(scene_bounds) * MAX_NARRATIVE_REPETITION_CANDIDATES
        ),
        "extra_provider_calls_per_chapter": 0,
        "extra_provider_calls_total": 0,
        "extra_tokens_per_chapter": 0,
        "extra_tokens_total": 0,
        "uses_existing_adherence_review": True,
        "second_judge_enabled": False,
    })
    return projection.model_dump(mode="json")


def parse_narrative_quality_signal_authorization(
    value: Any,
) -> NarrativeQualitySignalAuthorization:
    try:
        parsed = NarrativeQualitySignalAuthorization.model_validate(value)
    except ValidationError as exc:
        raise ValueError(
            "narrative quality signal authorization is invalid"
        ) from exc
    raw = dict(value) if isinstance(value, Mapping) else value
    if _canonical_json(raw) != _canonical_json(parsed.model_dump(mode="json")):
        raise ValueError(
            "narrative quality signal authorization requires exact JSON types"
        )
    return parsed


def narrative_quality_signal_authorization_digest(value: Any) -> str:
    parsed = (
        value
        if isinstance(value, NarrativeQualitySignalAuthorization)
        else parse_narrative_quality_signal_authorization(value)
    )
    return _digest(parsed.model_dump(mode="json"))


def validate_readiness_narrative_quality_signal_authorization(
    readiness: Mapping[str, Any],
) -> NarrativeQualitySignalAuthorization:
    """Rebuild the frozen scope from the signed work snapshot."""

    planning = readiness.get("planning")
    work = readiness.get("work")
    if not isinstance(planning, Mapping) or not isinstance(work, Mapping):
        raise ValueError("narrative quality readiness projection is invalid")
    raw_chapters = work.get("chapters")
    if not isinstance(raw_chapters, list):
        raise ValueError("narrative quality readiness worklist is invalid")
    chapters: list[dict[str, Any]] = []
    for snapshot in raw_chapters:
        if not isinstance(snapshot, Mapping):
            raise ValueError("narrative quality chapter snapshot is invalid")
        has_content = snapshot.get("has_content")
        has_outline = snapshot.get("has_outline")
        scene_count = snapshot.get("scene_count")
        if (
            type(has_content) is not bool
            or type(has_outline) is not bool
            or isinstance(scene_count, bool)
            or not isinstance(scene_count, int)
            or scene_count < 0
            or scene_count > MAX_CHAPTER_OUTLINE_SCENES
        ):
            raise ValueError("narrative quality chapter snapshot changed")
        chapters.append({
            "_id": str(snapshot.get("chapter_id") or ""),
            "content": "present" if has_content else "",
            "outline": (
                {"scenes": [{} for _index in range(scene_count)]}
                if has_outline
                else {}
            ),
        })
    parsed = parse_narrative_quality_signal_authorization(
        planning.get("narrative_quality_signal_authorization")
    )
    expected = build_narrative_quality_signal_authorization(chapters)
    if parsed.model_dump(mode="json") != expected:
        raise ValueError("narrative quality signal scope changed after readiness")
    return parsed
