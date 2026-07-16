"""Location, item and world-rule card repository."""

from backend.db.repositories.reference_card_repository import ReferenceCardRepository


worldbook_repo = ReferenceCardRepository("worldbook", {"location", "item", "rule"})
