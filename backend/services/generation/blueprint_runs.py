"""Shared blueprint readiness, execution and recovery over the workflow runtime."""
from __future__ import annotations

import asyncio
from contextlib import suppress
from copy import deepcopy
import json
from typing import Any, Callable
from uuid import uuid4

from bson import ObjectId

from backend.db.repositories.blueprint_run_repository import (
    BlueprintRunConflict, BlueprintRunRepository, blueprint_run_repo, content_digest,
)
from backend.services.generation.author_brief import AuthorBrief
from backend.services.generation.blueprint_attempt_scope import BlueprintAttemptScope
from backend.services.generation.blueprint_authorization import (
    plan_from_record, plan_record, prepare_blueprint_authorization, request_record,
)
from backend.services.generation.blueprint_workflow import (
    BLUEPRINT_WORKFLOW_PROTOCOL, BlueprintGenerationRequest, BlueprintGenerationStartRequest,
    blueprint_result, blueprint_workflow, workflow_params,
)
from backend.services.llm.workflow_runner import WorkflowDeps, run_workflow, sse_event


class BlueprintRunService:
    HEARTBEAT_SECONDS = 10

    def __init__(self, *, runtime_factory: Callable, prompt_supplier: Callable,
                 repo: BlueprintRunRepository = blueprint_run_repo):
        self.runtime_factory = runtime_factory
        self.prompt_supplier = prompt_supplier
        self.repo = repo

    @staticmethod
    def _validate(run: dict) -> dict:
        authorization = run["authorization"]
        if (authorization["workflow_protocol"] != BLUEPRINT_WORKFLOW_PROTOCOL
                or authorization["owner_id"] != str(run["owner_id"])
                or authorization["run_id"] != str(run["_id"])
                or authorization["draft_id"] != run["draft_id"]
                or content_digest(authorization) != run["authorization_digest"]):
            raise BlueprintRunConflict("blueprint_readiness_stale")
        brief = AuthorBrief.from_record(authorization["author_brief"])
        request = BlueprintGenerationRequest.model_validate(authorization["request"])
        workflow_name, steps = blueprint_workflow(request.strategy)
        if (authorization["strategy"] != request.strategy
                or authorization["workflow_name"] != workflow_name
                or authorization["step_order"] != [step.key for step in steps]):
            raise BlueprintRunConflict("blueprint_readiness_stale")
        values = {}
        for step in steps:
            candidate = run["candidates"].get(step.key)
            if candidate is None:
                break
            expected = authorization["prefix_sources"].get(step.key) or {
                "digest": candidate["digest"], "source_run_id": str(run["_id"]),
                "authorization_digest": run["authorization_digest"],
            }
            if (candidate["digest"] != content_digest(candidate["value"])
                    or candidate["author_brief_revision"] != brief.revision
                    or any(candidate.get(key) != value for key, value in expected.items())):
                raise BlueprintRunConflict("blueprint_candidate_source_invalid")
            values[step.key] = step.schema.model_validate(candidate["value"])
        if set(values) != set(run["candidates"]):
            raise BlueprintRunConflict("blueprint_candidate_prefix_invalid")
        return values

    async def inspect(self, request: BlueprintGenerationRequest, owner_id: str) -> dict:
        if request.cached_steps is not None:
            raise BlueprintRunConflict("blueprint_legacy_cache_not_authorized")
        candidates, source_summary = {}, None
        if request.reuse_run_id:
            previous = await self.repo.get_run(request.reuse_run_id, owner_id)
            self._validate(previous)
            if previous["status"] == "running":
                raise BlueprintRunConflict("blueprint_source_run_must_stop")
            brief = workflow_params(request)["author_brief"]
            if (previous["authorization"]["author_brief"]["revision"] != brief["revision"]
                    or previous["authorization"]["strategy"] != request.strategy
                    or previous["authorization"]["request"].get("card_imports", []) != request.model_dump(mode="json")["card_imports"]
                    or (request.draft_id and previous["draft_id"] != request.draft_id)):
                raise BlueprintRunConflict("blueprint_source_input_changed")
            request = request.model_copy(update={"draft_id": previous["draft_id"]})
            candidates = deepcopy(previous["candidates"])
            earlier = previous["authorization"].get("source_summary") or {}
            source_summary = {
                "run_id": str(previous["_id"]), "authorization_digest": previous["authorization_digest"],
                "calls_used": previous["calls_reserved"] + int(earlier.get("calls_used", 0)),
                "tokens_used": previous["tokens_used"] + int(earlier.get("tokens_used", 0)),
                "has_uncertain": previous["has_uncertain"] or bool(earlier.get("has_uncertain")),
            }
        request = request.model_copy(update={"draft_id": request.draft_id or uuid4().hex})
        run_id = str(ObjectId())
        authorization, report = prepare_blueprint_authorization(
            request, run_id=run_id, owner_id=owner_id,
            runtime=self.runtime_factory(max_provider_retries=0),
            all_prompts=self.prompt_supplier(), candidates=candidates, source_summary=source_summary,
        )
        await self.repo.create_readiness(run_id=run_id, owner_id=owner_id, draft_id=request.draft_id,
            authorization=authorization, report=report, candidates=candidates)
        return report

    def _validate_pending_plans(self, run: dict, *, check_current_prompts: bool):
        runtime = self.runtime_factory(max_provider_retries=0)
        authorization = run["authorization"]
        if check_current_prompts and content_digest(self.prompt_supplier()[authorization["workflow_name"]]) != authorization["prompt_revision"]:
            raise BlueprintRunConflict("blueprint_readiness_stale")
        for step, record in authorization["plans"].items():
            if step not in run["candidates"]:
                frozen = plan_from_record(record)
                if plan_record(runtime.plan_structured(frozen.target)) != record:
                    raise BlueprintRunConflict("blueprint_readiness_stale")

    async def start(self, request: BlueprintGenerationStartRequest, owner_id: str) -> dict:
        run = await self.repo.get_run(request.run_id, owner_id)
        self._validate(run)
        source = request_record(request)
        source["draft_id"] = source["draft_id"] or run["draft_id"]
        if source != run["authorization"]["request"]:
            raise BlueprintRunConflict("blueprint_readiness_stale")
        if run["status"] != "completed":
            self._validate_pending_plans(run, check_current_prompts=True)
        return await self.repo.acquire(request.run_id, owner_id, digest=request.readiness_digest,
            acknowledge_automatic_budget=request.acknowledge_automatic_token_budget,
            acknowledge_uncertain_source=request.acknowledge_uncertain_source)

    async def resume(self, run_id: str, owner_id: str, digest: str) -> dict:
        run = await self.repo.get_run(run_id, owner_id)
        self._validate(run)
        if run["status"] != "completed":
            # Continue the original frozen prompts; a new preview uses new ones.
            self._validate_pending_plans(run, check_current_prompts=False)
        return await self.repo.acquire(run_id, owner_id, digest=digest, resume=True)

    @classmethod
    def public_view(cls, run: dict, *, summary: bool = False) -> dict:
        values = run["completed_steps"] if summary else cls._validate(run)
        authorization = run["authorization"]
        source = authorization.get("source_summary") or {}
        view = {
            "schema_version": "blueprint_run.v1", "run_id": str(run["_id"]),
            "draft_id": run["draft_id"], "status": run["status"], "revision": run["revision"],
            "authorization_digest": run["authorization_digest"],
            "author_brief_revision": authorization["author_brief"]["revision"],
            "prompt_revision": authorization["prompt_revision"],
            "strategy": authorization["strategy"], "current_step": run["current_step"],
            "completed_steps": list(values), "cancel_requested": run["cancel_requested"],
            "calls_used": run["calls_reserved"], "tokens_used": run["tokens_used"],
            "tokens_reserved": run["tokens_reserved"], "has_uncertain": run["has_uncertain"],
            "cumulative_calls_used": run["calls_reserved"] + int(source.get("calls_used", 0)),
            "cumulative_tokens_used": run["tokens_used"] + int(source.get("tokens_used", 0)),
            "failure_code": run["failure_code"],
            "created_at": run["created_at"], "updated_at": run["updated_at"],
        }
        if not summary:
            view.update({
                "request": authorization["request"], "readiness": run["readiness"],
                "cached_steps": {key: value.model_dump(mode="json") for key, value in values.items()},
                "result": blueprint_result(authorization["strategy"], values) if run["status"] == "completed" else None,
                "attempts": [{
                    "attempt_id": key, "step": item["step"], "provider_alias": item["provider_alias"],
                    "phase": item["phase"], "state": item["state"], "usage": item["usage"],
                    "reserved_tokens": item["reserved_tokens"],
                } for key, item in run["attempts"].items()],
            })
        return view

    async def stream(self, run: dict, *, is_disconnected=None):
        run_id, owner_id = str(run["_id"]), str(run["owner_id"])
        if run["status"] == "completed":
            view = self.public_view(run)
            yield sse_event("done", {"success": True, "result": view["result"], "run_id": run_id, "reused": True})
            return
        token = run["lease"]["token"]
        args = (run_id, owner_id, token)
        owner_task = asyncio.current_task()
        checkpoint_failed = False

        async def heartbeat():
            try:
                while True:
                    await asyncio.sleep(self.HEARTBEAT_SECONDS)
                    await self.repo.heartbeat(*args)
            except asyncio.CancelledError:
                raise
            except Exception:
                if owner_task is not None:
                    owner_task.cancel()

        async def started(step):
            await self.repo.begin_step(*args, step)

        async def completed(step, value, usage):
            nonlocal checkpoint_failed
            try:
                await self.repo.checkpoint(*args, step, value=value.model_dump(mode="json"), usage=usage.model_dump())
            except BaseException:
                checkpoint_failed = True
                raise

        async def failed(step, error):
            current = await self.repo.get_run(run_id, owner_id)
            has_step_attempts = any(item["step"] == step and item["state"] != "released" for item in current["attempts"].values())
            if checkpoint_failed or (getattr(error, "provider_request_not_dispatched", False) and not has_step_attempts):
                await self.repo.suspend(*args)
            else:
                await self.repo.finish(*args, success=False,
                    failure_code=getattr(error, "code", None) or getattr(error, "diagnostic_code", None) or "blueprint_generation_failed")

        frames = None
        heartbeat_task = None
        try:
            scope = BlueprintAttemptScope(self.repo, run)
            execution = self.runtime_factory(attempt_scope=scope, max_provider_retries=0)
            authorization = run["authorization"]
            workflow_name, steps = blueprint_workflow(authorization["strategy"])
            frames = run_workflow(
                workflow_name=workflow_name, steps=steps, prompts=authorization["prompts"],
                params=workflow_params(BlueprintGenerationRequest.model_validate(authorization["request"])),
                gen_kwargs=authorization["generation_params"], cached=self._validate(run),
                deps=WorkflowDeps(runtime=execution,
                    structured_plans={key: plan_from_record(value) for key, value in authorization["plans"].items()},
                    on_step_started=started, on_step_completed=completed, on_step_failed=failed),
                request_id=run_id, is_disconnected=is_disconnected, log_partial_on_disconnect=False,
            )
            heartbeat_task = asyncio.create_task(heartbeat())
            yield sse_event("run", {"run_id": run_id, "draft_id": run["draft_id"], "authorization_digest": run["authorization_digest"]})
            async for frame in frames:
                if frame.startswith("event: done\n"):
                    payload = json.loads(frame.split("data: ", 1)[1])
                    current = await self.repo.get_run(run_id, owner_id)
                    if current["status"] == "running":
                        current = await self.repo.finish(*args, success=payload.get("success") is True)
                    payload["run_id"] = run_id
                    payload["run_status"] = current["status"]
                    if payload.get("success") and current["status"] != "completed":
                        raise BlueprintRunConflict("blueprint_checkpoints_incomplete")
                    if payload.get("success"):
                        payload["result"] = blueprint_result(authorization["strategy"], self._validate(current))
                    yield sse_event("done", payload)
                else:
                    yield frame
        finally:
            if heartbeat_task is not None:
                heartbeat_task.cancel()
                with suppress(asyncio.CancelledError):
                    await heartbeat_task
            if frames is not None:
                await frames.aclose()
            # A crash after settlement but before checkpoint is deliberately
            # uncertain; a completed checkpoint remains reusable with no call.
            await self.repo.suspend(*args)
