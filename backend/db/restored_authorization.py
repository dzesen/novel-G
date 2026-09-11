"""Restored execution records are audit history, never reusable authority."""
from __future__ import annotations
from collections.abc import Mapping

RESTORED_AUTHORITY_FIELD = "authorization_invalidated_by_restore"
RESTORED_AUTHORITY_MESSAGE = "备份恢复后原作业授权已失效，请重新预检并创建新作业。"


def require_current_authorization(document: Mapping) -> None:
    if document.get(RESTORED_AUTHORITY_FIELD) is not None:
        raise ValueError(RESTORED_AUTHORITY_MESSAGE)
