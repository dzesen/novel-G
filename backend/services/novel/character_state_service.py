"""人物人工订正的可恢复领域写入。"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from backend.db.errors import NotFoundError
from backend.db.mutation import MutationCommand, commit_mutation
from backend.db.repositories.character_state_repository import (
    FACT_KIND_VALUES,
    character_state_repo,
)
from backend.db.utils import to_object_id
from backend.services.novel.narrative_timeline import narrative_timeline
from backend.services.novel.state_timeline import record_manual_correction


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, default=str
        ).encode("utf-8")
    ).hexdigest()[:24]


def _fact_from_state(state: dict[str, Any] | None, fact_id: str) -> dict[str, Any]:
    for fact in (state or {}).get("permanent_facts") or []:
        if str(fact.get("id")) == str(fact_id):
            return dict(fact)
    raise NotFoundError(f"Permanent fact '{fact_id}' was not found")


class CharacterStateService:
    @staticmethod
    async def _record_correction_and_refresh(
        session,
        mutation,
        *,
        correction_type: str,
        subject_id: str,
        fields: dict[str, Any],
    ) -> None:
        command = mutation.journal["command"]["payload"]
        novel_id = str(mutation.journal["novel_id"])
        effective_chapter_id = str(command["effective_chapter_id"])
        if not mutation.was_received("correction"):
            await mutation.advance_phase("timeline_writes")
            await record_manual_correction(
                novel_id,
                effective_chapter_id,
                correction_type,
                subject_id,
                fields,
                baseline=command.get("state_baseline"),
                source_operation=str(mutation.journal["operation"]),
                source_operation_id=str(mutation.journal["idempotency_key"]),
                source_revision=int(
                    mutation.journal["command"].get("version") or 1
                ),
                session=session,
            )
            await mutation.receipt(
                "correction", {"effective_chapter_id": effective_chapter_id}
            )
        # Primary materialized writes can be replayed before journal completion.
        # Refresh every time so an old effective chapter never becomes "current".
        await mutation.advance_phase("derived_data")
        report = await narrative_timeline.refresh(novel_id, session=session)
        await mutation.receipt("projection", {"digest": report["digest"]})

    @staticmethod
    async def _execute_legacy_update_current_state(session, mutation):
        command = mutation.journal["command"]["payload"]
        novel_id = str(mutation.journal["novel_id"])
        await character_state_repo.set_current_state(
            novel_id,
            str(command["card_id"]),
            str(command["current_state"]),
            int(command["chapter_order"]),
            session=session,
        )
        state = await character_state_repo.get_state(
            novel_id, str(command["card_id"]), session=session
        )
        if state is None:
            raise NotFoundError(
                f"Character state for card '{command['card_id']}' was not found"
            )
        return state

    @staticmethod
    async def _execute_update_current_state(session, mutation):
        command = mutation.journal["command"]["payload"]
        novel_id = str(mutation.journal["novel_id"])
        await character_state_repo.set_current_state(
            novel_id,
            str(command["card_id"]),
            str(command["current_state"]),
            int(command["chapter_order"]),
            session=session,
            as_of_chapter_id=str(command["effective_chapter_id"]),
        )
        await mutation.receipt("state", {"card_id": str(command["card_id"])})
        await CharacterStateService._record_correction_and_refresh(
            session,
            mutation,
            correction_type="character_current_state",
            subject_id=str(command["card_id"]),
            fields={"current_state": str(command["current_state"])},
        )
        state = await character_state_repo.get_state(
            novel_id, str(command["card_id"]), session=session
        )
        if state is None:
            raise NotFoundError(
                f"Character state for card '{command['card_id']}' was not found"
            )
        return state

    @staticmethod
    async def update_current_state(
        novel_id: str,
        card_id: str,
        current_state: str,
        effective_chapter_id: str,
        chapter_order: int,
    ) -> dict[str, Any]:
        if int(chapter_order) <= 0:
            raise ValueError("as_of_chapter_order must be greater than 0")
        state = await character_state_repo.get_state(novel_id, card_id)
        if state is None:
            raise NotFoundError(f"Character state for card '{card_id}' was not found")
        payload = {
            "card_id": card_id,
            "current_state": str(current_state),
            "effective_chapter_id": effective_chapter_id,
            "chapter_order": int(chapter_order),
            "state_baseline": state,
        }
        return await commit_mutation(
            MutationCommand(
                novel_id=novel_id,
                idempotency_key=(
                    f"update-character-state:{card_id}:{state.get('updated_at')}:{_digest(payload)}"
                ),
                operation="update_character_current_state",
                payload=payload,
                before_image={"state": state},
            ),
            CharacterStateService._execute_update_current_state,
        )

    @staticmethod
    async def _execute_update_fact(session, mutation):
        command = mutation.journal["command"]["payload"]
        novel_id = str(mutation.journal["novel_id"])
        await character_state_repo.update_permanent_fact(
            novel_id,
            str(command["card_id"]),
            str(command["fact_id"]),
            dict(command["fields"]),
            session=session,
        )
        await mutation.receipt("fact", {"fact_id": str(command["fact_id"])})
        await CharacterStateService._record_correction_and_refresh(
            session,
            mutation,
            correction_type="permanent_fact",
            subject_id=str(command["fact_id"]),
            fields=dict(command["fields"]),
        )
        state = await character_state_repo.get_state(
            novel_id, str(command["card_id"]), session=session
        )
        if state is None:
            raise NotFoundError(
                f"Character state for card '{command['card_id']}' was not found"
            )
        return state

    @staticmethod
    async def _execute_legacy_update_fact(session, mutation):
        command = mutation.journal["command"]["payload"]
        novel_id = str(mutation.journal["novel_id"])
        await character_state_repo.update_permanent_fact(
            novel_id,
            str(command["card_id"]),
            str(command["fact_id"]),
            dict(command["fields"]),
            session=session,
        )
        state = await character_state_repo.get_state(
            novel_id, str(command["card_id"]), session=session
        )
        if state is None:
            raise NotFoundError(
                f"Character state for card '{command['card_id']}' was not found"
            )
        return state

    @staticmethod
    async def update_fact(
        novel_id: str,
        card_id: str,
        fact_id: str,
        fields: dict[str, Any],
        effective_chapter_id: str,
    ) -> dict[str, Any]:
        prepared = dict(fields)
        if prepared.get("fact") is not None and not str(prepared["fact"]).strip():
            raise ValueError("Permanent fact text cannot be empty")
        if prepared.get("kind") is not None and str(prepared["kind"]) not in FACT_KIND_VALUES:
            raise ValueError(f"Unsupported permanent fact kind: {prepared['kind']}")
        if prepared.get("chapter_order") is not None and int(prepared["chapter_order"]) <= 0:
            raise ValueError("chapter_order must be greater than 0")
        state = await character_state_repo.get_state(novel_id, card_id)
        fact = _fact_from_state(state, fact_id)
        if not prepared:
            return state
        payload = {
            "card_id": card_id,
            "fact_id": fact_id,
            "fields": prepared,
            "effective_chapter_id": effective_chapter_id,
            "state_baseline": state,
        }
        return await commit_mutation(
            MutationCommand(
                novel_id=novel_id,
                idempotency_key=(
                    f"update-permanent-fact:{fact_id}:{state.get('updated_at')}:{_digest(payload)}"
                ),
                operation="update_permanent_fact",
                payload=payload,
                before_image={"fact": fact},
            ),
            CharacterStateService._execute_update_fact,
        )

    @staticmethod
    async def _execute_delete_fact(session, mutation):
        command = mutation.journal["command"]["payload"]
        novel_id = str(mutation.journal["novel_id"])
        state = await character_state_repo.get_state(
            novel_id, str(command["card_id"]), session=session
        )
        exists = any(
            str(fact.get("id")) == str(command["fact_id"])
            for fact in (state or {}).get("permanent_facts") or []
        )
        if exists:
            await character_state_repo.delete_permanent_fact(
                novel_id,
                str(command["card_id"]),
                str(command["fact_id"]),
                session=session,
            )
        await mutation.receipt("fact", {"fact_id": str(command["fact_id"])})
        await CharacterStateService._record_correction_and_refresh(
            session,
            mutation,
            correction_type="permanent_fact_delete",
            subject_id=str(command["fact_id"]),
            fields={
                "card_id": str(command["card_id"]),
                "fact_snapshot": command["fact_snapshot"],
            },
        )
        state = await character_state_repo.get_state(
            novel_id, str(command["card_id"]), session=session
        )
        if state is None:
            raise NotFoundError(
                f"Character state for card '{command['card_id']}' was not found"
            )
        return state

    @staticmethod
    async def delete_fact(
        novel_id: str,
        card_id: str,
        fact_id: str,
        effective_chapter_id: str,
    ) -> dict[str, Any]:
        state = await character_state_repo.get_state(novel_id, card_id)
        fact = _fact_from_state(state, fact_id)
        payload = {
            "card_id": card_id,
            "fact_id": fact_id,
            "effective_chapter_id": effective_chapter_id,
            "fact_snapshot": fact,
            "state_baseline": state,
        }
        return await commit_mutation(
            MutationCommand(
                novel_id=novel_id,
                idempotency_key=(
                    f"delete-permanent-fact:{fact_id}:{effective_chapter_id}:{_digest(fact)}"
                ),
                operation="delete_permanent_fact",
                payload=payload,
                before_image={"state": state},
            ),
            CharacterStateService._execute_delete_fact,
        )
