"""Resolve immutable application resources and persistent desktop data separately."""
from __future__ import annotations

import os
from pathlib import Path

RESOURCE_ROOT = Path(__file__).resolve().parents[1]
DATA_DIRECTORY_ENV = "NOVEL_G_DATA_DIR"


def desktop_data_root() -> Path | None:
    value = os.environ.get(DATA_DIRECTORY_ENV, "").strip()
    if not value:
        return None
    path = Path(value)
    if not path.is_absolute():
        raise ValueError("NOVEL_G_DATA_DIR must be an absolute directory")
    return path.resolve()


def data_root() -> Path:
    return desktop_data_root() or RESOURCE_ROOT


def data_path(*parts: str) -> Path:
    return data_root().joinpath(*parts)


def resource_path(*parts: str) -> Path:
    return RESOURCE_ROOT.joinpath(*parts)
