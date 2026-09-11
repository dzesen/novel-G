"""Deterministic, source-bound selectors for exact state evidence spans."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib

STATE_SOURCE_REFERENCE_PREFIX = "[[state-source:"
MAX_STATE_SOURCE_CHARS = 500


@dataclass(frozen=True)
class StateEvidenceSource:
    reference: str
    start: int
    end: int
    quote: str

    def as_span(self) -> dict[str, int | str]:
        return {"start": self.start, "end": self.end, "quote": self.quote}


def build_state_evidence_catalog(prose: str) -> tuple[StateEvidenceSource, ...]:
    """Use full source digests and exact offsets; repeated lines stay distinct."""
    source_digest = hashlib.sha256(prose.encode("utf-8")).hexdigest()
    entries = []
    offset = 0
    for line in prose.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        for local_start in range(0, len(body), MAX_STATE_SOURCE_CHARS):
            quote = body[local_start:local_start + MAX_STATE_SOURCE_CHARS]
            if not quote.strip():
                continue
            start = offset + local_start
            entries.append(StateEvidenceSource(
                reference=f"{STATE_SOURCE_REFERENCE_PREFIX}{source_digest}:{len(entries) + 1}]]",
                start=start, end=start + len(quote), quote=quote,
            ))
        offset += len(line)
    return tuple(entries)


def render_state_evidence_source(prose: str) -> str:
    """Labels are prompt-only; the original prose remains the canonical source."""
    return "\n\n".join(f"{entry.reference}\n{entry.quote}"
                         for entry in build_state_evidence_catalog(prose))
