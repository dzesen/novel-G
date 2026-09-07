"""Product bounds shared by new requests and historical generation preflight."""
from collections.abc import Mapping
from typing import Annotated, Any

from pydantic import Field, StringConstraints

MIN_CHAPTERS = 1
MAX_CHAPTERS = 10_000
MIN_WORDS_PER_CHAPTER = 500
MAX_WORDS_PER_CHAPTER = 50_000

ChapterCount = Annotated[int, Field(strict=True, ge=MIN_CHAPTERS, le=MAX_CHAPTERS)]
WordsPerChapter = Annotated[int, Field(strict=True, ge=MIN_WORDS_PER_CHAPTER, le=MAX_WORDS_PER_CHAPTER)]
CreationIdea = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


def invalid_novel_scale_fields(novel: Mapping[str, Any]) -> list[str]:
    """Absent legacy optional values retain their established defaults.

    An explicit zero, Boolean, numeric string or out-of-range value is never
    reinterpreted as an absent value. Reading this function never repairs data.
    """
    invalid = []
    for field, low, high in (
        ("number_of_chapters", MIN_CHAPTERS, MAX_CHAPTERS),
        ("words_per_chapter", MIN_WORDS_PER_CHAPTER, MAX_WORDS_PER_CHAPTER),
    ):
        value = novel.get(field)
        if value is not None and (type(value) is not int or not low <= value <= high):
            invalid.append(field)
    return invalid


class InvalidNovelScale(ValueError):
    pass
