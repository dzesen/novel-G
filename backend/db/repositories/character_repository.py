"""Character card repository."""

from backend.db.collections import CHARACTERS
from backend.db.repositories.reference_card_repository import ReferenceCardRepository


character_repo = ReferenceCardRepository(CHARACTERS, {"character"})
