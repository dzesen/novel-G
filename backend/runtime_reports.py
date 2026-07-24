"""Small, non-sensitive local operation reports used by diagnostics."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parent.parent
REPORTS_DIR = ROOT_DIR / "reports"


def report_path(name: str) -> Path:
    normalized = "".join(char for char in name if char.isalnum() or char in {"-", "_"})
    if not normalized:
        raise ValueError("Runtime report name is empty")
    return REPORTS_DIR / f"last-{normalized}.json"


def write_runtime_report(name: str, payload: dict[str, Any]) -> Path:
    path = report_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "format": "novel-generator-runtime-report",
        "version": 1,
        "kind": name,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        **payload,
    }
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, default=str)
        + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
    return path


def read_runtime_report(name: str) -> dict[str, Any] | None:
    path = report_path(name)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None
