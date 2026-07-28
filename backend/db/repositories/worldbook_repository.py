"""Location, item, world-rule and neutral lore card repository."""

from backend.db.collections import WORLDBOOK
from backend.db.repositories.reference_card_repository import ReferenceCardRepository


worldbook_repo = ReferenceCardRepository(
    WORLDBOOK,
    {"location", "item", "rule", "lore"},
)
