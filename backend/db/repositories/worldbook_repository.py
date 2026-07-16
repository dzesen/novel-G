"""Location, item and world-rule card repository."""

from backend.db.collections import WORLDBOOK
from backend.db.repositories.reference_card_repository import ReferenceCardRepository


worldbook_repo = ReferenceCardRepository(WORLDBOOK, {"location", "item", "rule"})
