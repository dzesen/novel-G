"""Task-local authority for one persistently leased Generation Job worker."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Iterator, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


JOB_EXECUTION_LEASE_SECONDS = 30
MAX_JOB_EXECUTION_EPOCH = 2**63 - 1


class JobExecutionLeaseUnavailable(RuntimeError):
    """Another worker still owns the live Generation Job execution lease."""


class JobExecutionLeaseLost(RuntimeError):
    """The current task no longer owns its Generation Job execution lease."""


class JobExecutionLeaseV1(BaseModel):
    """Persistent authority shared by a Job worker and all child tasks."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["job_execution_lease.v1"]
    job_id: str = Field(pattern=r"^[0-9a-f]{24}$")
    worker_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    epoch: int = Field(ge=1, le=MAX_JOB_EXECUTION_EPOCH)
    heartbeat_at: datetime
    expires_at: datetime

    @model_validator(mode="after")
    def validate_times(self) -> "JobExecutionLeaseV1":
        if self.heartbeat_at.tzinfo is None or self.expires_at.tzinfo is None:
            raise ValueError("Job execution lease timestamps must be timezone-aware")
        if (
            self.expires_at.astimezone(timezone.utc)
            <= self.heartbeat_at.astimezone(timezone.utc)
        ):
            raise ValueError("Job execution lease must expire after its heartbeat")
        return self


_CURRENT_JOB_EXECUTION: ContextVar[JobExecutionLeaseV1 | None] = ContextVar(
    "current_generation_job_execution",
    default=None,
)


def current_job_execution() -> JobExecutionLeaseV1 | None:
    """Return the worker authority inherited by the current async task."""

    return _CURRENT_JOB_EXECUTION.get()


@contextmanager
def bind_job_execution(
    lease: JobExecutionLeaseV1,
) -> Iterator[JobExecutionLeaseV1]:
    """Fence all GenerationJobRepository access in this task tree."""

    frozen = JobExecutionLeaseV1.model_validate(
        lease.model_dump(mode="python")
    )
    token = _CURRENT_JOB_EXECUTION.set(frozen)
    try:
        yield frozen
    finally:
        _CURRENT_JOB_EXECUTION.reset(token)
