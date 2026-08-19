"""Pure value parsing for persisted prose-remediation receipt pointers."""

from __future__ import annotations

from typing import Any, Mapping


REMEDIATION_RECEIPT_POINTER_SCHEMA = (
    "prose_remediation_receipt_pointer.v1"
)


class InvalidRemediationReceiptPointer(ValueError):
    """A persisted latest-receipt pointer is not a valid V1 value."""


def parse_remediation_receipt_pointer(
    document: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Return one normalized pointer, or ``None`` when no pointer exists."""
    remediation = document.get("remediation")
    if remediation in (None, {}):
        return None
    if not isinstance(remediation, Mapping):
        raise InvalidRemediationReceiptPointer(
            "prose remediation projection is invalid"
        )
    raw_pointer = remediation.get("latest_receipt")
    if raw_pointer in (None, {}):
        return None
    if not isinstance(raw_pointer, Mapping):
        raise InvalidRemediationReceiptPointer(
            "prose remediation receipt pointer is invalid"
        )
    pointer = dict(raw_pointer)
    source_revision = pointer.get("source_revision")
    result_revision = pointer.get("result_revision")
    if (
        isinstance(source_revision, bool)
        or not isinstance(source_revision, int)
        or source_revision <= 0
        or isinstance(result_revision, bool)
        or not isinstance(result_revision, int)
        or result_revision <= 0
    ):
        raise InvalidRemediationReceiptPointer(
            "prose remediation receipt revision is invalid"
        )
    if (
        pointer.get("schema_version")
        != REMEDIATION_RECEIPT_POINTER_SCHEMA
        or not str(pointer.get("idempotency_key") or "")
        or not str(pointer.get("request_digest") or "")
        or not isinstance(pointer.get("result_projection"), Mapping)
    ):
        raise InvalidRemediationReceiptPointer(
            "prose remediation receipt pointer is incomplete"
        )
    pointer["source_revision"] = source_revision
    pointer["result_revision"] = result_revision
    pointer["result_projection"] = dict(pointer["result_projection"])
    return pointer
