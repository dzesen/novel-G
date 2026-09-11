"""Strict read-only proof for a positively rejected legacy initial review."""
from __future__ import annotations
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from backend.services.generation.candidate_repair_contracts import (
    CandidatePipelineCheckpointV1, ProseCandidateCheckpointV1,
    ReviewGenerationRejectedCheckpointV9, is_safe_candidate_identifier,
    parse_candidate_pipeline_checkpoint,
)
from backend.services.llm.generation_runtime import safe_structured_repair_failure_diagnostics

@dataclass(frozen=True)
class RejectedReviewEvidence:
    attempt_ids: tuple[str, ...]
    retry_index: int
    diagnostics: Mapping[str, Any]
    failure_event_id: str


def _utc(value: Any) -> datetime | None:
    if not isinstance(value, datetime): return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def recognize_legacy_review_rejection(
    job: Mapping[str, Any], *, execution_id: str, novel_id: str,
    chapter_id: str, readiness_digest: str, narrative_revision: int,
    checkpoints: tuple[CandidatePipelineCheckpointV1, ...],
    attempt_slots: Sequence[Mapping[str, Any]],
) -> RejectedReviewEvidence | None:
    readiness=job.get('readiness')
    if (
        str(job.get('_id') or '')!=execution_id or str(job.get('novel_id') or '')!=novel_id
        or job.get('expected_narrative_revision')!=narrative_revision
        or not isinstance(readiness,Mapping) or readiness.get('digest')!=readiness_digest
        or job.get('has_uncertain_attempts') or not checkpoints
        or not isinstance(checkpoints[0],ProseCandidateCheckpointV1)
        or checkpoints[0].origin!='initial' or checkpoints[0].cycle!=0
        or any(c.chapter_id!=chapter_id for c in checkpoints)
        or any(not isinstance(c,ReviewGenerationRejectedCheckpointV9) or c.cycle!=0
               or c.source!=checkpoints[0].source or c.retry_index!=index
               for index,c in enumerate(checkpoints[1:]))
    ): return None
    try:
        stored=tuple(parse_candidate_pipeline_checkpoint(c) for c in job.get('candidate_pipeline_checkpoints',()) if isinstance(c,Mapping) and c.get('chapter_id')==chapter_id)
    except (TypeError,ValueError): return None
    if stored!=checkpoints: return None
    slots=[s for s in attempt_slots if str(s.get('chapter_id') or '')==chapter_id]
    durable=[s for s in job.get('attempt_slots',()) if isinstance(s,Mapping) and str(s.get('chapter_id') or '')==chapter_id]
    if slots!=durable: return None
    candidates=[s for s in slots if str(s.get('step_id') or '').startswith('candidate-')]
    prefix_ids=tuple(a for c in checkpoints for a in c.attempt_ids)
    ids=tuple(str(s.get('attempt_id') or '') for s in candidates)
    if ids[:len(prefix_ids)]!=prefix_ids or len(set(ids))!=len(ids):return None
    tail=candidates[len(prefix_ids):]
    previous=checkpoints[-1]
    expected_step=f'candidate-outline-adherence-retry:{previous.sequence}' if isinstance(previous,ReviewGenerationRejectedCheckpointV9) else 'candidate-outline-adherence'
    if (not 2<=len(tail)<=3 or any(s.get('step_id')!=expected_step or s.get('state')!='accounted' for s in tail)
        or tuple(s.get('phase') for s in tail) not in {('primary','repair'),('primary','schema_fallback','repair')}):return None
    settled=[_utc(s.get('accounted_at')) for s in candidates];claimed=[_utc(s.get('claimed_at')) for s in candidates]
    if (any(t is None for t in (*settled,*claimed))
        or any(start>end for start,end in zip(claimed,settled))
        or any(end>following for end,following in zip(settled,claimed[1:]))):return None
    events=job.get('diagnostics')
    if not isinstance(events,list):return None
    for event in events:
        if not isinstance(event,Mapping):continue
        details=event.get('details');occurred_at=_utc(event.get('occurred_at'));event_id=event.get('event_id')
        if (event.get('schema_version')!=1 or event.get('source')!='runtime'
            or event.get('step')!='candidate_pipeline' or event.get('chapter_id')!=chapter_id
            or event.get('category')!='validation_logic' or event.get('code') not in {'review_evidence_invalid','structured_output_truncated'}
            or event.get('evidence')!='confirmed' or not isinstance(details,Mapping)
            or type(details.get('attempt_count')) is not int or details['attempt_count']!=len(candidates)
            or occurred_at is None or occurred_at<settled[-1]
            or not is_safe_candidate_identifier(event_id,maximum=240)):continue
        safe=safe_structured_repair_failure_diagnostics(details.get('structured_validation'))
        if safe is None or any(safe.get(k) not in {'stop','length'} for k in ('primary_finish_reason','repair_finish_reason')):continue
        if event.get('code')=='structured_output_truncated' and (details.get('reason_codes')!=['review_evidence_invalid'] or 'length' not in (safe['primary_finish_reason'],safe['repair_finish_reason'])):continue
        return RejectedReviewEvidence(ids[len(prefix_ids):],len(checkpoints)-1,safe,event_id)
    return None
