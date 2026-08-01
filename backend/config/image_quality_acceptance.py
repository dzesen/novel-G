"""受信的分阶段插图质量验收记录。

记录独立于用户可编辑配置；普通设置保存不能伪造 ``accepted``。生产文件位于
``reports/``，因此不会把部署指纹、评测结果或原始图片带入版本库。读取不存在的
文件保持纯只读，只有显式验收命令可以写入。
"""

from __future__ import annotations

import json
import os
from copy import deepcopy
from datetime import datetime
from pathlib import Path, PurePosixPath
import re
from threading import RLock
from typing import Any, Literal, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from backend.config.image_providers import ImagePipelineStageQualityEvidence


_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_PIPELINE_ALIAS = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")
QUALITY_ACCEPTANCE_STORE_VERSION = 1


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class IllustrationQualityCaseRating(_FrozenModel):
    case_id: str = Field(min_length=1, max_length=120)
    identity_replacement: bool
    identity_edit_outcome: Literal["clear_win", "tie", "clear_regression"]
    scene_following_regression: bool

    @field_validator("case_id")
    @classmethod
    def normalize_case_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("case_id must not be empty")
        return normalized


class IllustrationQualityMetrics(_FrozenModel):
    sample_count: int = Field(ge=0)
    identity_replacements: int = Field(ge=0)
    identity_edit_clear_wins: int = Field(ge=0)
    identity_edit_clear_regressions: int = Field(ge=0)
    scene_following_regressions: int = Field(ge=0)

    @property
    def passed(self) -> bool:
        return (
            self.sample_count == 12
            and self.identity_replacements == 0
            and self.identity_edit_clear_wins >= 7
            and self.identity_edit_clear_regressions <= 2
            and self.scene_following_regressions <= 2
        )


def score_illustration_quality_cases(
    ratings: tuple[IllustrationQualityCaseRating, ...]
    | list[IllustrationQualityCaseRating],
) -> IllustrationQualityMetrics:
    return IllustrationQualityMetrics(
        sample_count=len(ratings),
        identity_replacements=sum(item.identity_replacement for item in ratings),
        identity_edit_clear_wins=sum(
            item.identity_edit_outcome == "clear_win" for item in ratings
        ),
        identity_edit_clear_regressions=sum(
            item.identity_edit_outcome == "clear_regression" for item in ratings
        ),
        scene_following_regressions=sum(
            item.scene_following_regression for item in ratings
        ),
    )


class IllustrationQualityAcceptanceRecord(_FrozenModel):
    schema_version: Literal[1] = 1
    pipeline_alias: str
    pipeline_revision: str
    quality_fingerprint: str
    stage_evidence: dict[str, ImagePipelineStageQualityEvidence]
    sample_set_revision: str
    evaluated_at: datetime
    evaluator: str = Field(min_length=1, max_length=200)
    evidence_document: str = Field(min_length=1, max_length=500)
    ratings: tuple[IllustrationQualityCaseRating, ...]
    metrics: IllustrationQualityMetrics
    decision: Literal["accepted", "rejected"]

    @field_validator("pipeline_alias")
    @classmethod
    def validate_pipeline_alias(cls, value: str) -> str:
        if value != value.strip() or _PIPELINE_ALIAS.fullmatch(value) is None:
            raise ValueError("pipeline_alias must be a canonical ASCII identifier")
        return value

    @field_validator(
        "pipeline_revision",
        "quality_fingerprint",
        "sample_set_revision",
    )
    @classmethod
    def validate_hash(cls, value: str) -> str:
        if _SHA256.fullmatch(value.strip()) is None:
            raise ValueError("quality acceptance hashes must be sha256 digests")
        return value.strip()

    @field_validator("evaluator")
    @classmethod
    def normalize_evaluator(cls, value: str) -> str:
        return value.strip()

    @field_validator("evidence_document")
    @classmethod
    def validate_evidence_document(cls, value: str) -> str:
        normalized = value.strip().replace("\\", "/")
        path = PurePosixPath(normalized)
        if (
            not normalized
            or path.is_absolute()
            or ".." in path.parts
            or path.parts[0] not in {"docs", "reports"}
        ):
            raise ValueError(
                "evidence_document must be a repository-relative docs/ or reports/ path"
            )
        return normalized

    @model_validator(mode="after")
    def validate_decision_and_metrics(self) -> "IllustrationQualityAcceptanceRecord":
        if set(self.stage_evidence) not in (
            {"compose", "identity_edit"},
            {"compose", "identity_edit", "refine"},
        ):
            raise ValueError("stage_evidence must describe the complete fixed pipeline")
        if len(self.ratings) != 12:
            raise ValueError("the fixed illustration quality gate requires 12 ratings")
        case_ids = [item.case_id for item in self.ratings]
        if len(set(case_ids)) != len(case_ids):
            raise ValueError("illustration quality case IDs must be unique")
        scored = score_illustration_quality_cases(list(self.ratings))
        if self.metrics != scored:
            raise ValueError("quality metrics must be derived from the recorded ratings")
        expected_decision = "accepted" if scored.passed else "rejected"
        if self.decision != expected_decision:
            raise ValueError("quality decision does not match the frozen thresholds")
        return self


