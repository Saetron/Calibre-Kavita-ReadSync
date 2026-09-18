"""Generic KOSync protocol client (for Calibre-Web-Automated or external KOSync servers)."""

import hashlib
import logging
from typing import Optional
import httpx
from ..models import ProgressRecord
from .base import BaseSyncClient

logger = logging.getLogger("kosync_hub.kosync")


class GenericKOSyncClient(BaseSyncClient):
    """Client for standard KOReader sync servers (e.g. CWA /kosync or sync.koreader.rocks)."""

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        prefix: str = "",
        timeout: float = 10.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.prefix = f"/{prefix.strip('/')}" if prefix.strip("/") else ""
        self.username = username
        self.password = password
        # KOSync sends md5 of password in x-auth-key header
        self.userkey = hashlib.md5(password.encode("utf-8")).hexdigest()
        self.timeout = timeout

    @property
    def name(self) -> str:
        return f"KOSync ({self.base_url})"

    def _get_headers(self) -> dict:
        return {
            "Accept": "application/vnd.koreader.v1+json",
            "Content-Type": "application/json",
            "x-auth-user": self.username,
            "x-auth-key": self.userkey,
            "User-Agent": "kosync-hub/1.0",
        }

    async def test_connection(self) -> bool:
        """Tests authentication with GET /users/auth."""
        url = f"{self.base_url}{self.prefix}/users/auth"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                res = await client.get(url, headers=self._get_headers())
                if res.status_code == 200:
                    logger.info(f"KOSync connection to {self.base_url} successful.")
                    return True
                logger.warning(f"KOSync auth failed: status={res.status_code}, text={res.text[:100]}")
                return False
        except Exception as e:
            logger.error(f"Error connecting to KOSync at {url}: {e}")
            return False

    async def get_progress(self, document: str) -> Optional[ProgressRecord]:
        """Pulls progress from GET /syncs/progress/:document."""
        url = f"{self.base_url}{self.prefix}/syncs/progress/{document}"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                res = await client.get(url, headers=self._get_headers())
                if res.status_code == 200:
                    data = res.json()
                    return ProgressRecord(
                        document=data.get("document", document),
                        progress=str(data.get("progress", "")),
                        percentage=float(data.get("percentage", 0.0)),
                        timestamp=int(data.get("timestamp", 0)),
                        device=data.get("device", "KOSync"),
                    )
                return None
        except Exception as e:
            logger.error(f"Error getting progress from KOSync: {e}")
            return None

    async def update_progress(self, record: ProgressRecord) -> bool:
        """Pushes progress to PUT /syncs/progress."""
        url = f"{self.base_url}{self.prefix}/syncs/progress"
        payload = {
            "document": record.document,
            "progress": record.progress,
            "percentage": record.percentage,
            "device": record.device or "KOReader",
            "device_id": record.device_id or "kosync-hub",
        }
        if record.title or record.authors or record.filename:
            payload["metadata"] = {
                "title": record.title,
                "authors": record.authors,
                "filename": record.filename,
            }

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                res = await client.put(url, json=payload, headers=self._get_headers())
                return res.status_code in (200, 201, 202)
        except Exception as e:
            logger.error(f"Error updating progress on KOSync: {e}")
            return False
