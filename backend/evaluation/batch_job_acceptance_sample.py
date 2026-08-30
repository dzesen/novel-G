"""Closed sample choices shared by readonly approval and paid evaluation."""

from __future__ import annotations

from enum import Enum
import hashlib
import json
from pathlib import Path
from typing import Any


JOB_POLL_SECONDS = 0.5
PAUSE_TIMEOUT_SECONDS = 45.0
COMPLETION_TIMEOUT_SECONDS = 1800.0
REPRESENTATIVE_MAXIMUM_REAL_RUNS = 2


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _prompt_protocol_digest() -> str:
    from backend.llm.prompts.prompt_selector import (
        CHAPTER_OUTLINE_PROMPT_NAME,
        CHAPTER_STATE_PROMPT_NAME,
        OUTLINE_ADHERENCE_PROMPT_NAME,
        PROSE_PROMPT_NAME,
        load_prompt_config,
    )

    prompts = load_prompt_config()
    backend_dir = Path(__file__).resolve().parents[1]
    # Code-defined Planner/Tool instructions, schemas and continuation prompts
    # are part of the frozen protocol too.  No config/secret/report is read.
    sources = {
        path.relative_to(backend_dir).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(backend_dir.rglob("*.py"))
    }
    return _digest({
        "selected_templates": {
            name: prompts[name]
            for name in (
                CHAPTER_OUTLINE_PROMPT_NAME, PROSE_PROMPT_NAME,
                OUTLINE_ADHERENCE_PROMPT_NAME, CHAPTER_STATE_PROMPT_NAME,
            )
        },
        "backend_source_digests": sources,
    })


class BatchJobAcceptanceSample(str, Enum):
    STRESS_10000 = "stress-10000-v42"
    REPRESENTATIVE_3000 = "representative-3000-v1"

    @property
    def target_word_count(self) -> int:
        return 3000 if self is self.REPRESENTATIVE_3000 else 10000

    @property
    def maximum_scene_count(self) -> int:
        return 2 if self is self.REPRESENTATIVE_3000 else 20

    def scope(self) -> dict[str, int]:
        return {
            "chapter_count": 3,
            "target_word_count": self.target_word_count,
            "max_accepted_outline_scenes": self.maximum_scene_count,
        }

    def scene_word_budgets(self) -> list[dict[str, int]]:
        if self is self.STRESS_10000:
            return []
        return [
            {"min": 1200, "target": 1500, "max": 1800},
            {"min": 1200, "target": 1500, "max": 1800},
        ]

    def contract(self) -> dict[str, Any]:
        if self is self.STRESS_10000:
            return {}
        return {
            "schema_version": "batch_job_representative_sample.v1",
            "sample_id": self.value,
            "scene_word_budgets": self.scene_word_budgets(),
            "fixture_blueprint_digest": _digest(self.fixture_blueprint()),
            "prompt_protocol_digest": _prompt_protocol_digest(),
            "execution_controls": {
                "job_poll_seconds": JOB_POLL_SECONDS,
                "pause_timeout_seconds": PAUSE_TIMEOUT_SECONDS,
                "completion_timeout_seconds": COMPLETION_TIMEOUT_SECONDS,
            },
            "maximum_real_runs": REPRESENTATIVE_MAXIMUM_REAL_RUNS,
            "same_readiness_digest_required": True,
            "successor_requires_new_decision_and_authorization": True,
        }

    def fixture_blueprint(self) -> dict[str, Any]:
        """Fresh material copies; persistent IDs are deliberately not stimulus."""
        arc = "从接信、穿越封锁到在黎明前交付。"
        if self is self.REPRESENTATIVE_3000:
            arc += (
                "本卷恰好三章，每章 target_word_count=3000，恰好两场。"
                '每一场 word_budget 固定为 {"min":1200,"target":1500,"max":1800}。'
                "第一章两场依次为接过密封信、确认渡河路线；"
                "第二章两场依次为通过封锁路口、抵达渡口；"
                "第三章两场依次为渡河、黎明前交付并结束本卷任务。"
                "章纲必须将本章两场分别写成完整的场景状态转移合同，不增减场数或字数分配。"
            )
        return {
            "novel": {
                "title": "__batch_job_end_to_end_fixture_representative_3000_v1__",
                "subtitle": "隔离批量验收夹具",
                "genre": "synthetic-test",
                "summary": "仅用于批量作业端到端验收的合成故事材料。",
                "core_seed": "一名信使必须在黎明前把密封信送到河对岸。",
                "worldview": "合成的近未来河港城市，所有人物与事件均为测试用途。",
                "writing_style": "克制、清晰的中文叙事。",
                "narrative_pov": "第三人称有限视角",
                "tone": "紧张而克制",
                "era_background": "近未来",
                "words_per_chapter": self.target_word_count,
                "narrative_revision": 0,
            },
            "volume": {
                "title": "隔离验收卷",
                "summary": "信使在河港城市完成一次有限的送信任务。",
                "arc": arc,
                "order_index": 1,
            },
            "chapters": [
                {"title": f"隔离验收第 {order} 章", "order_index": order, "content": ""}
                for order in range(1, 4)
            ],
        }

    def execution_protocol(self, stress_protocol: str) -> str:
        if self is self.STRESS_10000:
            return stress_protocol
        return stress_protocol + ".representative-v1"
