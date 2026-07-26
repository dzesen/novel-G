"""Auditable Agent revision proposals with stale-safe, recoverable acceptance."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Literal

from bson import ObjectId
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pymongo import ReturnDocument

from backend.db import collections
from backend.db.errors import NotFoundError
from backend.db.mongo import get_database
from backend.db.mutation import MutationCommand, commit_mutation
from backend.db.narrative_revision import narrative_revision_store
from backend.db.repositories.chapter_repository import chapter_repo
from backend.db.repositories.volume_repository import volume_repo
from backend.db.utils import get_utc_now, to_object_id
from backend.services.llm.agent_run import AgentRunStore, agent_run_store
from backend.services.novel.chapter_service import count_chapter_words
from backend.services.novel.derived_stats import derived_stats


RevisionTargetKind = Literal[
    "volume_outline",
    "chapter_outline",
    "scene",
    "chapter_prose",
]
RevisionSourceKind = Literal["creative_idea", "continuity_issue"]


class RevisionProposalConflict(ValueError):
    """The proposal state/version does not permit the requested transition."""


class StaleRevisionProposal(RevisionProposalConflict):
    """The run context or target asset changed after the proposal was captured."""


class RevisionTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: RevisionTargetKind
    volume_id: str | None = Field(default=None, min_length=1, max_length=64)
    chapter_id: str | None = Field(default=None, min_length=1, max_length=64)
    scene_index: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_identity(self):
        if self.kind == "volume_outline" and not self.volume_id:
            raise ValueError("volume_outline target requires volume_id")
        if self.kind in {"chapter_outline", "scene", "chapter_prose"}:
            if not self.chapter_id:
                raise ValueError(f"{self.kind} target requires chapter_id")
        if self.kind == "scene" and self.scene_index is None:
            raise ValueError("scene target requires scene_index")
        if self.kind != "scene" and self.scene_index is not None:
            raise ValueError("scene_index is only valid for scene targets")
        return self


class RevisionPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str | None = Field(default=None, max_length=20_000)
    arc: str | None = Field(default=None, max_length=20_000)
    core_conflict: str | None = Field(default=None, max_length=4_000)
    ending_hook: str | None = Field(default=None, max_length=4_000)
    scene_summary: str | None = Field(default=None, max_length=4_000)
    scene_purpose: str | None = Field(default=None, max_length=2_000)
    content: str | None = Field(default=None, max_length=1_000_000)


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _jsonable(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, datetime):
        normalized = (
            value.replace(tzinfo=timezone.utc)
            if value.tzinfo is None
            else value.astimezone(timezone.utc)
        )
        return normalized.isoformat()
    return value


def _digest(value: Any) -> str:
    encoded = json.dumps(
        _jsonable(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _proposal_view(document: dict[str, Any]) -> dict[str, Any]:
    return {
        "proposal_id": str(document["_id"]),
        "novel_id": str(document["novel_id"]),
        "actor_id": str(document["actor_id"]),
        "run_id": str(document["run_id"]),
        "source_kind": str(document.get("source_kind") or ""),
        "source_index": int(document.get("source_index") or 0),
        "source": _jsonable(document.get("source") or {}),
        "agent": _jsonable(document.get("agent") or {}),
        "context_snapshot": _jsonable(document.get("context_snapshot") or {}),
        "target": _jsonable(document.get("target") or {}),
        "target_revision": str(document.get("target_revision") or ""),
        "patch": _jsonable(document.get("patch") or {}),
        "status": str(document.get("status") or ""),
        "version": int(document.get("version") or 1),
        "acceptance": _jsonable(document.get("acceptance")),
        "rejection": _jsonable(document.get("rejection")),
        "stale_reason": document.get("stale_reason"),
        "created_at": document.get("created_at"),
        "updated_at": document.get("updated_at"),
    }


class AgentRevisionProposalService:
    def __init__(self, run_store: AgentRunStore = agent_run_store) -> None:
        self.run_store = run_store

    @property
    def collection(self):
        return get_database()[collections.AGENT_REVISION_PROPOSALS]

    @staticmethod
    def _validate_patch(
        target: RevisionTarget,
        patch: RevisionPatch,
    ) -> dict[str, Any]:
        values = patch.model_dump(exclude_none=True)
        allowed = {
            "volume_outline": {"summary", "arc"},
            "chapter_outline": {"core_conflict", "ending_hook"},
            "scene": {"scene_summary", "scene_purpose"},
            "chapter_prose": {"content"},
        }[target.kind]
        unknown = set(values) - allowed
        if unknown:
            raise ValueError(
                f"{target.kind} proposal does not accept: {', '.join(sorted(unknown))}"
            )
        if not values:
            raise ValueError("Revision proposal patch must change at least one field")
        if target.kind == "scene" and not {
            "scene_summary",
            "scene_purpose",
        }.intersection(values):
            raise ValueError("Scene proposal must change summary or purpose")
        return values

    @staticmethod
    def _source_from_run(
        run: dict[str, Any],
        source_kind: RevisionSourceKind,
        source_index: int,
    ) -> dict[str, Any]:
        if source_index < 0:
            raise ValueError("source_index must be non-negative")
        result = run.get("result") or {}
        if source_kind == "creative_idea":
            if run.get("capability") != "creative_inspiration":
                raise ValueError("Run does not contain creative ideas")
            items = result.get("ideas") or []
        else:
            if run.get("capability") != "continuity_review":
                raise ValueError("Run does not contain continuity issues")
            items = result.get("issues") or []
        try:
            source = items[source_index]
        except (IndexError, TypeError) as exc:
            raise ValueError("Selected Agent result no longer exists") from exc
        if not isinstance(source, dict):
            raise ValueError("Selected Agent result is malformed")
        return deepcopy(source)

    @staticmethod
    def _validate_source_target(
        source_kind: RevisionSourceKind,
        target_kind: RevisionTargetKind,
    ) -> None:
        allowed = {
            "creative_idea": {"volume_outline", "chapter_outline", "scene"},
            "continuity_issue": {"scene", "chapter_prose"},
        }[source_kind]
        if target_kind not in allowed:
            raise ValueError(
                f"{source_kind} cannot create a {target_kind} proposal"
            )

    @staticmethod
    async def _load_target(
        novel_id: str,
        target: RevisionTarget,
        *,
        session: Any = None,
    ) -> dict[str, Any]:
        if target.kind == "volume_outline":
            volume = await volume_repo.get_volume_by_id(
                str(target.volume_id),
                session=session,
            )
            if str(volume.get("novel_id")) != str(novel_id):
                raise ValueError("Target volume does not belong to this novel")
            return volume
        chapter = await chapter_repo.get_chapter_by_id(
            str(target.chapter_id),
            session=session,
        )
        if str(chapter.get("novel_id")) != str(novel_id):
            raise ValueError("Target chapter does not belong to this novel")
        if target.kind == "scene":
            scenes = (chapter.get("outline") or {}).get("scenes") or []
            if target.scene_index is None or target.scene_index >= len(scenes):
                raise ValueError("Target scene does not exist")
        return chapter

    @staticmethod
    def _target_view(
        target: RevisionTarget,
        document: dict[str, Any],
    ) -> dict[str, Any]:
        common = {
            "kind": target.kind,
            "updated_at": document.get("updated_at"),
        }
        if target.kind == "volume_outline":
            return {
                **common,
                "volume_id": str(document["_id"]),
                "summary": document.get("summary", ""),
                "arc": document.get("arc", ""),
            }
        outline = deepcopy(document.get("outline") or {})
        if target.kind == "chapter_outline":
            return {
                **common,
                "chapter_id": str(document["_id"]),
                "outline": outline,
            }
        if target.kind == "scene":
            return {
                **common,
                "chapter_id": str(document["_id"]),
                "scene_index": target.scene_index,
                "scenes": deepcopy(outline.get("scenes") or []),
            }
        return {
            **common,
            "chapter_id": str(document["_id"]),
            "content": str(document.get("content") or ""),
        }

    @staticmethod
    def _patch_is_applied(
        target: RevisionTarget,
        document: dict[str, Any],
        patch: dict[str, Any],
    ) -> bool:
        if target.kind == "volume_outline":
            return all(document.get(key, "") == value for key, value in patch.items())
        if target.kind == "chapter_outline":
            outline = document.get("outline") or {}
            return all(outline.get(key, "") == value for key, value in patch.items())
        if target.kind == "scene":
            scenes = (document.get("outline") or {}).get("scenes") or []
            if target.scene_index is None or target.scene_index >= len(scenes):
                return False
            scene = scenes[target.scene_index]
            checks = {
                "scene_summary": scene.get("summary", ""),
                "scene_purpose": scene.get("purpose", ""),
            }
            return all(checks[key] == value for key, value in patch.items())
        return str(document.get("content") or "") == str(patch.get("content") or "")

    @staticmethod
    def _ensure_target_within_run_scope(
        run: dict[str, Any],
        target: RevisionTarget,
        target_document: dict[str, Any],
    ) -> None:
        snapshot = run.get("context_snapshot") or {}
        scope = snapshot.get("scope")
        if scope == "chapter":
            if str(target_document.get("_id")) != str(snapshot.get("chapter_id")):
                raise ValueError("Target chapter is outside the Agent run scope")
        elif scope == "volume":
            target_volume_id = (
                str(target_document.get("_id"))
                if target.kind == "volume_outline"
                else str(target_document.get("volume_id"))
            )
            if target_volume_id != str(snapshot.get("volume_id")):
                raise ValueError("Target asset is outside the Agent run scope")

    async def create(
        self,
        *,
        actor_id: str,
        novel_id: str,
        run_id: str,
        source_kind: RevisionSourceKind,
        source_index: int,
        target: RevisionTarget,
        patch: RevisionPatch,
    ) -> dict[str, Any]:
        run = await self.run_store.get_owned(actor_id=actor_id, run_id=run_id)
        if str(run.get("novel_id")) != str(novel_id):
            raise ValueError("Agent run belongs to another novel")
        if run.get("status") != "completed":
            raise RevisionProposalConflict("Agent run is not completed")
        snapshot = run.get("context_snapshot") or {}
        captured_revision = int(snapshot.get("narrative_revision") or 0)
        if await narrative_revision_store.current(novel_id) != captured_revision:
            raise StaleRevisionProposal(
                "小说内容已在 Agent 运行后发生变化，请重新运行 Agent"
            )

        source = self._source_from_run(run, source_kind, source_index)
        self._validate_source_target(source_kind, target.kind)
        prepared_patch = self._validate_patch(target, patch)
        target_document = await self._load_target(novel_id, target)
        self._ensure_target_within_run_scope(run, target, target_document)
        target_revision = _digest(self._target_view(target, target_document))
        now = get_utc_now()
        proposal_id = ObjectId()
        await self.collection.insert_one(
            {
                "_id": proposal_id,
                "novel_id": to_object_id(novel_id),
                "actor_id": to_object_id(actor_id),
                "run_id": to_object_id(run_id),
                "source_kind": source_kind,
                "source_index": source_index,
                "source": source,
                "agent": {
                    "agent_id": run.get("agent_id"),
                    "agent_version": run.get("agent_version"),
                    "provider_alias": run.get("provider_alias"),
                },
                "context_snapshot": deepcopy(snapshot),
                "target": target.model_dump(exclude_none=True),
                "target_revision": target_revision,
                "patch": prepared_patch,
                "status": "proposed",
                "version": 1,
                "acceptance": None,
                "rejection": None,
                "stale_reason": None,
                "created_at": now,
                "updated_at": now,
                "is_deleted": False,
                "deleted_at": None,
            }
        )
        return _proposal_view(
            await self.collection.find_one({"_id": proposal_id})
        )

    async def get_owned(
        self,
        *,
        actor_id: str,
        proposal_id: str,
    ) -> dict[str, Any]:
        document = await self.collection.find_one(
            {
                "_id": to_object_id(proposal_id),
                "actor_id": to_object_id(actor_id),
                "is_deleted": False,
            }
        )
        if document is None:
            raise NotFoundError(
                f"Agent revision proposal '{proposal_id}' was not found"
            )
        return document

    async def list_owned(
        self,
        *,
        actor_id: str,
        novel_id: str,
        status: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        query: dict[str, Any] = {
            "actor_id": to_object_id(actor_id),
            "novel_id": to_object_id(novel_id),
            "is_deleted": False,
        }
        if status:
            query["status"] = status
        cursor = self.collection.find(query).sort("created_at", -1).limit(
            max(1, min(int(limit), 100))
        )
        return [_proposal_view(item) for item in await cursor.to_list(length=None)]

    async def _mark_stale(
        self,
        proposal_id: str,
        reason: str,
    ) -> None:
        await self.collection.update_one(
            {
                "_id": to_object_id(proposal_id),
                "status": {"$in": ["proposed", "applying"]},
            },
            {
                "$set": {
                    "status": "stale",
                    "stale_reason": reason,
                    "updated_at": get_utc_now(),
                },
                "$inc": {"version": 1},
            },
        )

    @staticmethod
    async def _execute_apply(session: Any, mutation: Any) -> dict[str, Any]:
        command = mutation.journal["command"]["payload"]
        proposal_id = str(command["proposal_id"])
        novel_id = str(mutation.journal["novel_id"])
        target = RevisionTarget.model_validate(command["target"])
        patch = dict(command["patch"])
        proposals = get_database()[collections.AGENT_REVISION_PROPOSALS]
        proposal = await proposals.find_one(
            {"_id": to_object_id(proposal_id)},
            session=session,
        )
        if proposal is None:
            raise NotFoundError(
                f"Agent revision proposal '{proposal_id}' was not found"
            )
        if proposal.get("status") == "applied":
            return {
                "proposal_id": proposal_id,
                "status": "applied",
                "target": deepcopy(proposal.get("target") or {}),
            }
        if proposal.get("status") != "applying":
            raise RevisionProposalConflict(
                f"Proposal cannot be applied from status {proposal.get('status')}"
            )

        expected_narrative_revision = int(command["narrative_revision"])
        if not mutation.was_received("target"):
            current_revision = await narrative_revision_store.current(
                novel_id,
                session=session,
            )
            if current_revision != expected_narrative_revision + 1:
                raise StaleRevisionProposal(
                    "小说 revision 已变化，修订提案不能继续应用"
                )
            target_document = await AgentRevisionProposalService._load_target(
                novel_id,
                target,
                session=session,
            )
            current_target_revision = _digest(
                AgentRevisionProposalService._target_view(
                    target,
                    target_document,
                )
            )
            if current_target_revision != command["target_revision"]:
                if not AgentRevisionProposalService._patch_is_applied(
                    target,
                    target_document,
                    patch,
                ):
                    raise StaleRevisionProposal(
                        "目标资产 revision 已变化，修订提案不能继续应用"
                    )
                await mutation.receipt(
                    "target",
                    {
                        "revision_before": command["target_revision"],
                        "revision_after": current_target_revision,
                    },
                )
            else:
                if target.kind == "volume_outline":
                    await volume_repo.update_volume_info(
                        str(target.volume_id),
                        patch,
                        session=session,
                    )
                else:
                    chapter_id = str(target.chapter_id)
                    if target.kind == "chapter_outline":
                        outline = deepcopy(target_document.get("outline") or {})
                        outline.update(patch)
                        await chapter_repo.update_chapter(
                            chapter_id,
                            {"outline": outline},
                            session=session,
                        )
                    elif target.kind == "scene":
                        outline = deepcopy(target_document.get("outline") or {})
                        scenes = list(deepcopy(outline.get("scenes") or []))
                        scene = dict(scenes[int(target.scene_index)])
                        if "scene_summary" in patch:
                            scene["summary"] = patch["scene_summary"]
                        if "scene_purpose" in patch:
                            scene["purpose"] = patch["scene_purpose"]
                        scenes[int(target.scene_index)] = scene
                        outline["scenes"] = scenes
                        await chapter_repo.update_chapter(
                            chapter_id,
                            {"outline": outline},
                            session=session,
                        )
                    else:
                        content = str(patch["content"])
                        await chapter_repo.update_chapter(
                            chapter_id,
                            {
                                "content": content,
                                "word_count": count_chapter_words(content),
                            },
                            session=session,
                        )

                changed = await AgentRevisionProposalService._load_target(
                    novel_id,
                    target,
                    session=session,
                )
                await mutation.receipt(
                    "target",
                    {
                        "revision_before": command["target_revision"],
                        "revision_after": _digest(
                            AgentRevisionProposalService._target_view(
                                target,
                                changed,
                            )
                        ),
                    },
                )

        if (
            target.kind == "chapter_prose"
            and not mutation.was_received("derived_stats")
        ):
            await derived_stats.refresh(novel_id, session=session)
            await mutation.receipt(
                "derived_stats",
                {"refreshed": True},
            )

        receipts = mutation.journal.get("receipts") or {}
        target_receipt = deepcopy(receipts.get("target") or {})
        now = get_utc_now()
        applied = await proposals.find_one_and_update(
            {
                "_id": to_object_id(proposal_id),
                "status": "applying",
            },
            {
                "$set": {
                    "status": "applied",
                    "acceptance": {
                        "actor_id": to_object_id(command["actor_id"]),
                        "accepted_at": now,
                        "narrative_revision_before": expected_narrative_revision,
                        "narrative_revision_after": expected_narrative_revision + 1,
                        **target_receipt,
                    },
                    "updated_at": now,
                },
                "$inc": {"version": 1},
            },
            return_document=ReturnDocument.AFTER,
            session=session,
        )
        if applied is None:
            applied = await proposals.find_one(
                {"_id": to_object_id(proposal_id)},
                session=session,
            )
        return {
            "proposal_id": proposal_id,
            "status": "applied",
            "target": deepcopy(applied.get("target") or {}),
        }

    async def apply(
        self,
        *,
        actor_id: str,
        proposal_id: str,
        expected_version: int,
    ) -> dict[str, Any]:
        proposal = await self.get_owned(
            actor_id=actor_id,
            proposal_id=proposal_id,
        )
        if proposal.get("status") == "applied":
            return _proposal_view(proposal)

        if proposal.get("status") == "proposed":
            if int(proposal.get("version") or 1) != expected_version:
                raise RevisionProposalConflict(
                    "修订提案版本已变化，请刷新后重试"
                )
            novel_id = str(proposal["novel_id"])
            snapshot = proposal.get("context_snapshot") or {}
            captured_revision = int(snapshot.get("narrative_revision") or 0)
            if await narrative_revision_store.current(novel_id) != captured_revision:
                reason = "小说内容已在提案创建后发生变化"
                await self._mark_stale(proposal_id, reason)
                raise StaleRevisionProposal(reason)
            target = RevisionTarget.model_validate(proposal["target"])
            current_target = await self._load_target(novel_id, target)
            if _digest(self._target_view(target, current_target)) != proposal.get(
                "target_revision"
            ):
                reason = "目标资产已在提案创建后发生变化"
                await self._mark_stale(proposal_id, reason)
                raise StaleRevisionProposal(reason)

            claimed = await self.collection.find_one_and_update(
                {
                    "_id": proposal["_id"],
                    "actor_id": to_object_id(actor_id),
                    "status": "proposed",
                    "version": expected_version,
                },
                {
                    "$set": {
                        "status": "applying",
                        "applying_actor_id": to_object_id(actor_id),
                        "applying_at": get_utc_now(),
                        "updated_at": get_utc_now(),
                    },
                    "$inc": {"version": 1},
                },
                return_document=ReturnDocument.AFTER,
            )
            if claimed is None:
                raise RevisionProposalConflict(
                    "修订提案状态已变化，请刷新后重试"
                )
            proposal = claimed
        elif not (
            proposal.get("status") == "applying"
            and int(proposal.get("version") or 0) == expected_version + 1
            and str(proposal.get("applying_actor_id")) == str(actor_id)
        ):
            raise RevisionProposalConflict(
                f"修订提案当前状态不能接受: {proposal.get('status')}"
            )

        command = MutationCommand(
            novel_id=str(proposal["novel_id"]),
            idempotency_key=f"apply-agent-revision:{proposal_id}",
            operation="apply_agent_revision_proposal",
            version=1,
            payload={
                "proposal_id": proposal_id,
                "actor_id": actor_id,
                "target": deepcopy(proposal["target"]),
                "patch": deepcopy(proposal["patch"]),
                "target_revision": proposal["target_revision"],
                "narrative_revision": int(
                    (proposal.get("context_snapshot") or {}).get(
                        "narrative_revision"
                    )
                    or 0
                ),
            },
            before_image={
                "proposal": {
                    "status": "proposed",
                    "version": expected_version,
                }
            },
        )
        try:
            await commit_mutation(command, self._execute_apply)
        except StaleRevisionProposal as exc:
            await self._mark_stale(proposal_id, str(exc))
            raise
        return _proposal_view(
            await self.get_owned(
                actor_id=actor_id,
                proposal_id=proposal_id,
            )
        )

    async def reject(
        self,
        *,
        actor_id: str,
        proposal_id: str,
        expected_version: int,
        reason: str = "",
    ) -> dict[str, Any]:
        now = get_utc_now()
        document = await self.collection.find_one_and_update(
            {
                "_id": to_object_id(proposal_id),
                "actor_id": to_object_id(actor_id),
                "status": "proposed",
                "version": expected_version,
                "is_deleted": False,
            },
            {
                "$set": {
                    "status": "rejected",
                    "rejection": {
                        "actor_id": to_object_id(actor_id),
                        "reason": reason.strip()[:1000],
                        "rejected_at": now,
                    },
                    "updated_at": now,
                },
                "$inc": {"version": 1},
            },
            return_document=ReturnDocument.AFTER,
        )
        if document is None:
            raise RevisionProposalConflict(
                "修订提案状态或版本已变化，请刷新后重试"
            )
        return _proposal_view(document)


agent_revision_proposal_service = AgentRevisionProposalService()
