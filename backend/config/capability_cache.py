"""独立于用户配置和 revision 的 Provider 能力缓存。"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
from threading import RLock
import time
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


class CapabilityCacheRecord(BaseModel):
    alias: str
    fingerprint: str
    capabilities: dict[str, Any] = Field(default_factory=dict)
    tested_at: float
    revision: str


class FileCapabilityCacheStore:
    """用 HMAC 指纹匹配已保存 Provider；缓存文件不含密钥。"""

    def __init__(self, path: Path, *, key: bytes) -> None:
        self.path = Path(path)
        self._key = key
        self._lock = RLock()

    def _fingerprint(self, provider: dict[str, Any]) -> str:
        payload = json.dumps(provider, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hmac.new(self._key, payload.encode("utf-8"), hashlib.sha256).hexdigest()

    def _read(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (FileNotFoundError, ValueError, json.JSONDecodeError):
            return {}

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as file:
                json.dump(data, file, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary.exists():
                temporary.unlink()

    def put(
        self,
        alias: str,
        provider: dict[str, Any],
        capabilities: dict[str, Any],
    ) -> CapabilityCacheRecord:
        with self._lock:
            record = CapabilityCacheRecord(
                alias=alias,
                fingerprint=self._fingerprint(provider),
                capabilities=capabilities,
                tested_at=time.time(),
                revision=uuid4().hex,
            )
            data = self._read()
            data[alias] = record.model_dump()
            self._write(data)
            return record

    def get(self, alias: str, provider: dict[str, Any]) -> CapabilityCacheRecord | None:
        with self._lock:
            raw = self._read().get(alias)
            if not isinstance(raw, dict):
                return None
            try:
                record = CapabilityCacheRecord.model_validate(raw)
            except ValueError:
                return None
            if not hmac.compare_digest(record.fingerprint, self._fingerprint(provider)):
                return None
            return record
