"""Deterministic, content-preserving scene word-budget convergence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from backend.services.novel.chapter_service import count_chapter_words


@dataclass(frozen=True)
class SceneWordBudgetTrim:
    text: str
    original_word_count: int
    discarded_word_count: int = 0
    boundary: Literal["sentence", "word"] | None = None

    @property
    def trimmed(self) -> bool:
        return self.discarded_word_count > 0


_SCENE_SENTENCE_END_CHARACTERS = frozenset("。！？!?…")
_SCENE_SENTENCE_CLOSING_CHARACTERS = frozenset(
    "”’」』】）》)]} \t\r\n"
)


def _longest_prefix_with_word_limit(text: str, maximum_words: int) -> str:
    """Return the longest source prefix whose Novel-G word count fits."""

    source = str(text or "")
    limit = max(0, int(maximum_words))
    low = 0
    high = len(source)
    while low < high:
        middle = (low + high + 1) // 2
        if count_chapter_words(source[:middle]) <= limit:
            low = middle
        else:
            high = middle - 1
    return source[:low].rstrip()


def trim_scene_contribution_to_word_budget(
    *,
    current_text: str,
    contribution: str,
    maximum_words: int | None,
    enabled: bool,
) -> SceneWordBudgetTrim:
    """Converge one Provider contribution without inventing prose."""

    source = str(contribution or "").strip()
    original_word_count = count_chapter_words(source)
    if not enabled or maximum_words is None:
        return SceneWordBudgetTrim(
            text=source,
            original_word_count=original_word_count,
        )
    current_word_count = count_chapter_words(current_text)
    remaining_words = max(0, int(maximum_words) - current_word_count)
    if original_word_count <= remaining_words:
        return SceneWordBudgetTrim(
            text=source,
            original_word_count=original_word_count,
        )

    hard_prefix = _longest_prefix_with_word_limit(source, remaining_words)
    sentence_end = max(
        (
            hard_prefix.rfind(character) + 1
            for character in _SCENE_SENTENCE_END_CHARACTERS
        ),
        default=0,
    )
    if sentence_end > 0:
        while (
            sentence_end < len(hard_prefix)
            and hard_prefix[sentence_end]
            in _SCENE_SENTENCE_CLOSING_CHARACTERS
        ):
            sentence_end += 1
        retained = hard_prefix[:sentence_end].rstrip()
        boundary: Literal["sentence", "word"] = "sentence"
    else:
        retained = hard_prefix
        boundary = "word"
    retained_word_count = count_chapter_words(retained)
    return SceneWordBudgetTrim(
        text=retained,
        original_word_count=original_word_count,
        discarded_word_count=max(0, original_word_count - retained_word_count),
        boundary=boundary,
    )
