"""Base interface for sync providers."""

from abc import ABC, abstractmethod
from typing import Optional
from ..models import ProgressRecord


class BaseSyncClient(ABC):
    """Abstract interface representing a sync endpoint/target."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Name of the provider (e.g. 'Kavita', 'Calibre DB')."""
        pass

    @abstractmethod
    async def test_connection(self) -> bool:
        """Tests if the provider is reachable and credentials are valid."""
        pass

    @abstractmethod
    async def get_progress(self, document: str) -> Optional[ProgressRecord]:
        """Retrieves reading progress for a given document hash."""
        pass

    @abstractmethod
    async def update_progress(self, record: ProgressRecord) -> bool:
        """Pushes reading progress to the provider."""
        pass
