"""Human-readable review evidence. Never an LLM context or completion proof."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
import hashlib
import json
import re
from typing import Any

from bson import ObjectId

from backend.config.config import get_all_config
from backend.db.repositories.judge_review_record_repository import judge_review_record_repo
from backend.db.utils import get_utc_now


_PRIVATE_KEYS = re.compile(r"^(?:reasoning(?:_content)?|thinking(?:_content)?|api[_-]?key|access_token|refresh_token|authorization|password|secret)$", re.I)
_PRIVATE_FIELD = re.compile(r'"(?:reasoning(?:_content)?|thinking(?:_content)?|api[_-]?key|access_token|refresh_token|authorization|password|secret)"\s*:', re.I)
_THINK = re.compile(r"<(think|thinking|reasoning)\b[^>]*>.*?(?:</\1\s*>|$)", re.I | re.S)
_SECRET_TOKEN = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{8,}|Bearer\s+[A-Za-z0-9._~+/-]{8,}=*)", re.I)


def configured_secrets() -> tuple[str, ...]:
    # Read credential values only to remove exact accidental echoes. They are
    # never attached to a record, diagnostic, exception, prompt or log.
    values = []

    def visit(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if _PRIVATE_KEYS.fullmatch(str(key)) and isinstance(child, str) and len(child) >= 6:
                    values.append(child)
                elif isinstance(child, (dict, list)):
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(get_all_config())
    return tuple(sorted(set(values), key=len, reverse=True))


def sanitize_archive(value: Any, secrets: tuple[str, ...] = ()) -> Any:
    if isinstance(value, dict):
        return {str(key): "[REDACTED]" if _PRIVATE_KEYS.fullmatch(str(key)) else sanitize_archive(child, secrets) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize_archive(child, secrets) for child in value]
    if not isinstance(value, str):
        return value
    text = _THINK.sub("[REDACTED]", value)
    for secret in secrets:
        text = text.replace(secret, "[REDACTED]")
    text = _SECRET_TOKEN.sub("[REDACTED]", text)
    if _PRIVATE_FIELD.search(text):
        try:
            parsed = json.loads(_unfence(text))
        except (ValueError, RecursionError):
            # For partial/malformed JSON we cannot safely locate the private
            # value's end. Preserve the prefix and omit the remainder.
            text = _PRIVATE_FIELD.split(text, maxsplit=1)[0] + "[REDACTED PRIVATE FIELD]"
        else:
            text = json.dumps(sanitize_archive(parsed, secrets), ensure_ascii=False)
    return text


def _unfence(text: str) -> str:
    return re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I)


def _public(value):
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat() + ("Z" if value.tzinfo is None else "")
    if isinstance(value, dict):
        return {("id" if key == "_id" else key): _public(child) for key, child in value.items() if key not in {"owner_id", "is_deleted", "deleted_at"}}
    if isinstance(value, list):
        return [_public(child) for child in value]
    return value


def legacy_review_records(job: dict, chapter_id: str) -> list[dict]:
    """Project only surviving chapter evidence, with explicit missing fields."""
    records = []
    attempts = [item for item in job.get("attempt_slots") or [] if str(item.get("chapter_id")) == chapter_id]

    def append_record(suffix, *, evidence, failure, source, step_id, model=None, alias=None, diagnostics=None):
        slots = [item for item in attempts if item.get("step_id") == step_id]
        if (
            isinstance(evidence, dict)
            and evidence.get("schema_version") == "chapter_not_reviewed.v1"
            and not slots and not failure
        ):
            return  # An authorized omission is not a failed historical Judge call.
        if not evidence and not failure and not slots:
            return
        rounds = [{
            "ordinal": index + 1, "phase": slot.get("phase"), "attempt_id": slot.get("attempt_id"),
            "provider_alias": slot.get("provider_alias"), "model": model,
            "started_at": slot.get("claimed_at"), "finished_at": slot.get("accounted_at"),
            "duration_ms": None, "accounting_state": slot.get("state"),
            "usage": slot.get("usage") if slot.get("state") == "accounted" else None,
            "finish_reason": slot.get("finish_reason", "unreported"),
            "visible_text": None, "representation": "unavailable", "parsed_json": None,
            "local_validation": "not_recorded", "validation_issues": None,
            "response_complete": False, "truncated": False, "redacted": False,
        } for index, slot in enumerate(slots)]
        decision = evidence.get("decision") if isinstance(evidence, dict) else None
        usage = {key: sum(int(item["usage"].get(key) or 0) for item in rounds) for key in ("input_tokens", "output_tokens", "total_tokens")} if rounds and all(isinstance(item["usage"], dict) for item in rounds) else None
        records.append({
            "_id": f"legacy:{job['_id']}:{suffix}", "origin": "legacy_job", "job_id": job["_id"],
            "novel_id": job.get("novel_id"), "chapter_id": chapter_id, "step_id": step_id,
            "status": "pass" if decision == "pass" else "needs_review" if decision == "manual_review" else "findings" if decision == "repair" else "review_incomplete",
            "decision": decision, "evidence": evidence, "evidence_excerpts": [],
            "failure_code": failure if not evidence else None, "diagnostics": diagnostics,
            "created_at": job.get("created_at"), "duration_ms": None, "round_count": len(rounds), "rounds": rounds,
            "provider_alias": alias or (rounds[0]["provider_alias"] if rounds else None), "model": model,
            "source_run_id": source.get("source_run_id") or source.get("prose_run_id") or source.get("source_prose_run_id"),
            "source_run_revision": source.get("source_run_revision") or source.get("prose_run_revision") or source.get("source_prose_run_revision"),
            "source_content_digest": source.get("source_content_digest") or source.get("content_digest"),
            "outline_digest": source.get("outline_revision"), "view_digest": source.get("view_digest"),
            "review_protocol": None, "usage": usage, "cost": None, "cost_basis": "unavailable",
            "issue_count": len(evidence.get("local_issues") or []) if evidence else None,
        })

    readiness = job.get("readiness") or {}
    source = readiness.get("source_binding") or {}
    if job.get("scope") == "interactive_completion" and str(source.get("chapter_id") or job.get("current_chapter_id")) == chapter_id:
        progress = job.get("interactive_completion_progress") or {}
        judge_progress = progress if progress.get("stage") == "outline_adherence" else {}
        provider = (readiness.get("generation_plans") or {}).get("adherence") or {}
        append_record("interactive", evidence=(job.get("interactive_completion_evidence") or {}).get("outline_adherence"),
            failure=judge_progress.get("failure_code"), source=source, step_id="interactive-adherence",
            alias=provider.get("provider_alias") or judge_progress.get("provider_alias"),
            model=provider.get("provider_model") or judge_progress.get("provider_model"),
            diagnostics=judge_progress.get("validation_diagnostics"))
    journal = job.get("required_adherence_journal") or {}
    if str((journal.get("binding") or {}).get("chapter_id")) == chapter_id:
        for index, entry in enumerate(journal.get("entries") or []):
            checkpoint = entry.get("checkpoint") or {}
            receipt = checkpoint.get("receipt") or {}
            append_record(str(index), evidence=checkpoint.get("evidence"), failure=checkpoint.get("failure_code"),
                source=receipt, step_id=f"required-adherence:{receipt.get('view_digest', '')}")
    return records


class JudgeReviewRecording:
    def __init__(self, service, *, owner_id, novel_id, chapter_id, job_id=None, step_id=None):
        self.service = service
        self.identity = {"owner_id": owner_id, "novel_id": novel_id, "chapter_id": chapter_id, "job_id": job_id, "step_id": step_id}
        self.record_id = None
        self.ordinal = 0
        self.rounds = []
        self.secrets = ()
        self.snapshot = None

    def _scope(self):
        return {"owner_id": self.identity["owner_id"], "chapter_id": self.identity["chapter_id"], "record_id": self.record_id}

    async def begin(self, snapshot, plan):
        self.snapshot = snapshot
        self.secrets = self.service.secret_supplier()
        self.record_id = await self.service.repo.begin({
            **self.identity, "schema_version": "judge_review_record.v1", "origin": "archive",
            "source_run_id": snapshot.source_run_id, "source_run_revision": snapshot.source_run_revision,
            "source_content_digest": snapshot.source_content_digest,
            "outline_digest": hashlib.sha256(snapshot.outline_json.encode("utf-8")).hexdigest(),
            "view_digest": snapshot.view_digest, "review_protocol": plan.protocol,
            "review_contract_digest": plan.contract_digest,
            "provider_alias": sanitize_archive(plan.generation.provider_alias, self.secrets),
            "model": sanitize_archive(plan.generation.provider_model, self.secrets),
            "writer_model": sanitize_archive(plan.writer_model, self.secrets),
            "cost": None, "cost_basis": "unavailable",
        })

    async def observe(self, response):
        data = asdict(response)
        safe = sanitize_archive(data, self.secrets)
        safe["redacted"] = data != safe
        # Redaction can slightly expand JSON; clip only the display text.
        encoded = safe["visible_text"].encode("utf-8")
        if len(encoded) > 65_536:
            safe["visible_text"] = encoded[:65_536].decode("utf-8", errors="ignore")
            safe["truncated"] = True
        await self.service.repo.append_round(**self._scope(), ordinal=self.ordinal + 1, response=safe)
        self.ordinal += 1
        self.rounds.append(safe)

    async def finish(self, result=None, *, failure_code=None):
        if self.record_id is None:
            return
        evidence = result.evidence if result is not None else None
        failure = failure_code or (result.failure_code if result is not None else None)
        decision = evidence.get("decision") if evidence else None
        status = "review_incomplete" if failure or not evidence else ("pass" if decision == "pass" else "needs_review" if decision == "manual_review" else "findings")
        data = {
            "status": status, "decision": decision, "failure_code": failure,
            "evidence": evidence, "diagnostics": getattr(result, "diagnostics", None),
            "evidence_excerpts": self._excerpts(evidence),
            "finished_at": get_utc_now(), "duration_ms": sum(item["duration_ms"] for item in self.rounds),
            "usage": result.usage.model_dump() if result is not None and self.rounds and all(item["usage"] is not None for item in self.rounds) else None,
            "issue_count": len(evidence.get("local_issues") or []) if evidence else None,
        }
        safe = sanitize_archive(data, self.secrets)
        safe["content_redacted"] = safe != data
        await self.service.repo.finish(**self._scope(), result=safe)

    def _excerpts(self, evidence):
        excerpts = []
        if not evidence or self.snapshot is None:
            return excerpts

        def visit(value, path):
            if isinstance(value, dict):
                start, end, digest = value.get("start"), value.get("end"), value.get("quote_hash")
                if type(start) is int and type(end) is int and isinstance(digest, str):
                    if 0 <= start < end <= len(self.snapshot.prose) and end - start <= 500:
                        quote = self.snapshot.prose[start:end]
                        if hashlib.sha256(quote.encode("utf-8")).hexdigest() == digest:
                            excerpts.append({"path": path, "start": start, "end": end, "quote": quote})
                else:
                    for key, child in value.items():
                        visit(child, f"{path}.{key}" if path else key)
            elif isinstance(value, list):
                for index, child in enumerate(value):
                    visit(child, f"{path}[{index}]")

        visit(evidence, "")
        return excerpts


class JudgeReviewRecordService:
    def __init__(self, repo=judge_review_record_repo, *, secret_supplier=configured_secrets):
        self.repo = repo
        self.secret_supplier = secret_supplier

    def recording(self, **identity):
        return JudgeReviewRecording(self, **identity)

    async def list_chapter(self, *, owner_id, chapter_id, before=None):
        rows = await self.repo.list_chapter(owner_id=owner_id, chapter_id=chapter_id, before=before, limit=21)
        result = rows[:20]
        legacy = []
        if before is None:
            archived = {(str(row.get("job_id")), row.get("step_id")) for row in result}
            for job in await self.repo.legacy_jobs(owner_id=owner_id, chapter_id=chapter_id):
                for record in legacy_review_records(job, chapter_id):
                    if (str(record.get("job_id")), record.get("step_id")) not in archived:
                        legacy.append({key: value for key, value in record.items() if key not in {"rounds", "evidence", "evidence_excerpts", "diagnostics"}})
        return {"records": _public(sanitize_archive(result, self.secret_supplier())), "legacy_records": _public(sanitize_archive(legacy, self.secret_supplier())), "next_cursor": str(result[-1]["_id"]) if len(rows) > 20 else None}

    async def detail(self, *, owner_id, chapter_id, record_id):
        if record_id.startswith("legacy:"):
            if not re.fullmatch(r"legacy:[0-9a-f]{24}:(?:interactive|[0-2])", record_id):
                raise ValueError("invalid legacy review record identity")
            jobs = await self.repo.legacy_jobs(owner_id=owner_id, chapter_id=chapter_id, job_id=record_id.split(":")[1])
            candidates = [record for job in jobs for record in legacy_review_records(job, chapter_id) if record["_id"] == record_id]
            if not candidates:
                from backend.db.errors import NotFoundError

                raise NotFoundError("审查记录不存在")
            record = candidates[0]
        else:
            record = await self.repo.detail(owner_id=owner_id, chapter_id=chapter_id, record_id=record_id)
        record = sanitize_archive(record, self.secret_supplier())
        for response in record.get("rounds") or []:
            if not isinstance(response.get("visible_text"), str):
                continue
            try:
                response["parsed_json"] = json.loads(_unfence(response["visible_text"]))
            except (ValueError, RecursionError):
                response["parsed_json"] = None
        return _public(record)


judge_review_records = JudgeReviewRecordService()
