"""Deterministic, non-blocking narrative quality-signal assessment."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from difflib import SequenceMatcher
from itertools import combinations
from typing import Any, Mapping, Sequence

from backend.llm.schemas.scene_contract_pydantic import (
    NarrativeQualitySidecarSchema,
)
from backend.scene_contract_versions import (
    MAX_NARRATIVE_REPETITION_CANDIDATES,
    NARRATIVE_QUALITY_SIDECAR_SCHEMA,
    NARRATIVE_REPETITION_SIGNAL_LAYERS,
    NARRATIVE_REPETITION_SIGNAL_POLICY,
    MODERN_SCENE_CONTRACT_VERSIONS,
)
from backend.services.novel.state_completion import chapter_content_digest


_EVENT_FIELDS = ("actor_role", "action", "object_role", "outcome")
_FUNCTION_FIELDS = (
    "goal",
    "conflict",
    "turn",
    "outcome",
    "new_information",
    "character_change",
    "stakes_delta",
)


class NarrativeQualitySignalError(ValueError):
    """The quality sidecar cannot be bound to the supplied candidate."""


def _canonical_digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _outline_contract_digest(outline: Mapping[str, Any]) -> str:
    return _canonical_digest(dict(outline))


def _normalized_text(value: str) -> str:
    return "".join(
        character.casefold()
        for character in unicodedata.normalize("NFKC", value)
        if character.isalnum()
    )


def _similarity_basis_points(left: str, right: str) -> int:
    normalized_left = _normalized_text(left)
    normalized_right = _normalized_text(right)
    if not normalized_left or not normalized_right:
        return 0
    return round(
        SequenceMatcher(
            None,
            normalized_left,
            normalized_right,
            autojunk=False,
        ).ratio()
        * 10_000
    )


def _narrative_function_layer(
    left: Mapping[str, str],
    right: Mapping[str, str],
) -> dict[str, Any] | None:
    scores = {
        field: _similarity_basis_points(left[field], right[field])
        for field in _FUNCTION_FIELDS
    }
    matched = [field for field in _FUNCTION_FIELDS if scores[field] >= 8_500]
    mean_score = round(sum(scores.values()) / len(scores))
    if len(matched) < 5 or mean_score < 8_500:
        return None
    return {
        "kind": "narrative_function",
        "score_basis_points": mean_score,
        "matched_dimensions": matched,
    }


def _literal_similarity_layer(
    left: Mapping[str, Any],
    right: Mapping[str, Any],
    *,
    prose: str,
) -> dict[str, Any] | None:
    left_text = "".join(
        prose[span["start"] : span["end"]]
        for span in left["representative_spans"]
    )
    right_text = "".join(
        prose[span["start"] : span["end"]]
        for span in right["representative_spans"]
    )
    if min(len(_normalized_text(left_text)), len(_normalized_text(right_text))) < 16:
        return None
    score = _similarity_basis_points(left_text, right_text)
    if score < 8_000:
        return None
    return {
        "kind": "literal_similarity",
        "score_basis_points": score,
        "matched_dimensions": [],
    }


def _event_fingerprint_layer(
    left: Mapping[str, str],
    right: Mapping[str, str],
) -> dict[str, Any] | None:
    matched = [
        field
        for field in _EVENT_FIELDS
        if _normalized_text(left[field]) == _normalized_text(right[field])
    ]
    if len(matched) != len(_EVENT_FIELDS):
        return None
    return {
        "kind": "event_fingerprint",
        "score_basis_points": 10_000,
        "matched_dimensions": matched,
    }


def _quality_candidate(
    left: Mapping[str, Any],
    right: Mapping[str, Any],
    *,
    layers: list[dict[str, Any]],
) -> dict[str, Any]:
    scene_ids = [str(left["scene_id"]), str(right["scene_id"])]
    signature = _canonical_digest(
        {
            "policy_version": NARRATIVE_REPETITION_SIGNAL_POLICY,
            "scene_ids": scene_ids,
            "layers": [layer["kind"] for layer in layers],
        }
    )
    return {
        "candidate_signature": signature,
        "scene_ids": scene_ids,
        "layers": layers,
        "severity": "quality_debt",
        "hard_gate": False,
        "contract_reference_ids": [],
    }


def _repetition_candidates(
    profiles: Sequence[Mapping[str, Any]],
    *,
    prose: str,
) -> tuple[list[dict[str, Any]], int]:
    candidates: list[dict[str, Any]] = []
    for left, right in combinations(profiles, 2):
        literal_layer = _literal_similarity_layer(
            left,
            right,
            prose=prose,
        )
        event_layer = _event_fingerprint_layer(
            left["event_fingerprint"],
            right["event_fingerprint"],
        )
        function_layer = _narrative_function_layer(
            left["narrative_function"],
            right["narrative_function"],
        )
        layers = [
            layer
            for layer in (literal_layer, event_layer, function_layer)
            if layer is not None
        ]
        if layers:
            candidates.append(
                _quality_candidate(left, right, layers=layers)
            )
    candidates.sort(
        key=lambda candidate: (
            -max(
                layer["score_basis_points"]
                for layer in candidate["layers"]
            ),
            tuple(candidate["scene_ids"]),
        )
    )
    truncated = max(0, len(candidates) - MAX_NARRATIVE_REPETITION_CANDIDATES)
    return candidates[:MAX_NARRATIVE_REPETITION_CANDIDATES], truncated


def _required_text(value: Any, *, field: str) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 500:
        raise NarrativeQualitySignalError(f"{field} is invalid")
    return text


def _validate_spans(
    value: Any,
    *,
    prose: str,
    field: str,
) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not 1 <= len(value) <= 2:
        raise NarrativeQualitySignalError(f"{field} is invalid")
    spans: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise NarrativeQualitySignalError(f"{field} is invalid")
        start = item.get("start")
        end = item.get("end")
        if (
            type(start) is not int
            or type(end) is not int
            or start < 0
            or end <= start
            or end > len(prose)
        ):
            raise NarrativeQualitySignalError(f"{field} is invalid")
        quote_hash = hashlib.sha256(prose[start:end].encode("utf-8")).hexdigest()
        if item.get("quote_hash") != quote_hash:
            raise NarrativeQualitySignalError(f"{field} quote hash changed")
        spans.append({"start": start, "end": end, "quote_hash": quote_hash})
    if spans != sorted(spans, key=lambda item: (item["start"], item["end"])):
        raise NarrativeQualitySignalError(f"{field} order is invalid")
    return spans


def _validate_profiles(
    scene_profiles: Sequence[Mapping[str, Any]],
    *,
    scene_ids: tuple[str, ...],
    prose: str,
) -> list[dict[str, Any]]:
    if not isinstance(scene_profiles, Sequence) or isinstance(
        scene_profiles,
        (str, bytes),
    ):
        raise NarrativeQualitySignalError("scene quality profiles are invalid")
    profiles: list[dict[str, Any]] = []
    for value in scene_profiles:
        if not isinstance(value, Mapping):
            raise NarrativeQualitySignalError("scene quality profile is invalid")
        event = value.get("event_fingerprint")
        function = value.get("narrative_function")
        if not isinstance(event, Mapping) or not isinstance(function, Mapping):
            raise NarrativeQualitySignalError("scene quality profile is incomplete")
        profiles.append(
            {
                "profile_id": _required_text(
                    value.get("profile_id"),
                    field="quality profile identity",
                ),
                "scene_id": _required_text(
                    value.get("scene_id"),
                    field="quality profile scene identity",
                ),
                "representative_spans": _validate_spans(
                    value.get("representative_spans"),
                    prose=prose,
                    field="quality profile spans",
                ),
                "event_fingerprint": {
                    field: _required_text(
                        event.get(field),
                        field=f"event fingerprint {field}",
                    )
                    for field in _EVENT_FIELDS
                },
                "narrative_function": {
                    field: _required_text(
                        function.get(field),
                        field=f"narrative function {field}",
                    )
                    for field in _FUNCTION_FIELDS
                },
            }
        )
    actual_scene_ids = tuple(item["scene_id"] for item in profiles)
    if actual_scene_ids != scene_ids or len(actual_scene_ids) != len(
        set(actual_scene_ids)
    ):
        raise NarrativeQualitySignalError(
            "scene quality profiles do not exactly cover the outline"
        )
    profile_ids = [item["profile_id"] for item in profiles]
    if len(profile_ids) != len(set(profile_ids)):
        raise NarrativeQualitySignalError("scene quality profile identity is duplicated")
    return profiles


def assess_narrative_quality_signals(
    *,
    outline: Mapping[str, Any],
    prose: str,
    scene_profiles: Sequence[Mapping[str, Any]],
    source_prose_run_id: str,
    source_prose_run_revision: int,
    source_content_digest: str,
) -> dict[str, Any]:
    """Build a source-bound sidecar; quality signals never decide the hard gate."""

    if outline.get("scene_contract_version") not in MODERN_SCENE_CONTRACT_VERSIONS:
        raise NarrativeQualitySignalError("quality signals require a V2 outline")
    run_id = str(source_prose_run_id or "").strip()
    if not run_id or len(run_id) > 128:
        raise NarrativeQualitySignalError("quality signal source run is invalid")
    if type(source_prose_run_revision) is not int or source_prose_run_revision < 0:
        raise NarrativeQualitySignalError("quality signal source revision is invalid")
    if source_content_digest != chapter_content_digest(prose):
        raise NarrativeQualitySignalError("quality signal source digest changed")
    scenes = list(outline.get("scenes") or [])
    scene_ids = tuple(str(scene.get("scene_id") or "") for scene in scenes)
    if not scene_ids or any(not scene_id for scene_id in scene_ids):
        raise NarrativeQualitySignalError("quality signal outline scenes are invalid")
    profiles = _validate_profiles(
        scene_profiles,
        scene_ids=scene_ids,
        prose=prose,
    )
    comparison_pair_count = len(scene_ids) * (len(scene_ids) - 1) // 2
    candidates, truncated_candidate_count = _repetition_candidates(
        profiles,
        prose=prose,
    )
    sidecar = {
        "schema_version": NARRATIVE_QUALITY_SIDECAR_SCHEMA,
        "policy_version": NARRATIVE_REPETITION_SIGNAL_POLICY,
        "status": "evaluated",
        "source_binding": {
            "source_prose_run_id": run_id,
            "source_prose_run_revision": source_prose_run_revision,
            "source_content_digest": source_content_digest,
            "outline_contract_digest": _outline_contract_digest(outline),
        },
        "detection_scope": {
            "kind": "current_chapter_scene_pairs",
            "scene_ids": list(scene_ids),
            "scene_count": len(scene_ids),
            "comparison_pair_count": comparison_pair_count,
            "layers": list(NARRATIVE_REPETITION_SIGNAL_LAYERS),
            "maximum_candidates": MAX_NARRATIVE_REPETITION_CANDIDATES,
            "additional_provider_calls": 0,
            "additional_token_bound": 0,
            "second_judge_enabled": False,
        },
        "scene_profiles_digest": _canonical_digest(profiles),
        "candidate_count": len(candidates),
        "truncated_candidate_count": truncated_candidate_count,
        "candidates": candidates,
    }
    projected = {
        **sidecar,
        "sidecar_digest": _canonical_digest(sidecar),
    }
    return NarrativeQualitySidecarSchema.model_validate(projected).model_dump(
        mode="python"
    )
