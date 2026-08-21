"""协议无关的图像生成异步任务契约。"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal, Protocol

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    field_validator,
    model_validator,
)

from backend.services.novel.appearance_anchor import RuntimeFingerprintSchema


class ImageFailureCode(str, Enum):
    """图像生成失败码闭集；新增成员必须重新审视重试与协议集合。"""

    PROVIDER_UNAVAILABLE = "provider_unavailable"
    CONTENT_POLICY_REJECTED = "content_policy_rejected"
    RATE_LIMITED = "rate_limited"
    QUOTA_EXHAUSTED = "quota_exhausted"
    PROVIDER_CONFIGURATION_ERROR = "provider_configuration_error"
    WORKFLOW_VALIDATION_FAILED = "workflow_validation_failed"
    PARTIAL_WORKFLOW_VALIDATION = "partial_workflow_validation"
    DEPENDENCY_MISSING = "dependency_missing"
    EXECUTION_FAILED = "execution_failed"
    OUT_OF_MEMORY = "out_of_memory"
    QUEUE_FULL = "queue_full"
    CANCELLED = "cancelled"
    ASSET_EXPIRED = "asset_expired"
    JOB_LOST = "job_lost"


RETRYABLE_IMAGE_FAILURE_CODES: frozenset[ImageFailureCode] = frozenset(
    {ImageFailureCode.PROVIDER_UNAVAILABLE}
)

COMFYUI_FAILURE_CODES: frozenset[ImageFailureCode] = frozenset(
    {
        ImageFailureCode.PROVIDER_UNAVAILABLE,
        ImageFailureCode.WORKFLOW_VALIDATION_FAILED,
        ImageFailureCode.PARTIAL_WORKFLOW_VALIDATION,
        ImageFailureCode.DEPENDENCY_MISSING,
        ImageFailureCode.EXECUTION_FAILED,
        ImageFailureCode.OUT_OF_MEMORY,
        ImageFailureCode.QUEUE_FULL,
        ImageFailureCode.CANCELLED,
        ImageFailureCode.ASSET_EXPIRED,
        ImageFailureCode.JOB_LOST,
    }
)


def is_image_failure_retryable(code: ImageFailureCode) -> bool:
    """唯一的可重试性判定入口。"""

    return code in RETRYABLE_IMAGE_FAILURE_CODES


class _ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ImageFailure(_ContractModel):
    code: ImageFailureCode
    message: str
    action: str
    details: dict[str, Any] = Field(default_factory=dict)

    @computed_field
    @property
    def retryable(self) -> bool:
        return is_image_failure_retryable(self.code)


class ImageProviderError(RuntimeError):
    """在 submit 前或 Provider HTTP 交互中得到的结构化业务失败。"""

    def __init__(self, failure: ImageFailure) -> None:
        super().__init__(failure.message)
        self.failure = failure


class ImageProviderPreSubmitError(ImageProviderError):
    """A provider rejected locally before issuing any external request."""


class ImageInputAsset(_ContractModel):
    """仅驻留内存、供 ComfyUI 上传的参考图。"""

    filename: str = Field(min_length=1)
    content: bytes = Field(repr=False)
    mime_type: str = Field(default="application/octet-stream", min_length=1)

    @model_validator(mode="after")
    def validate_filename(self) -> "ImageInputAsset":
        if "/" in self.filename or "\\" in self.filename:
            raise ValueError("Image input filename must not contain a path")
        return self


ImageSlotValue = str | int | float | bool | None | ImageInputAsset


class ImageProviderExpectation(_ContractModel):
    """Frozen provider facts that must match the concrete submit payload."""

    alias: str = Field(min_length=1)
    model: str = Field(min_length=1)
    workflow_revision: str = Field(min_length=1)


class ImageGenerationRequest(_ContractModel):
    usage: Literal["character_portrait", "cover", "scene_illustration"]
    slot_values: dict[str, ImageSlotValue] = Field(default_factory=dict)
    required_slots: frozenset[str] = Field(default_factory=frozenset)
    provider_expectation: ImageProviderExpectation | None = None

    @model_validator(mode="after")
    def validate_slot_names(self) -> "ImageGenerationRequest":
        names = set(self.slot_values) | set(self.required_slots)
        if any(not str(name).strip() for name in names):
            raise ValueError("Image semantic slot names must not be empty")
        return self


class ImageOutputBindingSnapshot(_ContractModel):
    node_id: str
    field: str


class ImageJobAudit(_ContractModel):
    template_revision: str
    submitted_graph_hash: str
    comfyui_version: str
    seed: str | None = None
    checkpoint_names: tuple[str, ...] = ()
    lora_names: tuple[str, ...] = ()
    input_asset_hashes: dict[str, str] = Field(default_factory=dict)
    runtime_fingerprint: RuntimeFingerprintSchema = Field(
        default_factory=RuntimeFingerprintSchema
    )

    @field_validator("seed", mode="before")
    @classmethod
    def normalize_seed(cls, value: Any) -> str | None:
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
        return str(parsed)


class ImageJobHandle(_ContractModel):
    provider_alias: str
    prompt_id: str
    submitted_at_epoch: float
    timeout_seconds: int
    ignored_slots: tuple[str, ...] = ()
    outputs: tuple[ImageOutputBindingSnapshot, ...] = ()
    audit: ImageJobAudit


class ImageArtifact(_ContractModel):
    content: bytes = Field(repr=False)
    content_hash: str
    mime_type: str
    filename: str
    subfolder: str
    provider_storage_type: str


ImagePollStatus = Literal[
    "pending",
    "queued",
    "running",
    "succeeded",
    "failed",
    "rejected",
    "cancelled",
]


class ImagePollResult(_ContractModel):
    status: ImagePollStatus
    queue_position: int | None = None
    ignored_slots: tuple[str, ...] = ()
    artifacts: tuple[ImageArtifact, ...] = ()
    failure: ImageFailure | None = None

    @model_validator(mode="after")
    def validate_terminal_payload(self) -> "ImagePollResult":
        if self.status == "succeeded" and not self.artifacts:
            raise ValueError("Succeeded image jobs must include artifacts")
        if self.status in {"failed", "rejected", "cancelled"} and self.failure is None:
            raise ValueError(
                "Failed, rejected, or cancelled image jobs must include a failure"
            )
        return self

    @property
    def is_terminal(self) -> bool:
        """Whether the provider job itself is finished.

        A retryable ``failed`` result means only that this poll attempt could
        not reach the provider. The original handle remains live and must be
        polled again without another submit.
        """

        if self.status in {"pending", "queued", "running"}:
            return False
        if (
            self.status == "failed"
            and self.failure is not None
            and self.failure.retryable
        ):
            return False
        return True


ImageCancelStatus = Literal["cancelled", "already_finished", "failed"]


class ImageCancelResult(_ContractModel):
    status: ImageCancelStatus
    failure: ImageFailure | None = None

    @model_validator(mode="after")
    def validate_failure(self) -> "ImageCancelResult":
        if self.status in {"cancelled", "failed"} and self.failure is None:
            raise ValueError("Cancellation result must include its structured outcome")
        return self


class ImageProvider(Protocol):
    async def submit(self, request: ImageGenerationRequest) -> ImageJobHandle: ...

    async def poll(self, handle: ImageJobHandle) -> ImagePollResult: ...

    async def cancel(self, handle: ImageJobHandle) -> ImageCancelResult: ...
