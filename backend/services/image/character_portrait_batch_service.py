"""Durable, explicitly confirmed batches of single character portraits."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import time
from typing import Any, Callable, Literal, Protocol

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from backend.db.repositories.image_batch_repository import image_batch_repo
from backend.db.repositories.image_job_repository import image_job_repo
from backend.db.utils import get_utc_now, to_object_id
from backend.services.image.character_portrait_service import (
    CharacterPortraitService,
    character_portrait_service,
)
from backend.services.image.single_image_job_service import (
    ConfiguredImageProviderResolver,
    ImageProviderSnapshot,
    PortraitConfigurationError,
    PortraitJobProjection,
)
from backend.services.llm.agent_orchestrator import IllustrationPromptResult
from backend.services.novel.reference_card_service import ReferenceCardService


MAX_PORTRAIT_BATCH_ITEMS = 64
PORTRAIT_BATCH_PLAN_REVISION = "character-portrait-batch-plan-v1"

PortraitBatchStatus = Literal[
    "running",
    "cancelling",
    "completed",
    "completed_with_failures",
    "cancelled",
]
PortraitBatchItemStatus = Literal[
    "pending",
    "starting",
    "running",
    "succeeded",
    "failed",
    "cancelled",
]


class PortraitBatchNotFoundError(LookupError):
    pass


class PortraitBatchConflictError(RuntimeError):
    pass


class PortraitBatchPlanStaleError(RuntimeError):
    pass


class PortraitBatchItemInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    card_id: str
    prompt: IllustrationPromptResult
    seed: int | None = None

    @field_validator("card_id", mode="before")
    @classmethod
    def validate_card_id(cls, value: Any) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("card_id is required")
        return str(to_object_id(normalized))

    @field_validator("seed", mode="before")
    @classmethod
    def validate_seed(cls, value: Any) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool):
            raise ValueError("seed must be an integer from 0 through 2^64-1")
        if isinstance(value, int):
            parsed = value
        elif (
            isinstance(value, str)
            and value
            and value.isascii()
            and value.isdecimal()
            and (value == "0" or not value.startswith("0"))
        ):
            parsed = int(value)
        else:
            raise ValueError("seed must be an integer from 0 through 2^64-1")
        if not 0 <= parsed <= (2**64 - 1):
            raise ValueError("seed must be an integer from 0 through 2^64-1")
        return parsed

    @model_validator(mode="after")
    def require_appearance(self) -> "PortraitBatchItemInput":
        if not self.prompt.appearance.strip():
            raise ValueError(
                "appearance must not be empty for a character portrait"
            )
        return self


class PortraitBatchPlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    items: tuple[PortraitBatchItemInput, ...] = Field(
        min_length=1,
        max_length=MAX_PORTRAIT_BATCH_ITEMS,
    )
    provider_alias: str | None = Field(default=None, max_length=200)

    @field_validator("provider_alias", mode="before")
    @classmethod
    def normalize_provider_alias(cls, value: Any) -> str | None:
        normalized = str(value or "").strip()
        return normalized or None

    @model_validator(mode="after")
    def reject_duplicate_cards(self) -> "PortraitBatchPlanRequest":
        card_ids = [item.card_id for item in self.items]
        if len(set(card_ids)) != len(card_ids):
            raise ValueError("A portrait batch cannot contain duplicate cards")
        return self


class PortraitBatchPlanProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    plan_revision: str = PORTRAIT_BATCH_PLAN_REVISION
    plan_digest: str
    provider_alias: str
    provider_model: str
    workflow_revision: str
    total_images: int
    unit_estimated_seconds: int
    estimated_seconds: int
    queue_position: int
    max_provider_requests: int
    max_concurrency: Literal[1] = 1
    estimate_source: Literal["history", "provider_timeout"]
    warnings: tuple[str, ...] = ()


class PortraitBatchPlanConfirmation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    plan_revision: Literal[PORTRAIT_BATCH_PLAN_REVISION]
    plan_digest: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    provider_alias: str
    provider_model: str
    workflow_revision: str
    total_images: int = Field(ge=1, le=MAX_PORTRAIT_BATCH_ITEMS)
    unit_estimated_seconds: int = Field(ge=1)
    estimated_seconds: int = Field(ge=1)
    queue_position: int = Field(ge=0)
    max_provider_requests: int = Field(ge=1, le=MAX_PORTRAIT_BATCH_ITEMS)
    max_concurrency: Literal[1] = 1
    estimate_source: Literal["history", "provider_timeout"]


class PortraitBatchStartRequest(PortraitBatchPlanRequest):
    expected_plan: PortraitBatchPlanConfirmation
    confirm: bool

    @model_validator(mode="after")
    def require_confirmation(self) -> "PortraitBatchStartRequest":
        if self.confirm is not True:
            raise ValueError("The frozen portrait batch plan must be confirmed")
        return self


class PortraitBatchFailure(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    message: str
    action: str
    retryable: bool = False


class PortraitBatchItemProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    card_id: str
    card_name: str
    status: PortraitBatchItemStatus
    job_id: str | None = None
    job_status: str | None = None
    queue_position: int | None = None
    submit_count: int = 0
    completed_images: int = 0
    failure: PortraitBatchFailure | None = None


class PortraitBatchProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    batch_id: str
    plan_digest: str
    status: PortraitBatchStatus
    terminal: bool
    cancel_requested: bool
    provider_alias: str
    provider_model: str
    workflow_revision: str
    total_images: int
    unit_estimated_seconds: int
    estimated_seconds: int
    elapsed_seconds: int
    max_provider_requests: int
    submitted_requests: int
    request_upper_bound_exceeded: bool
    max_concurrency: Literal[1] = 1
    completed_images: int
    succeeded_items: int
    failed_items: int
    cancelled_items: int
    pending_items: int
    current_index: int | None
    queue_position: int | None
    items: tuple[PortraitBatchItemProjection, ...]
    failure: PortraitBatchFailure | None = None
    warnings: tuple[str, ...] = ()
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ImageBatchRepositoryProtocol(Protocol):
    async def create_batch(self, document: dict[str, Any]) -> dict[str, Any]: ...

    async def get_owned_batch(
        self,
        *,
        owner_id: str,
        novel_id: str,
        batch_id: str,
    ) -> dict[str, Any] | None: ...

    async def find_active_owned_batch(
        self,
        *,
        owner_id: str,
        novel_id: str,
    ) -> dict[str, Any] | None: ...

    async def compare_and_update_owned_batch(
        self,
        *,
        owner_id: str,
        novel_id: str,
        batch_id: str,
        expected_revision: int,
        fields: dict[str, Any],
    ) -> dict[str, Any] | None: ...


class PortraitImageJobRepositoryProtocol(Protocol):
    async def list_busy_portrait_subject_ids(
        self,
        *,
        owner_id: str,
        novel_id: str,
        card_ids: list[str] | tuple[str, ...],
    ) -> set[str]: ...

    async def find_owned_portrait_batch_job(
        self,
        *,
        owner_id: str,
        novel_id: str,
        batch_id: str,
        card_id: str,
    ) -> dict[str, Any] | None: ...

    async def median_completed_seconds(
        self,
        *,
        provider_alias: str,
        usage: str = "character_portrait",
    ) -> int | None: ...


class PortraitProviderInspectorProtocol(Protocol):
    async def inspect(
        self,
        *,
        usage: str,
        provider_alias: str | None,
    ) -> ImageProviderSnapshot: ...


class PortraitExecutorProtocol(Protocol):
    async def start(
        self,
        *,
        owner_id: str,
        novel_id: str,
        card_id: str,
        prompt: IllustrationPromptResult,
        seed: int | None = None,
        provider_alias: str | None = None,
        confirm_anchor_reset: bool = False,
        portrait_batch_id: str | None = None,
    ) -> PortraitJobProjection: ...

    async def poll(
        self,
        *,
        owner_id: str,
        novel_id: str,
        card_id: str,
        job_id: str,
    ) -> PortraitJobProjection: ...

    async def cancel(
        self,
        *,
        owner_id: str,
        novel_id: str,
        card_id: str,
        job_id: str,
    ) -> PortraitJobProjection: ...


class ReferenceCardCatalogProtocol(Protocol):
    async def list(
        self,
        novel_id: str,
        card_type: str,
        *,
        deleted_only: bool = False,
    ) -> list[dict[str, Any]]: ...


@dataclass(frozen=True, slots=True)
class _PreparedPlan:
    projection: PortraitBatchPlanProjection
    card_names: dict[str, str]


def _canonical_id(value: Any, *, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{field_name} is required")
    return str(to_object_id(normalized))


def _batch_id(document: dict[str, Any]) -> str:
    return str(document.get("_id") or document.get("batch_id") or "")


def _failure_from_job(job: PortraitJobProjection) -> PortraitBatchFailure | None:
    if job.failure is None:
        return None
    code = getattr(job.failure.code, "value", job.failure.code)
    return PortraitBatchFailure(
        code=str(code),
        message=job.failure.message,
        action=job.failure.action,
        retryable=job.failure.retryable,
    )


def _exception_failure(error: Exception) -> PortraitBatchFailure:
    return PortraitBatchFailure(
        code="batch_item_start_rejected",
        message=str(error) or "角色立绘任务未能启动",
        action="检查角色卡、外观锚点与图像后端状态后，重新准备批次",
    )


def _cancelled_before_submit_failure() -> dict[str, Any]:
    return PortraitBatchFailure(
        code="batch_cancelled_before_submit",
        message="整批取消时该角色尚未提交到图像后端",
        action="如仍需该立绘，请重新准备一个新批次",
    ).model_dump(mode="json")


def _item_status_from_job(job: PortraitJobProjection) -> PortraitBatchItemStatus:
    if not job.terminal:
        return "running"
    if job.status == "succeeded":
        return "succeeded"
    if job.status == "cancelled":
        return "cancelled"
    return "failed"


def _counts(items: list[dict[str, Any]]) -> dict[str, int]:
    succeeded = sum(item.get("status") == "succeeded" for item in items)
    failed = sum(item.get("status") == "failed" for item in items)
    cancelled = sum(item.get("status") == "cancelled" for item in items)
    submitted = sum(max(0, int(item.get("submit_count") or 0)) for item in items)
    completed_images = sum(
        max(0, int(item.get("completed_images") or 0)) for item in items
    )
    return {
        "succeeded_items": succeeded,
        "failed_items": failed,
        "cancelled_items": cancelled,
        "pending_items": len(items) - succeeded - failed - cancelled,
        "submitted_requests": submitted,
        "completed_images": completed_images,
    }


def _projection(
    document: dict[str, Any],
    *,
    now_epoch: float,
) -> PortraitBatchProjection:
    items = [dict(item) for item in document.get("items") or ()]
    counts = _counts(items)
    current_index = int(document.get("current_index") or 0)
    terminal = bool(document.get("is_terminal"))
    current = (
        items[current_index]
        if not terminal and 0 <= current_index < len(items)
        else None
    )
    started_at_epoch = float(document.get("started_at_epoch") or now_epoch)
    failure = document.get("failure")
    return PortraitBatchProjection(
        batch_id=_batch_id(document),
        plan_digest=str(document.get("plan_digest") or ""),
        status=str(document.get("status") or "running"),
        terminal=terminal,
        cancel_requested=bool(document.get("cancel_requested")),
        provider_alias=str(document.get("provider_alias") or ""),
        provider_model=str(document.get("provider_model") or ""),
        workflow_revision=str(document.get("workflow_revision") or ""),
        total_images=len(items),
        unit_estimated_seconds=max(
            1,
            int(document.get("unit_estimated_seconds") or 1),
        ),
        estimated_seconds=max(1, int(document.get("estimated_seconds") or 1)),
        elapsed_seconds=max(0, int(now_epoch - started_at_epoch)),
        max_provider_requests=max(
            1,
            int(document.get("max_provider_requests") or len(items) or 1),
        ),
        submitted_requests=counts["submitted_requests"],
        request_upper_bound_exceeded=(
            counts["submitted_requests"]
            > int(document.get("max_provider_requests") or len(items) or 1)
        ),
        completed_images=counts["completed_images"],
        succeeded_items=counts["succeeded_items"],
        failed_items=counts["failed_items"],
        cancelled_items=counts["cancelled_items"],
        pending_items=counts["pending_items"],
        current_index=None if terminal else current_index,
        queue_position=(
            int(current["queue_position"])
            if current is not None and current.get("queue_position") is not None
            else None
        ),
        items=tuple(
            PortraitBatchItemProjection(
                card_id=str(item.get("card_id") or ""),
                card_name=str(item.get("card_name") or ""),
                status=str(item.get("status") or "pending"),
                job_id=(str(item["job_id"]) if item.get("job_id") else None),
                job_status=(
                    str(item["job_status"])
                    if item.get("job_status")
                    else None
                ),
                queue_position=(
                    int(item["queue_position"])
                    if item.get("queue_position") is not None
                    else None
                ),
                submit_count=max(0, int(item.get("submit_count") or 0)),
                completed_images=max(
                    0,
                    int(item.get("completed_images") or 0),
                ),
                failure=(
                    PortraitBatchFailure.model_validate(item["failure"])
                    if item.get("failure")
                    else None
                ),
            )
            for item in items
        ),
        failure=(
            PortraitBatchFailure.model_validate(failure) if failure else None
        ),
        warnings=tuple(str(value) for value in document.get("warnings") or ()),
        created_at=document.get("created_at"),
        updated_at=document.get("updated_at"),
    )


class CharacterPortraitBatchService:
    """Run confirmed portrait prompts sequentially with a durable envelope."""

    def __init__(
        self,
        *,
        batches: ImageBatchRepositoryProtocol,
        image_jobs: PortraitImageJobRepositoryProtocol,
        portraits: PortraitExecutorProtocol,
        provider_inspector: PortraitProviderInspectorProtocol,
        cards: ReferenceCardCatalogProtocol,
        now: Callable[[], datetime],
        now_epoch: Callable[[], float] = time.time,
    ) -> None:
        self.batches = batches
        self.image_jobs = image_jobs
        self.portraits = portraits
        self.provider_inspector = provider_inspector
        self.cards = cards
        self._now = now
        self._now_epoch = now_epoch

    async def _prepare_plan(
        self,
        *,
        owner_id: str,
        novel_id: str,
        request: PortraitBatchPlanRequest,
        reject_active_batch: bool,
    ) -> _PreparedPlan:
        owner_id = _canonical_id(owner_id, field_name="owner_id")
        novel_id = _canonical_id(novel_id, field_name="novel_id")
        if reject_active_batch:
            active_batch = await self.batches.find_active_owned_batch(
                owner_id=owner_id,
                novel_id=novel_id,
            )
            if active_batch is not None:
                raise PortraitBatchConflictError(
                    "当前小说已有未结束的批量立绘任务"
                )

        all_cards = await self.cards.list(novel_id, "character")
        cards_by_id = {str(card.get("_id")): card for card in all_cards}
        selected_cards: list[dict[str, Any]] = []
        for item in request.items:
            card = cards_by_id.get(item.card_id)
            if card is None:
                raise PortraitBatchConflictError(
                    "批次包含已删除或不属于当前小说的角色卡"
                )
            selected_cards.append(card)

        anchored_names = [
            str(card.get("name") or "未命名角色")
            for card in selected_cards
            if card.get("appearance_anchor") is not None
        ]
        if anchored_names:
            raise PortraitBatchConflictError(
                "以下角色已经有外观锚点，批量立绘不会重设锚点："
                + "、".join(anchored_names)
            )
        busy_ids = await self.image_jobs.list_busy_portrait_subject_ids(
            owner_id=owner_id,
            novel_id=novel_id,
            card_ids=tuple(item.card_id for item in request.items),
        )
        if busy_ids:
            busy_names = [
                str(card.get("name") or "未命名角色")
                for card in selected_cards
                if str(card.get("_id")) in busy_ids
            ]
            raise PortraitBatchConflictError(
                "以下角色已有未结束的立绘任务：" + "、".join(busy_names)
            )

        snapshot = await self.provider_inspector.inspect(
            usage="character_portrait",
            provider_alias=request.provider_alias,
        )
        if not snapshot.available or not snapshot.alias:
            reason = snapshot.warnings[0] if snapshot.warnings else "图像后端不可用"
            raise PortraitConfigurationError(reason)
        historical = await self.image_jobs.median_completed_seconds(
            provider_alias=snapshot.alias,
            usage="character_portrait",
        )
        estimate_source: Literal["history", "provider_timeout"] = (
            "history" if historical is not None else "provider_timeout"
        )
        unit_estimated_seconds = max(
            1,
            int(historical or snapshot.timeout_seconds),
        )
        queue_position = max(0, int(snapshot.queue_position))
        total_images = len(request.items)
        estimated_seconds = unit_estimated_seconds * (
            queue_position + total_images
        )
        max_provider_requests = total_images
        warnings = list(snapshot.warnings)
        if historical is None:
            warnings.append(
                "暂无历史耗时样本，整批预计耗时使用后端超时值作为单张保守上限"
            )
        digest_payload = {
            "plan_revision": PORTRAIT_BATCH_PLAN_REVISION,
            "owner_id": owner_id,
            "novel_id": novel_id,
            "provider_alias": snapshot.alias,
            "provider_model": snapshot.model,
            "workflow_revision": snapshot.workflow_revision,
            "total_images": total_images,
            "unit_estimated_seconds": unit_estimated_seconds,
            "estimated_seconds": estimated_seconds,
            "queue_position": queue_position,
            "max_provider_requests": max_provider_requests,
            "max_concurrency": 1,
            "estimate_source": estimate_source,
            "items": [
                {
                    "card_id": item.card_id,
                    "prompt": item.prompt.model_dump(mode="json"),
                    "seed": str(item.seed) if item.seed is not None else None,
                }
                for item in request.items
            ],
        }
        plan_digest = hashlib.sha256(
            json.dumps(
                digest_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return _PreparedPlan(
            projection=PortraitBatchPlanProjection(
                plan_digest=plan_digest,
                provider_alias=snapshot.alias,
                provider_model=snapshot.model,
                workflow_revision=snapshot.workflow_revision,
                total_images=total_images,
                unit_estimated_seconds=unit_estimated_seconds,
                estimated_seconds=estimated_seconds,
                queue_position=queue_position,
                max_provider_requests=max_provider_requests,
                estimate_source=estimate_source,
                warnings=tuple(dict.fromkeys(warnings)),
            ),
            card_names={
                str(card.get("_id")): str(card.get("name") or "未命名角色")
                for card in selected_cards
            },
        )

    async def plan(
        self,
        *,
        owner_id: str,
        novel_id: str,
        request: PortraitBatchPlanRequest,
    ) -> PortraitBatchPlanProjection:
        prepared = await self._prepare_plan(
            owner_id=owner_id,
            novel_id=novel_id,
            request=request,
            reject_active_batch=True,
        )
        return prepared.projection

    @staticmethod
    def _confirmation_matches(
        plan: PortraitBatchPlanProjection,
        expected: PortraitBatchPlanConfirmation,
    ) -> bool:
        comparable = {
            "plan_revision": plan.plan_revision,
            "plan_digest": plan.plan_digest,
            "provider_alias": plan.provider_alias,
            "provider_model": plan.provider_model,
            "workflow_revision": plan.workflow_revision,
            "total_images": plan.total_images,
            "unit_estimated_seconds": plan.unit_estimated_seconds,
            "estimated_seconds": plan.estimated_seconds,
            "queue_position": plan.queue_position,
            "max_provider_requests": plan.max_provider_requests,
            "max_concurrency": plan.max_concurrency,
            "estimate_source": plan.estimate_source,
        }
        return comparable == expected.model_dump(mode="json")

    async def start(
        self,
        *,
        owner_id: str,
        novel_id: str,
        request: PortraitBatchStartRequest,
    ) -> PortraitBatchProjection:
        owner_id = _canonical_id(owner_id, field_name="owner_id")
        novel_id = _canonical_id(novel_id, field_name="novel_id")
        active = await self.batches.find_active_owned_batch(
            owner_id=owner_id,
            novel_id=novel_id,
        )
        if active is not None:
            if str(active.get("plan_digest") or "") == request.expected_plan.plan_digest:
                return _projection(active, now_epoch=self._now_epoch())
            raise PortraitBatchConflictError(
                "当前小说已有另一个未结束的批量立绘任务"
            )

        prepared = await self._prepare_plan(
            owner_id=owner_id,
            novel_id=novel_id,
            request=PortraitBatchPlanRequest(
                items=request.items,
                provider_alias=request.provider_alias,
            ),
            reject_active_batch=False,
        )
        if not self._confirmation_matches(
            prepared.projection,
            request.expected_plan,
        ):
            raise PortraitBatchPlanStaleError(
                "图像队列、后端版本或批次内容已经变化，请重新核对总量与耗时"
            )

        document = {
            "owner_id": owner_id,
            "novel_id": novel_id,
            "kind": "character_portrait",
            "plan_revision": prepared.projection.plan_revision,
            "plan_digest": prepared.projection.plan_digest,
            "provider_alias": prepared.projection.provider_alias,
            "provider_model": prepared.projection.provider_model,
            "workflow_revision": prepared.projection.workflow_revision,
            "unit_estimated_seconds": prepared.projection.unit_estimated_seconds,
            "estimated_seconds": prepared.projection.estimated_seconds,
            "max_provider_requests": prepared.projection.max_provider_requests,
            "max_concurrency": 1,
            "status": "running",
            "is_terminal": False,
            "cancel_requested": False,
            "current_index": 0,
            "revision": 0,
            "started_at_epoch": self._now_epoch(),
            "finished_at_epoch": None,
            "failure": None,
            "warnings": list(prepared.projection.warnings),
            "items": [
                {
                    "card_id": item.card_id,
                    "card_name": prepared.card_names[item.card_id],
                    "prompt": item.prompt.model_dump(mode="json"),
                    "seed": str(item.seed) if item.seed is not None else None,
                    "status": "pending",
                    "job_id": None,
                    "job_status": None,
                    "queue_position": None,
                    "submit_count": 0,
                    "completed_images": 0,
                    "failure": None,
                }
                for item in request.items
            ],
        }
        created = await self.batches.create_batch(document)
        if created.get("_was_created") is False:
            if str(created.get("plan_digest") or "") != prepared.projection.plan_digest:
                raise PortraitBatchConflictError(
                    "当前小说已有另一个未结束的批量立绘任务"
                )
            return _projection(created, now_epoch=self._now_epoch())
        return await self.advance(
            owner_id=owner_id,
            novel_id=novel_id,
            batch_id=_batch_id(created),
        )

    async def get_current(
        self,
        *,
        owner_id: str,
        novel_id: str,
    ) -> PortraitBatchProjection | None:
        active = await self.batches.find_active_owned_batch(
            owner_id=_canonical_id(owner_id, field_name="owner_id"),
            novel_id=_canonical_id(novel_id, field_name="novel_id"),
        )
        return (
            _projection(active, now_epoch=self._now_epoch())
            if active is not None
            else None
        )

    async def get(
        self,
        *,
        owner_id: str,
        novel_id: str,
        batch_id: str,
    ) -> PortraitBatchProjection:
        document = await self.batches.get_owned_batch(
            owner_id=_canonical_id(owner_id, field_name="owner_id"),
            novel_id=_canonical_id(novel_id, field_name="novel_id"),
            batch_id=_canonical_id(batch_id, field_name="batch_id"),
        )
        if document is None:
            raise PortraitBatchNotFoundError("批量立绘任务不存在")
        return _projection(document, now_epoch=self._now_epoch())

    def _terminal_fields(
        self,
        *,
        items: list[dict[str, Any]],
        cancelled: bool,
    ) -> dict[str, Any]:
        counts = _counts(items)
        status: PortraitBatchStatus
        if cancelled:
            status = "cancelled"
        elif counts["failed_items"] or counts["cancelled_items"]:
            status = "completed_with_failures"
        else:
            status = "completed"
        return {
            "items": items,
            "current_index": len(items),
            "status": status,
            "is_terminal": True,
            "finished_at_epoch": self._now_epoch(),
        }

    async def _save(
        self,
        *,
        batch: dict[str, Any],
        owner_id: str,
        novel_id: str,
        fields: dict[str, Any],
    ) -> dict[str, Any]:
        updated = await self.batches.compare_and_update_owned_batch(
            owner_id=owner_id,
            novel_id=novel_id,
            batch_id=_batch_id(batch),
            expected_revision=int(batch.get("revision") or 0),
            fields=fields,
        )
        if updated is not None:
            return updated
        current = await self.batches.get_owned_batch(
            owner_id=owner_id,
            novel_id=novel_id,
            batch_id=_batch_id(batch),
        )
        if current is None:
            raise PortraitBatchNotFoundError("批量立绘任务不存在")
        return current

    async def _record_job(
        self,
        *,
        batch: dict[str, Any],
        owner_id: str,
        novel_id: str,
        index: int,
        job: PortraitJobProjection,
    ) -> PortraitBatchProjection:
        items = [dict(item) for item in batch.get("items") or ()]
        item = dict(items[index])
        job_failure = _failure_from_job(job)
        item.update(
            {
                "status": _item_status_from_job(job),
                "job_id": job.job_id,
                "job_status": job.status,
                "queue_position": job.queue_position,
                "submit_count": max(0, int(job.submit_count)),
                "completed_images": max(0, int(job.completed_images)),
                "failure": (
                    job_failure.model_dump(mode="json")
                    if job_failure is not None
                    else None
                ),
            }
        )
        items[index] = item
        fields: dict[str, Any] = {
            "items": items,
            "status": "cancelling" if batch.get("cancel_requested") else "running",
        }
        if job.terminal:
            next_index = index + 1
            fields["current_index"] = next_index
            if next_index >= len(items):
                fields.update(
                    self._terminal_fields(
                        items=items,
                        cancelled=bool(batch.get("cancel_requested")),
                    )
                )
        counts = _counts(items)
        if counts["submitted_requests"] > int(batch["max_provider_requests"]):
            fields.update(
                self._upper_bound_failure_fields(items=items, start=index + 1)
            )
        updated = await self._save(
            batch=batch,
            owner_id=owner_id,
            novel_id=novel_id,
            fields=fields,
        )
        return _projection(updated, now_epoch=self._now_epoch())

    def _upper_bound_failure_fields(
        self,
        *,
        items: list[dict[str, Any]],
        start: int,
    ) -> dict[str, Any]:
        failure = PortraitBatchFailure(
            code="provider_request_upper_bound_reached",
            message="批次已达到冻结的生成请求上限",
            action="检查单图任务审计；未提交项目必须留在当前批次外",
        ).model_dump(mode="json")
        for index in range(max(0, start), len(items)):
            if items[index].get("status") in {"pending", "starting"}:
                items[index] = {
                    **items[index],
                    "status": "failed",
                    "failure": failure,
                }
        return {
            **self._terminal_fields(items=items, cancelled=False),
            "failure": failure,
        }

    async def _record_item_failure(
        self,
        *,
        batch: dict[str, Any],
        owner_id: str,
        novel_id: str,
        index: int,
        failure: PortraitBatchFailure,
    ) -> PortraitBatchProjection:
        items = [dict(item) for item in batch.get("items") or ()]
        items[index] = {
            **items[index],
            "status": "failed",
            "failure": failure.model_dump(mode="json"),
        }
        next_index = index + 1
        fields: dict[str, Any] = {
            "items": items,
            "current_index": next_index,
        }
        if next_index >= len(items):
            fields.update(
                self._terminal_fields(
                    items=items,
                    cancelled=bool(batch.get("cancel_requested")),
                )
            )
        updated = await self._save(
            batch=batch,
            owner_id=owner_id,
            novel_id=novel_id,
            fields=fields,
        )
        return _projection(updated, now_epoch=self._now_epoch())

    async def _advance_item(
        self,
        *,
        batch: dict[str, Any],
        owner_id: str,
        novel_id: str,
        index: int,
    ) -> PortraitBatchProjection:
        items = [dict(item) for item in batch.get("items") or ()]
        item = items[index]
        recovered = await self.image_jobs.find_owned_portrait_batch_job(
            owner_id=owner_id,
            novel_id=novel_id,
            batch_id=_batch_id(batch),
            card_id=str(item["card_id"]),
        )
        try:
            if item.get("job_id"):
                job = await self.portraits.poll(
                    owner_id=owner_id,
                    novel_id=novel_id,
                    card_id=str(item["card_id"]),
                    job_id=str(item["job_id"]),
                )
            elif recovered is not None:
                job = await self.portraits.poll(
                    owner_id=owner_id,
                    novel_id=novel_id,
                    card_id=str(item["card_id"]),
                    job_id=str(recovered["_id"]),
                )
            else:
                counts = _counts(items)
                if counts["submitted_requests"] >= int(
                    batch["max_provider_requests"]
                ):
                    fields = self._upper_bound_failure_fields(
                        items=items,
                        start=index,
                    )
                    updated = await self._save(
                        batch=batch,
                        owner_id=owner_id,
                        novel_id=novel_id,
                        fields=fields,
                    )
                    return _projection(updated, now_epoch=self._now_epoch())
                raw_seed = item.get("seed")
                job = await self.portraits.start(
                    owner_id=owner_id,
                    novel_id=novel_id,
                    card_id=str(item["card_id"]),
                    prompt=IllustrationPromptResult.model_validate(item["prompt"]),
                    seed=int(raw_seed) if raw_seed is not None else None,
                    provider_alias=str(batch["provider_alias"]),
                    confirm_anchor_reset=False,
                    portrait_batch_id=_batch_id(batch),
                )
        except Exception as error:
            return await self._record_item_failure(
                batch=batch,
                owner_id=owner_id,
                novel_id=novel_id,
                index=index,
                failure=_exception_failure(error),
            )
        return await self._record_job(
            batch=batch,
            owner_id=owner_id,
            novel_id=novel_id,
            index=index,
            job=job,
        )

    async def advance(
        self,
        *,
        owner_id: str,
        novel_id: str,
        batch_id: str,
    ) -> PortraitBatchProjection:
        owner_id = _canonical_id(owner_id, field_name="owner_id")
        novel_id = _canonical_id(novel_id, field_name="novel_id")
        batch_id = _canonical_id(batch_id, field_name="batch_id")
        for _ in range(4):
            batch = await self.batches.get_owned_batch(
                owner_id=owner_id,
                novel_id=novel_id,
                batch_id=batch_id,
            )
            if batch is None:
                raise PortraitBatchNotFoundError("批量立绘任务不存在")
            if batch.get("is_terminal"):
                return _projection(batch, now_epoch=self._now_epoch())
            if batch.get("cancel_requested"):
                return await self._advance_cancel(
                    batch=batch,
                    owner_id=owner_id,
                    novel_id=novel_id,
                )
            items = [dict(item) for item in batch.get("items") or ()]
            index = int(batch.get("current_index") or 0)
            if index >= len(items):
                updated = await self._save(
                    batch=batch,
                    owner_id=owner_id,
                    novel_id=novel_id,
                    fields=self._terminal_fields(items=items, cancelled=False),
                )
                return _projection(updated, now_epoch=self._now_epoch())
            status = str(items[index].get("status") or "pending")
            if status in {"succeeded", "failed", "cancelled"}:
                updated = await self._save(
                    batch=batch,
                    owner_id=owner_id,
                    novel_id=novel_id,
                    fields={"current_index": index + 1},
                )
                if int(updated.get("revision") or 0) == int(
                    batch.get("revision") or 0
                ):
                    continue
                batch = updated
                continue
            if status == "pending":
                items[index] = {**items[index], "status": "starting"}
                claimed = await self.batches.compare_and_update_owned_batch(
                    owner_id=owner_id,
                    novel_id=novel_id,
                    batch_id=batch_id,
                    expected_revision=int(batch.get("revision") or 0),
                    fields={"items": items},
                )
                if claimed is None:
                    continue
                batch = claimed
            return await self._advance_item(
                batch=batch,
                owner_id=owner_id,
                novel_id=novel_id,
                index=index,
            )
        return await self.get(
            owner_id=owner_id,
            novel_id=novel_id,
            batch_id=batch_id,
        )

    @staticmethod
    def _cancel_remaining(
        items: list[dict[str, Any]],
        *,
        start: int,
    ) -> list[dict[str, Any]]:
        failure = _cancelled_before_submit_failure()
        for index in range(max(0, start), len(items)):
            if items[index].get("status") in {"pending", "starting"}:
                items[index] = {
                    **items[index],
                    "status": "cancelled",
                    "failure": failure,
                }
        return items

    async def _advance_cancel(
        self,
        *,
        batch: dict[str, Any],
        owner_id: str,
        novel_id: str,
    ) -> PortraitBatchProjection:
        items = [dict(item) for item in batch.get("items") or ()]
        index = int(batch.get("current_index") or 0)
        if index >= len(items):
            updated = await self._save(
                batch=batch,
                owner_id=owner_id,
                novel_id=novel_id,
                fields=self._terminal_fields(items=items, cancelled=True),
            )
            return _projection(updated, now_epoch=self._now_epoch())
        item = items[index]
        if item.get("status") in {"succeeded", "failed", "cancelled"}:
            items = self._cancel_remaining(items, start=index + 1)
            updated = await self._save(
                batch=batch,
                owner_id=owner_id,
                novel_id=novel_id,
                fields=self._terminal_fields(items=items, cancelled=True),
            )
            return _projection(updated, now_epoch=self._now_epoch())
        recovered = await self.image_jobs.find_owned_portrait_batch_job(
            owner_id=owner_id,
            novel_id=novel_id,
            batch_id=_batch_id(batch),
            card_id=str(item["card_id"]),
        )
        job_id = str(item.get("job_id") or "")
        if not job_id and recovered is not None:
            job_id = str(recovered["_id"])
        if not job_id:
            items = self._cancel_remaining(items, start=index)
            updated = await self._save(
                batch=batch,
                owner_id=owner_id,
                novel_id=novel_id,
                fields=self._terminal_fields(items=items, cancelled=True),
            )
            return _projection(updated, now_epoch=self._now_epoch())
        try:
            job = await self.portraits.cancel(
                owner_id=owner_id,
                novel_id=novel_id,
                card_id=str(item["card_id"]),
                job_id=job_id,
            )
        except Exception as error:
            failure = PortraitBatchFailure(
                code="batch_cancel_failed",
                message=str(error) or "当前立绘任务取消失败",
                action="保留本批次并重新确认取消；系统不会提交后续角色",
                retryable=True,
            )
            updated = await self._save(
                batch=batch,
                owner_id=owner_id,
                novel_id=novel_id,
                fields={
                    "status": "cancelling",
                    "failure": failure.model_dump(mode="json"),
                },
            )
            return _projection(updated, now_epoch=self._now_epoch())
        if not job.terminal:
            job_failure = _failure_from_job(job)
            items[index] = {
                **item,
                "status": "running",
                "job_id": job.job_id,
                "job_status": job.status,
                "queue_position": job.queue_position,
                "submit_count": max(0, int(job.submit_count)),
                "completed_images": max(0, int(job.completed_images)),
                "failure": (
                    job_failure.model_dump(mode="json")
                    if job_failure is not None
                    else None
                ),
            }
            updated = await self._save(
                batch=batch,
                owner_id=owner_id,
                novel_id=novel_id,
                fields={"items": items, "status": "cancelling"},
            )
            return _projection(updated, now_epoch=self._now_epoch())

        job_failure = _failure_from_job(job)
        items[index] = {
            **item,
            "status": _item_status_from_job(job),
            "job_id": job.job_id,
            "job_status": job.status,
            "queue_position": job.queue_position,
            "submit_count": max(0, int(job.submit_count)),
            "completed_images": max(0, int(job.completed_images)),
            "failure": (
                job_failure.model_dump(mode="json")
                if job_failure is not None
                else None
            ),
        }
        items = self._cancel_remaining(items, start=index + 1)
        updated = await self._save(
            batch=batch,
            owner_id=owner_id,
            novel_id=novel_id,
            fields=self._terminal_fields(items=items, cancelled=True),
        )
        return _projection(updated, now_epoch=self._now_epoch())

    async def cancel(
        self,
        *,
        owner_id: str,
        novel_id: str,
        batch_id: str,
    ) -> PortraitBatchProjection:
        owner_id = _canonical_id(owner_id, field_name="owner_id")
        novel_id = _canonical_id(novel_id, field_name="novel_id")
        batch_id = _canonical_id(batch_id, field_name="batch_id")
        batch = await self.batches.get_owned_batch(
            owner_id=owner_id,
            novel_id=novel_id,
            batch_id=batch_id,
        )
        if batch is None:
            raise PortraitBatchNotFoundError("批量立绘任务不存在")
        if batch.get("is_terminal"):
            return _projection(batch, now_epoch=self._now_epoch())
        if not batch.get("cancel_requested"):
            batch = await self._save(
                batch=batch,
                owner_id=owner_id,
                novel_id=novel_id,
                fields={"cancel_requested": True, "status": "cancelling"},
            )
        return await self._advance_cancel(
            batch=batch,
            owner_id=owner_id,
            novel_id=novel_id,
        )


character_portrait_batch_service = CharacterPortraitBatchService(
    batches=image_batch_repo,
    image_jobs=image_job_repo,
    portraits=character_portrait_service,
    provider_inspector=ConfiguredImageProviderResolver(),
    cards=ReferenceCardService,
    now=get_utc_now,
)


__all__ = [
    "CharacterPortraitBatchService",
    "MAX_PORTRAIT_BATCH_ITEMS",
    "PORTRAIT_BATCH_PLAN_REVISION",
    "PortraitBatchConflictError",
    "PortraitBatchFailure",
    "PortraitBatchItemInput",
    "PortraitBatchNotFoundError",
    "PortraitBatchPlanConfirmation",
    "PortraitBatchPlanProjection",
    "PortraitBatchPlanRequest",
    "PortraitBatchPlanStaleError",
    "PortraitBatchProjection",
    "PortraitBatchStartRequest",
    "character_portrait_batch_service",
]
