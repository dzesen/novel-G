"""Durable, redacted attempt journal for one authorized Judge probe.

The real synthetic probe must survive process loss without silently issuing a
second paid request.  Each state transition is stored as one immutable journal
snapshot under ``reports/``.  This module constructs the filesystem adapter but
performs no I/O until ``open`` or an AttemptScope method is explicitly awaited.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
import asyncio
import json
import os
from pathlib import Path
import re
from typing import Annotated, Any, Callable, Literal, Protocol, Union
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.evaluation.required_book_successor_judge_probe import (
    REQUIRED_JUDGE_PROBE_AUTHORIZATION_CODES,
    RequiredJudgeCapabilityProbeReceipt,
    parse_required_judge_capability_probe_receipt,
    required_judge_probe_digest,
    validate_required_judge_capability_probe_readiness,
)
from backend.evaluation.required_book_successor_judge_probe_execution import (
    RequiredJudgeCapabilityProbeExecution,
    RequiredJudgeCapabilityProbeReport,
    RequiredJudgeCapabilityProbeRunner,
    RequiredJudgeProbeAttemptEvidence,
    parse_required_judge_capability_probe_report,
    validate_required_judge_capability_probe_execution,
    validate_required_judge_capability_probe_report,
)
from backend.llm.models import TokenUsage
from backend.runtime_reports import REPORTS_DIR
from backend.services.llm.generation_runtime import AttemptUsage


_SHA256 = r"^[0-9a-f]{64}$"
_ATTEMPT_ID = r"^[0-9a-f]{32}$"
_MAX = 2**63 - 1
_SNAPSHOT_NAME = re.compile(r"^(?P<event_count>\d{3})\.journal\.json$")
_MAX_SNAPSHOT_BYTES = 500_000
_DIRECTORY_NAME = "required-judge-capability-probe-v1"


class RequiredJudgeProbeJournalConflict(ValueError):
    """A durable prefix no longer matches the authorized probe."""


class RequiredJudgeProbeAttemptBudgetExceeded(RuntimeError):
    """Observed usage crossed the exact per-attempt reservation."""


class _Closed(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        hide_input_in_errors=True,
    )


class _AttemptClaimed(_Closed):
    kind: Literal["attempt_claimed"] = "attempt_claimed"
    attempt_id: str = Field(pattern=_ATTEMPT_ID)
    provider_alias: str = Field(min_length=1, max_length=160)
    phase: Literal["primary", "repair"]
    conservative_tokens: int = Field(ge=1, le=_MAX)
    reserved_input_tokens: int = Field(ge=1, le=_MAX)
    reserved_output_tokens: int = Field(ge=1, le=_MAX)
    reserved_total_tokens: int = Field(ge=2, le=_MAX)

    @model_validator(mode="after")
    def validate_reservation(self) -> "_AttemptClaimed":
        if (
            self.reserved_total_tokens
            != self.reserved_input_tokens + self.reserved_output_tokens
            or self.conservative_tokens > self.reserved_total_tokens
        ):
            raise ValueError("required_judge_probe_reservation_invalid")
        return self


class _AttemptAccounted(_Closed):
    kind: Literal["attempt_accounted"] = "attempt_accounted"
    attempt_id: str = Field(pattern=_ATTEMPT_ID)
    observed_input_tokens: int = Field(ge=0, le=_MAX)
    observed_output_tokens: int = Field(ge=0, le=_MAX)
    observed_total_tokens: int = Field(ge=0, le=_MAX)
    charged_input_tokens: int = Field(ge=1, le=_MAX)
    charged_output_tokens: int = Field(ge=1, le=_MAX)
    charged_total_tokens: int = Field(ge=2, le=_MAX)
    usage_complete: bool
    reservation_exceeded: bool

    @model_validator(mode="after")
    def validate_usage(self) -> "_AttemptAccounted":
        complete = (
            self.observed_input_tokens > 0
            and self.observed_output_tokens > 0
            and self.observed_total_tokens
            == self.observed_input_tokens + self.observed_output_tokens
        )
        if (
            self.usage_complete != complete
            or self.charged_total_tokens
            != self.charged_input_tokens + self.charged_output_tokens
        ):
            raise ValueError("required_judge_probe_accounting_invalid")
        return self


class _AttemptUncertain(_Closed):
    kind: Literal["attempt_uncertain"] = "attempt_uncertain"
    attempt_id: str = Field(pattern=_ATTEMPT_ID)
    reason_code: Literal["provider_result_unknown"] = (
        "provider_result_unknown"
    )


class _AttemptReleased(_Closed):
    kind: Literal["attempt_released"] = "attempt_released"
    attempt_id: str = Field(pattern=_ATTEMPT_ID)
    reason_code: Literal["not_dispatched"] = "not_dispatched"


class _FinishReasonRecorded(_Closed):
    kind: Literal["finish_reason_recorded"] = "finish_reason_recorded"
    attempt_id: str = Field(pattern=_ATTEMPT_ID)
    finish_reason: Literal[
        "stop",
        "length",
        "tool_call",
        "content_filter",
        "cancelled",
        "error",
        "unreported",
    ]
    raw_finish_reason_digest: str = Field(pattern=_SHA256)


class _ExecutionPublished(_Closed):
    kind: Literal["execution_published"] = "execution_published"
    report: RequiredJudgeCapabilityProbeReport
    receipt: RequiredJudgeCapabilityProbeReceipt | None = None

    @model_validator(mode="after")
    def validate_terminal(self) -> "_ExecutionPublished":
        if (
            self.report.status == "passed"
            and self.receipt is None
        ) or (
            self.report.status == "failed"
            and self.receipt is not None
        ):
            raise ValueError("required_judge_probe_terminal_invalid")
        return self


_Event = Annotated[
    Union[
        _AttemptClaimed,
        _AttemptAccounted,
        _AttemptUncertain,
        _AttemptReleased,
        _FinishReasonRecorded,
        _ExecutionPublished,
    ],
    Field(discriminator="kind"),
]


class RequiredJudgeProbeAttemptJournal(_Closed):
    schema_version: Literal["required_judge_probe_attempt_journal.v1"] = (
        "required_judge_probe_attempt_journal.v1"
    )
    journal_digest: str = Field(pattern=_SHA256)
    probe_readiness_digest: str = Field(pattern=_SHA256)
    authorization_contract_digest: str = Field(pattern=_SHA256)
    acknowledged_authorization_codes: tuple[str, str]
    created_at: datetime
    events: tuple[_Event, ...] = Field(max_length=8)

    @model_validator(mode="after")
    def validate_journal(self) -> "RequiredJudgeProbeAttemptJournal":
        if (
            self.created_at.tzinfo is None
            or self.acknowledged_authorization_codes
            != REQUIRED_JUDGE_PROBE_AUTHORIZATION_CODES
        ):
            raise ValueError("required_judge_probe_journal_invalid")
        _replay(self.events)
        identity = self.model_dump(mode="python", exclude={"journal_digest"})
        if required_judge_probe_digest(identity) != self.journal_digest:
            raise ValueError("required_judge_probe_journal_changed")
        return self


class RequiredJudgeProbeTerminalEvidence(_Closed):
    """Replayable, redacted proof consumed by the three-chapter gate."""

    schema_version: Literal["required_judge_probe_terminal_evidence.v1"] = (
        "required_judge_probe_terminal_evidence.v1"
    )
    evidence_digest: str = Field(pattern=_SHA256)
    probe_readiness: dict[str, Any]
    journal: RequiredJudgeProbeAttemptJournal
    receipt: RequiredJudgeCapabilityProbeReceipt

    @model_validator(mode="after")
    def validate_evidence(self) -> "RequiredJudgeProbeTerminalEvidence":
        _validate_terminal_probe_evidence_parts(
            readiness=self.probe_readiness,
            journal=self.journal,
            receipt=self.receipt,
        )
        identity = self.model_dump(
            mode="python",
            exclude={"evidence_digest"},
        )
        if required_judge_probe_digest(identity) != self.evidence_digest:
            raise ValueError("required_judge_probe_terminal_evidence_changed")
        return self


def _make_journal(
    *,
    readiness_digest: str,
    authorization_contract_digest: str,
    created_at: datetime,
    events: tuple[_Event, ...] = (),
) -> RequiredJudgeProbeAttemptJournal:
    identity = {
        "schema_version": "required_judge_probe_attempt_journal.v1",
        "probe_readiness_digest": readiness_digest,
        "authorization_contract_digest": authorization_contract_digest,
        "acknowledged_authorization_codes": (
            REQUIRED_JUDGE_PROBE_AUTHORIZATION_CODES
        ),
        "created_at": created_at,
        "events": events,
    }
    return RequiredJudgeProbeAttemptJournal(
        **identity,
        journal_digest=required_judge_probe_digest(identity),
    )


def parse_required_judge_probe_attempt_journal(
    value: Any,
) -> RequiredJudgeProbeAttemptJournal:
    try:
        return RequiredJudgeProbeAttemptJournal.model_validate_json(
            json.dumps(
                value.model_dump(mode="json")
                if isinstance(value, BaseModel)
                else value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    except (TypeError, ValueError) as exc:
        raise RequiredJudgeProbeJournalConflict(
            "required judge probe journal is invalid"
        ) from exc


def _replay(events: tuple[_Event, ...]) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    terminal_seen = False
    phases: list[str] = []
    for index, event in enumerate(events):
        if terminal_seen:
            raise ValueError("required_judge_probe_event_after_terminal")
        if isinstance(event, _AttemptClaimed):
            if event.attempt_id in records or len(records) >= 2:
                raise ValueError("required_judge_probe_attempt_sequence_invalid")
            phases.append(event.phase)
            if phases != ["primary", "repair"][: len(phases)]:
                raise ValueError("required_judge_probe_attempt_phase_invalid")
            records[event.attempt_id] = {
                "claim": event,
                "state": "claimed",
                "account": None,
                "finish": None,
            }
            continue
        if isinstance(event, _ExecutionPublished):
            if index != len(events) - 1:
                raise ValueError("required_judge_probe_terminal_order_invalid")
            terminal_seen = True
            continue
        record = records.get(event.attempt_id)
        if record is None:
            raise ValueError("required_judge_probe_attempt_unknown")
        if isinstance(event, _AttemptAccounted):
            if record["state"] != "claimed":
                raise ValueError("required_judge_probe_attempt_transition_invalid")
            claim = record["claim"]
            exceeded = (
                event.observed_input_tokens > claim.reserved_input_tokens
                or event.observed_output_tokens > claim.reserved_output_tokens
                or event.observed_total_tokens > claim.reserved_total_tokens
            )
            if event.reservation_exceeded != exceeded:
                raise ValueError("required_judge_probe_reservation_projection_changed")
            expected_charged = (
                (
                    event.observed_input_tokens,
                    event.observed_output_tokens,
                    event.observed_total_tokens,
                )
                if event.usage_complete
                else (
                    claim.reserved_input_tokens,
                    claim.reserved_output_tokens,
                    claim.reserved_total_tokens,
                )
            )
            if expected_charged != (
                event.charged_input_tokens,
                event.charged_output_tokens,
                event.charged_total_tokens,
            ):
                raise ValueError("required_judge_probe_charge_projection_changed")
            record["state"] = "accounted"
            record["account"] = event
        elif isinstance(event, _AttemptUncertain):
            if record["state"] != "claimed":
                raise ValueError("required_judge_probe_attempt_transition_invalid")
            record["state"] = "uncertain"
        elif isinstance(event, _AttemptReleased):
            if record["state"] != "claimed":
                raise ValueError("required_judge_probe_attempt_transition_invalid")
            record["state"] = "released_pre_dispatch"
        elif isinstance(event, _FinishReasonRecorded):
            if record["state"] != "accounted" or record["finish"] is not None:
                raise ValueError("required_judge_probe_finish_sequence_invalid")
            record["finish"] = event
    return records


def _validate_terminal_probe_evidence_parts(
    *,
    readiness: Mapping[str, Any],
    journal: RequiredJudgeProbeAttemptJournal,
    receipt: RequiredJudgeCapabilityProbeReceipt,
) -> RequiredJudgeCapabilityProbeExecution:
    authorization = validate_required_judge_capability_probe_readiness(
        readiness
    )
    checked_journal = parse_required_judge_probe_attempt_journal(journal)
    if (
        checked_journal.probe_readiness_digest != readiness.get("digest")
        or checked_journal.authorization_contract_digest
        != authorization.contract_digest
        or checked_journal.created_at < authorization.created_at
        or checked_journal.created_at > authorization.deadline_at
        or not checked_journal.events
        or not isinstance(checked_journal.events[-1], _ExecutionPublished)
    ):
        raise RequiredJudgeProbeJournalConflict(
            "required judge probe terminal evidence binding changed"
        )
    terminal = checked_journal.events[-1]
    execution = validate_required_judge_capability_probe_execution(
        RequiredJudgeCapabilityProbeExecution(
            terminal.report,
            terminal.receipt,
        ),
        readiness=readiness,
    )
    if (
        execution.report.status != "passed"
        or execution.receipt is None
        or execution.receipt != receipt
    ):
        raise RequiredJudgeProbeJournalConflict(
            "required judge probe terminal evidence is not a pass"
        )

    records = _replay(checked_journal.events)
    attempts: list[RequiredJudgeProbeAttemptEvidence] = []
    final_accounted_finish: _FinishReasonRecorded | None = None
    for attempt_id, record in records.items():
        claim = record["claim"]
        if (
            claim.provider_alias
            != authorization.generation_plan.provider_alias
            or claim.reserved_input_tokens
            != authorization.review_input_token_bound
            or claim.reserved_output_tokens
            != authorization.generation_plan.max_output_tokens
            or claim.reserved_total_tokens
            != authorization.review_input_token_bound
            + authorization.generation_plan.max_output_tokens
        ):
            raise RequiredJudgeProbeJournalConflict(
                "required judge probe terminal attempt binding changed"
            )
        if record["state"] == "released_pre_dispatch":
            continue
        account = record["account"]
        finish = record["finish"]
        if (
            record["state"] != "accounted"
            or account is None
            or finish is None
            or account.usage_complete is not True
            or account.reservation_exceeded is not False
        ):
            raise RequiredJudgeProbeJournalConflict(
                "required judge probe terminal attempt is not settled"
            )
        attempts.append(
            RequiredJudgeProbeAttemptEvidence(
                attempt_id_digest=required_judge_probe_digest(
                    {"attempt_id": attempt_id}
                ),
                provider_alias=claim.provider_alias,
                phase=claim.phase,
                input_tokens=account.charged_input_tokens,
                output_tokens=account.charged_output_tokens,
                total_tokens=account.charged_total_tokens,
            )
        )
        final_accounted_finish = finish
    attempts_tuple = tuple(attempts)
    attempt_set_digest = required_judge_probe_digest([
        item.model_dump(mode="json") for item in attempts_tuple
    ])
    if (
        not attempts_tuple
        or attempts_tuple != execution.report.attempts
        or final_accounted_finish is None
        or final_accounted_finish.finish_reason != "stop"
        or receipt.accounted_attempt_ids_digest != attempt_set_digest
    ):
        raise RequiredJudgeProbeJournalConflict(
            "required judge probe terminal attempt evidence changed"
        )
    return execution


def build_required_judge_probe_terminal_evidence(
    *,
    readiness: Mapping[str, Any],
    journal: RequiredJudgeProbeAttemptJournal | Mapping[str, Any],
) -> RequiredJudgeProbeTerminalEvidence:
    checked_journal = parse_required_judge_probe_attempt_journal(journal)
    if not checked_journal.events or not isinstance(
        checked_journal.events[-1],
        _ExecutionPublished,
    ):
        raise RequiredJudgeProbeJournalConflict(
            "required judge probe terminal execution is missing"
        )
    terminal_receipt = checked_journal.events[-1].receipt
    if terminal_receipt is None:
        raise RequiredJudgeProbeJournalConflict(
            "required judge probe terminal receipt is missing"
        )
    _validate_terminal_probe_evidence_parts(
        readiness=readiness,
        journal=checked_journal,
        receipt=terminal_receipt,
    )
    identity = {
        "schema_version": "required_judge_probe_terminal_evidence.v1",
        "probe_readiness": dict(readiness),
        "journal": checked_journal,
        "receipt": terminal_receipt,
    }
    return RequiredJudgeProbeTerminalEvidence(
        **identity,
        evidence_digest=required_judge_probe_digest(identity),
    )


def validate_required_judge_probe_terminal_evidence(
    value: Any,
) -> RequiredJudgeProbeTerminalEvidence:
    try:
        return RequiredJudgeProbeTerminalEvidence.model_validate_json(
            json.dumps(
                value.model_dump(mode="json")
                if isinstance(value, BaseModel)
                else value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
        )
    except (TypeError, ValueError) as exc:
        raise RequiredJudgeProbeJournalConflict(
            "required judge probe terminal evidence is invalid"
        ) from exc


class RequiredJudgeProbeSnapshotDirectory(Protocol):
    async def read_snapshots(self) -> Mapping[str, bytes]: ...

    async def write_exclusive(self, name: str, payload: bytes) -> bool: ...


class MemoryRequiredJudgeProbeSnapshotDirectory:
    def __init__(self) -> None:
        self.snapshots: dict[str, bytes] = {}

    async def read_snapshots(self) -> Mapping[str, bytes]:
        return dict(self.snapshots)

    async def write_exclusive(self, name: str, payload: bytes) -> bool:
        if name in self.snapshots:
            return False
        self.snapshots[name] = bytes(payload)
        return True


class FileRequiredJudgeProbeSnapshotDirectory:
    """Host-local Adapter; construction alone performs no filesystem I/O."""

    def __init__(self, readiness_digest: str) -> None:
        if re.fullmatch(_SHA256, readiness_digest) is None:
            raise ValueError("required judge probe readiness digest is invalid")
        reports = REPORTS_DIR.resolve()
        root = (reports / _DIRECTORY_NAME).resolve()
        path = (root / readiness_digest).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ValueError("required judge probe path escaped reports") from exc
        self.path = path

    async def read_snapshots(self) -> Mapping[str, bytes]:
        return await asyncio.to_thread(self._read_snapshots)

    async def write_exclusive(self, name: str, payload: bytes) -> bool:
        return await asyncio.to_thread(self._write_exclusive, name, payload)

    def _read_snapshots(self) -> dict[str, bytes]:
        if not self.path.exists():
            return {}
        if not self.path.is_dir():
            raise RequiredJudgeProbeJournalConflict(
                "required judge probe journal path is not a directory"
            )
        result: dict[str, bytes] = {}
        for item in self.path.iterdir():
            if item.name.startswith(".snapshot-") and item.suffix == ".tmp":
                continue
            if not item.is_file():
                raise RequiredJudgeProbeJournalConflict(
                    "required judge probe journal directory changed"
                )
            payload = item.read_bytes()
            if not payload or len(payload) > _MAX_SNAPSHOT_BYTES:
                raise RequiredJudgeProbeJournalConflict(
                    "required judge probe snapshot is invalid"
                )
            result[item.name] = payload
        return result

    def _write_exclusive(self, name: str, payload: bytes) -> bool:
        if _SNAPSHOT_NAME.fullmatch(name) is None:
            raise ValueError("required judge probe snapshot name is invalid")
        if not payload or len(payload) > _MAX_SNAPSHOT_BYTES:
            raise ValueError("required judge probe snapshot payload is invalid")
        self.path.mkdir(parents=True, exist_ok=True)
        destination = (self.path / name).resolve()
        if destination.parent != self.path:
            raise ValueError("required judge probe snapshot path escaped")
        temporary = self.path / f".snapshot-{uuid4().hex}.tmp"
        try:
            with temporary.open("xb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, destination)
            except FileExistsError:
                return False
            return True
        finally:
            temporary.unlink(missing_ok=True)


def _snapshot_payload(journal: RequiredJudgeProbeAttemptJournal) -> bytes:
    payload = (
        json.dumps(
            journal.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    if len(payload) > _MAX_SNAPSHOT_BYTES:
        raise RequiredJudgeProbeJournalConflict(
            "required judge probe snapshot is oversized"
        )
    return payload


class RequiredJudgeProbeSnapshotStore:
    def __init__(self, directory: RequiredJudgeProbeSnapshotDirectory) -> None:
        self._directory = directory

    async def load(self) -> RequiredJudgeProbeAttemptJournal | None:
        raw = await self._directory.read_snapshots()
        if not raw:
            return None
        parsed: dict[int, RequiredJudgeProbeAttemptJournal] = {}
        for name, payload in raw.items():
            match = _SNAPSHOT_NAME.fullmatch(str(name))
            if (
                match is None
                or not isinstance(payload, bytes)
                or not payload
                or len(payload) > _MAX_SNAPSHOT_BYTES
            ):
                raise RequiredJudgeProbeJournalConflict(
                    "required judge probe snapshot set changed"
                )
            try:
                value = json.loads(payload.decode("utf-8"))
            except (UnicodeError, ValueError) as exc:
                raise RequiredJudgeProbeJournalConflict(
                    "required judge probe snapshot is invalid"
                ) from exc
            journal = parse_required_judge_probe_attempt_journal(value)
            count = int(match.group("event_count"))
            if count in parsed or len(journal.events) != count:
                raise RequiredJudgeProbeJournalConflict(
                    "required judge probe snapshot identity changed"
                )
            parsed[count] = journal
        if sorted(parsed) != list(range(len(parsed))):
            raise RequiredJudgeProbeJournalConflict(
                "required judge probe snapshot sequence changed"
            )
        previous: RequiredJudgeProbeAttemptJournal | None = None
        for count in range(len(parsed)):
            current = parsed[count]
            if count == 0:
                if current.events:
                    raise RequiredJudgeProbeJournalConflict(
                        "required judge probe initial snapshot changed"
                    )
            elif (
                previous is None
                or current.probe_readiness_digest
                != previous.probe_readiness_digest
                or current.authorization_contract_digest
                != previous.authorization_contract_digest
                or current.acknowledged_authorization_codes
                != previous.acknowledged_authorization_codes
                or current.created_at != previous.created_at
                or current.events[:-1] != previous.events
            ):
                raise RequiredJudgeProbeJournalConflict(
                    "required judge probe snapshot forked"
                )
            previous = current
        return previous

    async def create(self, journal: RequiredJudgeProbeAttemptJournal) -> bool:
        candidate = parse_required_judge_probe_attempt_journal(journal)
        if candidate.events or await self.load() is not None:
            return False
        return await self._directory.write_exclusive(
            "000.journal.json",
            _snapshot_payload(candidate),
        )

    async def append(
        self,
        journal: RequiredJudgeProbeAttemptJournal,
        *,
        expected_journal_digest: str,
    ) -> bool:
        candidate = parse_required_judge_probe_attempt_journal(journal)
        current = await self.load()
        if (
            current is None
            or current.journal_digest != expected_journal_digest
            or candidate.events[:-1] != current.events
            or len(candidate.events) != len(current.events) + 1
            or candidate.probe_readiness_digest
            != current.probe_readiness_digest
            or candidate.authorization_contract_digest
            != current.authorization_contract_digest
            or candidate.acknowledged_authorization_codes
            != current.acknowledged_authorization_codes
            or candidate.created_at != current.created_at
        ):
            return False
        return await self._directory.write_exclusive(
            f"{len(candidate.events):03d}.journal.json",
            _snapshot_payload(candidate),
        )


class DurableRequiredJudgeProbeAttemptScope:
    """GenerationRuntime AttemptScope backed by immutable host snapshots."""

    def __init__(
        self,
        *,
        store: RequiredJudgeProbeSnapshotStore,
        readiness: Mapping[str, Any],
        journal: RequiredJudgeProbeAttemptJournal,
    ) -> None:
        self._store = store
        self._readiness = dict(readiness)
        self._authorization = (
            validate_required_judge_capability_probe_readiness(readiness)
        )
        self._journal = parse_required_judge_probe_attempt_journal(journal)
        if (
            self._journal.probe_readiness_digest != readiness.get("digest")
            or self._journal.authorization_contract_digest
            != self._authorization.contract_digest
        ):
            raise RequiredJudgeProbeJournalConflict(
                "required judge probe journal binding changed"
            )

    @classmethod
    async def open(
        cls,
        *,
        store: RequiredJudgeProbeSnapshotStore,
        readiness: Mapping[str, Any],
        acknowledged_authorization_codes: tuple[str, ...],
        opened_at: datetime,
    ) -> "DurableRequiredJudgeProbeAttemptScope":
        authorization = validate_required_judge_capability_probe_readiness(
            readiness
        )
        if (
            acknowledged_authorization_codes
            != REQUIRED_JUDGE_PROBE_AUTHORIZATION_CODES
            or opened_at.tzinfo is None
            or opened_at < authorization.created_at
        ):
            raise RequiredJudgeProbeJournalConflict(
                "required judge probe journal authority is invalid"
            )
        journal = await store.load()
        if journal is None:
            if opened_at > authorization.deadline_at:
                raise RequiredJudgeProbeJournalConflict(
                    "required judge probe journal authority expired"
                )
            journal = _make_journal(
                readiness_digest=str(readiness["digest"]),
                authorization_contract_digest=authorization.contract_digest,
                created_at=opened_at,
            )
            if not await store.create(journal):
                journal = await store.load()
                if journal is None:
                    raise RequiredJudgeProbeJournalConflict(
                        "required judge probe journal claim was lost"
                    )
        return cls(store=store, readiness=readiness, journal=journal)

    def _records(self) -> dict[str, dict[str, Any]]:
        return _replay(self._journal.events)

    async def _append(self, event: _Event) -> None:
        previous = self._journal
        candidate = _make_journal(
            readiness_digest=previous.probe_readiness_digest,
            authorization_contract_digest=(
                previous.authorization_contract_digest
            ),
            created_at=previous.created_at,
            events=(*previous.events, event),
        )
        if not await self._store.append(
            candidate,
            expected_journal_digest=previous.journal_digest,
        ):
            raise RequiredJudgeProbeJournalConflict(
                "required judge probe journal CAS failed"
            )
        self._journal = candidate

    @property
    def attempts(self) -> tuple[AttemptUsage, ...]:
        result: list[AttemptUsage] = []
        for attempt_id, record in self._records().items():
            account = record["account"]
            if record["state"] != "accounted" or account is None:
                continue
            claim = record["claim"]
            result.append(
                AttemptUsage(
                    attempt_id=attempt_id,
                    provider_alias=claim.provider_alias,
                    phase=claim.phase,
                    usage=TokenUsage(
                        input_tokens=account.charged_input_tokens,
                        output_tokens=account.charged_output_tokens,
                        total_tokens=account.charged_total_tokens,
                    ),
                )
            )
        return tuple(result)

    @property
    def claimed_attempt_ids(self) -> tuple[str, ...]:
        return tuple(self._records())

    @property
    def uncertain_attempt_ids(self) -> tuple[str, ...]:
        return tuple(
            attempt_id
            for attempt_id, record in self._records().items()
            if record["state"] in {"claimed", "uncertain"}
        )

    @property
    def usage_complete(self) -> bool:
        records = self._records()
        return all(
            record["state"] != "accounted"
            or (
                record["account"] is not None
                and record["account"].usage_complete
            )
            for record in records.values()
        )

    @property
    def terminal_execution(
        self,
    ) -> RequiredJudgeCapabilityProbeExecution | None:
        terminal = next(
            (
                event
                for event in self._journal.events
                if isinstance(event, _ExecutionPublished)
            ),
            None,
        )
        if terminal is None:
            return None
        return RequiredJudgeCapabilityProbeExecution(
            terminal.report,
            terminal.receipt,
        )

    async def claim(self, provider_alias: str, phase: str) -> str:
        raise ValueError(
            "required judge probe attempts need a conservative token bound"
        )

    async def claim_with_budget(
        self,
        provider_alias: str,
        phase: str,
        conservative_tokens: int | None,
    ) -> str:
        records = self._records()
        if (
            self.terminal_execution is not None
            or any(
                record["state"] in {"claimed", "uncertain"}
                for record in records.values()
            )
            or len(records) >= self._authorization.maximum_provider_attempts
            or provider_alias
            != self._authorization.generation_plan.provider_alias
            or phase not in {"primary", "repair"}
            or phase
            != ("primary" if not records else "repair")
            or type(conservative_tokens) is not int
            or conservative_tokens <= 0
        ):
            raise RequiredJudgeProbeJournalConflict(
                "required judge probe attempt claim is invalid"
            )
        output_tokens = int(
            self._authorization.generation_plan.max_output_tokens
        )
        attempt_id = uuid4().hex
        await self._append(
            _AttemptClaimed(
                attempt_id=attempt_id,
                provider_alias=provider_alias,
                phase=phase,
                conservative_tokens=conservative_tokens,
                reserved_input_tokens=(
                    self._authorization.review_input_token_bound
                ),
                reserved_output_tokens=output_tokens,
                reserved_total_tokens=(
                    self._authorization.review_input_token_bound
                    + output_tokens
                ),
            )
        )
        return attempt_id

    async def account(self, attempt_id: str, usage: TokenUsage) -> None:
        records = self._records()
        record = records.get(attempt_id)
        if record is None or record["state"] != "claimed":
            raise RequiredJudgeProbeJournalConflict(
                "required judge probe account transition is invalid"
            )
        claim = record["claim"]
        complete = (
            usage.input_tokens > 0
            and usage.output_tokens > 0
            and usage.total_tokens
            == usage.input_tokens + usage.output_tokens
        )
        exceeded = (
            usage.input_tokens > claim.reserved_input_tokens
            or usage.output_tokens > claim.reserved_output_tokens
            or usage.total_tokens > claim.reserved_total_tokens
        )
        charged = (
            usage
            if complete
            else TokenUsage(
                input_tokens=claim.reserved_input_tokens,
                output_tokens=claim.reserved_output_tokens,
                total_tokens=claim.reserved_total_tokens,
            )
        )
        await self._append(
            _AttemptAccounted(
                attempt_id=attempt_id,
                observed_input_tokens=usage.input_tokens,
                observed_output_tokens=usage.output_tokens,
                observed_total_tokens=usage.total_tokens,
                charged_input_tokens=charged.input_tokens,
                charged_output_tokens=charged.output_tokens,
                charged_total_tokens=charged.total_tokens,
                usage_complete=complete,
                reservation_exceeded=exceeded,
            )
        )
        if exceeded:
            raise RequiredJudgeProbeAttemptBudgetExceeded(
                "required judge probe attempt exceeded authorization"
            )

    async def account_with_observed_usage(
        self,
        attempt_id: str,
        usage: TokenUsage,
    ) -> None:
        await self.account(attempt_id, usage)

    async def mark_uncertain(self, attempt_id: str, _reason: str) -> None:
        record = self._records().get(attempt_id)
        if record is None or record["state"] != "claimed":
            raise RequiredJudgeProbeJournalConflict(
                "required judge probe uncertain transition is invalid"
            )
        await self._append(_AttemptUncertain(attempt_id=attempt_id))

    async def release_pre_dispatch(self, attempt_id: str, _reason: str) -> None:
        record = self._records().get(attempt_id)
        if record is None or record["state"] != "claimed":
            raise RequiredJudgeProbeJournalConflict(
                "required judge probe release transition is invalid"
            )
        await self._append(_AttemptReleased(attempt_id=attempt_id))

    async def record_finish_reason(
        self,
        attempt_id: str,
        finish_reason: str,
        raw_finish_reason: str,
    ) -> None:
        record = self._records().get(attempt_id)
        if (
            record is None
            or record["state"] != "accounted"
            or record["finish"] is not None
        ):
            raise RequiredJudgeProbeJournalConflict(
                "required judge probe finish transition is invalid"
            )
        await self._append(
            _FinishReasonRecorded(
                attempt_id=attempt_id,
                finish_reason=finish_reason,
                raw_finish_reason_digest=required_judge_probe_digest(
                    {"raw_finish_reason": raw_finish_reason}
                ),
            )
        )

    async def publish_execution(
        self,
        execution: RequiredJudgeCapabilityProbeExecution,
    ) -> None:
        report = validate_required_judge_capability_probe_report(
            execution.report,
            readiness=self._readiness,
        )
        receipt = (
            parse_required_judge_capability_probe_receipt(execution.receipt)
            if execution.receipt is not None
            else None
        )
        records = self._records()
        if (
            self.terminal_execution is not None
            or report.accounted_attempt_count
            != sum(
                record["state"] == "accounted"
                for record in records.values()
            )
            or report.uncertain_attempt_count
            != sum(
                record["state"] in {"claimed", "uncertain"}
                for record in records.values()
            )
            or (
                receipt is not None
                and (
                    receipt.probe_report_sha256 != report.report_digest
                    or receipt.probe_readiness_digest
                    != self._journal.probe_readiness_digest
                    or receipt.accounted_attempt_count
                    != report.accounted_attempt_count
                    or receipt.input_tokens != report.input_tokens
                    or receipt.output_tokens != report.output_tokens
                )
            )
        ):
            raise RequiredJudgeProbeJournalConflict(
                "required judge probe terminal binding changed"
            )
        await self._append(
            _ExecutionPublished(report=report, receipt=receipt)
        )


def production_required_judge_probe_store(
    readiness_digest: str,
) -> RequiredJudgeProbeSnapshotStore:
    """Construct the reports-directory store without reading or writing it."""

    return RequiredJudgeProbeSnapshotStore(
        FileRequiredJudgeProbeSnapshotDirectory(readiness_digest)
    )


async def production_required_judge_probe_runner(
    *,
    readiness: Mapping[str, Any],
    acknowledged_authorization_codes: tuple[str, ...],
    now_supplier: Callable[[], datetime],
) -> RequiredJudgeCapabilityProbeRunner:
    """Open the durable journal and compose the real runtime without a call."""

    from backend.services.llm.generation_runtime import (
        create_generation_runtime,
    )

    authorization = validate_required_judge_capability_probe_readiness(
        readiness
    )
    opened_at = now_supplier()
    store = production_required_judge_probe_store(str(readiness["digest"]))
    scope = await DurableRequiredJudgeProbeAttemptScope.open(
        store=store,
        readiness=readiness,
        acknowledged_authorization_codes=acknowledged_authorization_codes,
        opened_at=opened_at,
    )
    runtime = create_generation_runtime(
        scope,
        max_provider_retries=0,
    )
    if (
        authorization.maximum_provider_attempts != 2
        or authorization.generation_plan.max_semantic_attempts != 2
    ):
        raise RequiredJudgeProbeJournalConflict(
            "required judge probe production attempt bound changed"
        )
    return RequiredJudgeCapabilityProbeRunner(
        runtime,
        now_supplier=now_supplier,
        attempt_scope=scope,
    )


__all__ = [
    "DurableRequiredJudgeProbeAttemptScope",
    "FileRequiredJudgeProbeSnapshotDirectory",
    "MemoryRequiredJudgeProbeSnapshotDirectory",
    "RequiredJudgeProbeAttemptBudgetExceeded",
    "RequiredJudgeProbeAttemptJournal",
    "RequiredJudgeProbeJournalConflict",
    "RequiredJudgeProbeSnapshotDirectory",
    "RequiredJudgeProbeSnapshotStore",
    "RequiredJudgeProbeTerminalEvidence",
    "build_required_judge_probe_terminal_evidence",
    "parse_required_judge_probe_attempt_journal",
    "production_required_judge_probe_runner",
    "production_required_judge_probe_store",
    "validate_required_judge_probe_terminal_evidence",
]
