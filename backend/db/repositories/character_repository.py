"""Character card repository."""

from backend.db.repositories.reference_card_repository import ReferenceCardRepository


character_repo = ReferenceCardRepository("characters", {"character"})