class ImageQualityAcceptanceStore(Protocol):
    def put(
        self,
        record: IllustrationQualityAcceptanceRecord,
    ) -> IllustrationQualityAcceptanceRecord: ...

    def find_exact(
        self,
        pipeline_alias: str,
        quality_fingerprint: str,
    ) -> IllustrationQualityAcceptanceRecord | None: ...

    def latest_for_alias(
        self,
        pipeline_alias: str,
    ) -> IllustrationQualityAcceptanceRecord | None: ...

    def records_for_alias(
        self,
        pipeline_alias: str,
    ) -> tuple[IllustrationQualityAcceptanceRecord, ...]: ...


class MemoryImageQualityAcceptanceStore:
    def __init__(
        self,
        records: list[IllustrationQualityAcceptanceRecord] | None = None,
    ) -> None:
        self._records = list(records or [])

    def put(
        self,
        record: IllustrationQualityAcceptanceRecord,
    ) -> IllustrationQualityAcceptanceRecord:
        self._records = [
            item
            for item in self._records
            if not (
                item.pipeline_alias == record.pipeline_alias
                and item.quality_fingerprint == record.quality_fingerprint
            )
        ]
        self._records.append(record)
        return record

    def find_exact(
        self,
        pipeline_alias: str,
        quality_fingerprint: str,
    ) -> IllustrationQualityAcceptanceRecord | None:
        matches = [
            item
            for item in self._records
            if item.pipeline_alias == pipeline_alias
            and item.quality_fingerprint == quality_fingerprint
        ]
        return max(matches, key=lambda item: item.evaluated_at, default=None)

    def latest_for_alias(
        self,
        pipeline_alias: str,
    ) -> IllustrationQualityAcceptanceRecord | None:
        matches = [
            item for item in self._records if item.pipeline_alias == pipeline_alias
        ]
        return max(matches, key=lambda item: item.evaluated_at, default=None)

    def records_for_alias(
        self,
        pipeline_alias: str,
    ) -> tuple[IllustrationQualityAcceptanceRecord, ...]:
        return tuple(
            sorted(
                (item for item in self._records if item.pipeline_alias == pipeline_alias),
                key=lambda item: item.evaluated_at,
                reverse=True,
            )
        )


class FileImageQualityAcceptanceStore(MemoryImageQualityAcceptanceStore):
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = RLock()
        super().__init__(self._read())

    def _read(self) -> list[IllustrationQualityAcceptanceRecord]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if (
                not isinstance(payload, dict)
                or payload.get("version") != QUALITY_ACCEPTANCE_STORE_VERSION
                or not isinstance(payload.get("records"), list)
            ):
                return []
            return [
                IllustrationQualityAcceptanceRecord.model_validate(item)
                for item in payload["records"]
                if isinstance(item, dict)
            ]
        except (FileNotFoundError, ValueError, TypeError, json.JSONDecodeError):
            return []

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{uuid4().hex}.tmp")
        payload = {
            "version": QUALITY_ACCEPTANCE_STORE_VERSION,
            "records": [
                item.model_dump(mode="json") for item in self._records
            ],
        }
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as file:
                json.dump(
                    payload,
                    file,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary.exists():
                temporary.unlink()

    def put(
        self,
        record: IllustrationQualityAcceptanceRecord,
    ) -> IllustrationQualityAcceptanceRecord:
        with self._lock:
            self._records = self._read()
            stored = super().put(record)
            self._write()
            return stored

    def find_exact(
        self,
        pipeline_alias: str,
        quality_fingerprint: str,
    ) -> IllustrationQualityAcceptanceRecord | None:
        with self._lock:
            self._records = self._read()
            return super().find_exact(pipeline_alias, quality_fingerprint)

    def latest_for_alias(
        self,
        pipeline_alias: str,
    ) -> IllustrationQualityAcceptanceRecord | None:
        with self._lock:
            self._records = self._read()
            return super().latest_for_alias(pipeline_alias)

    def records_for_alias(
        self,
        pipeline_alias: str,
    ) -> tuple[IllustrationQualityAcceptanceRecord, ...]:
        with self._lock:
            self._records = self._read()
            return super().records_for_alias(pipeline_alias)

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return deepcopy(
                [item.model_dump(mode="json") for item in self._records]
            )


__all__ = [
    "FileImageQualityAcceptanceStore",
    "ImageQualityAcceptanceStore",
    "IllustrationQualityAcceptanceRecord",
    "IllustrationQualityCaseRating",
    "IllustrationQualityMetrics",
    "MemoryImageQualityAcceptanceStore",
    "QUALITY_ACCEPTANCE_STORE_VERSION",
    "score_illustration_quality_cases",
]
