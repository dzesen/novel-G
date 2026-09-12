"""Local state response diagnostics; never formal state or model context."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
from uuid import uuid4

import anyio

from backend.services.generation.judge_review_records import configured_secrets, sanitize_archive
from backend.services.llm.generation_runtime import StructuredVisibleResponse


from backend.runtime_paths import data_path

STATE_RESPONSE_RECORD_ROOT = data_path("reports", "diagnostics", "state-responses")
STATE_RESPONSE_RECORD_MAX_BYTES = 1_048_576
logger = logging.getLogger(__name__)


class StateResponseRecording:
    def __init__(self, *, request_id: str, novel_id: str, chapter_id: str,
                 proposal_id: str, source_content_digest: str,
                 root: Path | None = None, secrets: tuple[str, ...] | None = None):
        self.secrets = configured_secrets() if secrets is None else secrets
        self.identity = sanitize_archive({
            "request_id": request_id, "novel_id": novel_id, "chapter_id": chapter_id,
            "proposal_id": proposal_id, "source_content_digest": source_content_digest,
        }, self.secrets)
        # External IDs never become filesystem paths; each execution has a new directory.
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.directory = (root or STATE_RESPONSE_RECORD_ROOT) / f"{stamp}-{uuid4().hex}"
        self.ordinal = 0

    async def observe(self, response: StructuredVisibleResponse) -> None:
        raw = asdict(response)
        safe = sanitize_archive(raw, self.secrets)
        ordinal = self.ordinal + 1
        record = {
            "schema_version": "chapter_state_response_record.v1",
            **self.identity,
            "ordinal": ordinal,
            "record_limit_bytes": STATE_RESPONSE_RECORD_MAX_BYTES,
            "content_redacted": safe != raw,
            "response": safe,
        }
        # Each response is durable before Runtime starts repair or publishes a result.
        filename = f"{ordinal:02d}.json"
        path = self.directory / filename
        payload = json.dumps(record, ensure_ascii=False, indent=2) + "\n"

        def write_record():
            self.directory.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".json.tmp")
            try:
                temporary.write_text(payload, encoding="utf-8")
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)

        try:
            with anyio.CancelScope(shield=True):
                await anyio.to_thread.run_sync(write_record)
        except Exception:
            logger.error("state_response_record_failed request_id=%s ordinal=%s", self.identity["request_id"], ordinal)
            raise
        self.ordinal = ordinal
        logger.info(
            "state_response_recorded request_id=%s ordinal=%s phase=%s file=%s truncated=%s redacted=%s",
            self.identity["request_id"], ordinal, safe["phase"],
            path.relative_to(self.directory.parent).as_posix(), safe["truncated"], record["content_redacted"],
        )
