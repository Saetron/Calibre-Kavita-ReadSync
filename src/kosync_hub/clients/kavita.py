"""Kavita client handling both the KOReader sync API and Kavita REST API."""

import logging
import re
from typing import Any, Dict, List, Optional
import httpx

from ..models import KavitaRecentRead, ProgressRecord
from .base import BaseSyncClient

logger = logging.getLogger("kosync_hub.kavita")


def extract_calibre_id(filename: Optional[str]) -> Optional[int]:
    """Extracts Calibre ID from curly brackets in a filename (e.g. 'Dune {42}.epub' -> 42)."""
    if not filename:
        return None
    match = re.search(r"\{(\d+)\}", filename)
    if match:
        try:
            return int(match.group(1))
        except ValueError:
            return None
    return None


class KavitaClient(BaseSyncClient):
    """Client for interacting with Kavita's KOReader endpoint and REST API."""

    def __init__(self, base_url: str, api_key: str, timeout: float = 15.0):
        self.raw_base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.koreader_url = f"{self.raw_base_url}/api/koreader/{self.api_key}"
        self.timeout = timeout
        self.jwt_token: Optional[str] = None

    @property
    def name(self) -> str:
        return "Kavita"

    def _get_koreader_headers(self) -> dict:
        return {
            "Accept": "application/vnd.koreader.v1+json",
            "Content-Type": "application/json",
            "User-Agent": "kosync-hub/1.0",
        }

    def _get_rest_headers(self) -> dict:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
            "User-Agent": "kosync-hub/1.0",
        }
        if self.jwt_token:
            headers["Authorization"] = f"Bearer {self.jwt_token}"
        return headers

    async def authenticate_rest_api(self) -> bool:
        """Authenticates with Kavita REST API via /api/Plugin/authenticate to obtain JWT."""
        if not self.api_key:
            return False

        url = f"{self.raw_base_url}/api/Plugin/authenticate"
        params = {"apiKey": self.api_key, "pluginName": "kosync-hub"}

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                res = await client.post(url, params=params)
                if res.status_code == 200:
                    data = res.json()
                    self.jwt_token = data.get("token")
                    logger.info("Successfully authenticated with Kavita REST API via plugin token.")
                    return True
                else:
                    logger.debug(
                        f"Kavita plugin auth status={res.status_code}. Will use x-api-key header directly."
                    )
                    return True
        except Exception as e:
            logger.warning(f"Plugin auth failed ({e}), continuing with x-api-key header.")
            return True

    async def test_connection(self) -> bool:
        """Tests authentication with Kavita using both /users/auth and REST API."""
        if not self.api_key:
            logger.warning("Kavita API key is not configured.")
            return False

        # 1. Test KOReader endpoint
        auth_url = f"{self.koreader_url}/users/auth"
        ko_ok = False
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                res = await client.get(auth_url, headers=self._get_koreader_headers())
                if res.status_code in (200, 201):
                    ko_ok = True
                else:
                    logger.warning(f"Kavita KOReader endpoint auth returned status {res.status_code}")
        except Exception as e:
            logger.error(f"Failed to connect to Kavita KOReader endpoint at {auth_url}: {e}")

        # 2. Authenticate REST API
        await self.authenticate_rest_api()
        return ko_ok

    async def get_progress(self, document: str) -> Optional[ProgressRecord]:
        """Pulls KOReader progress for document hash from Kavita."""
        url = f"{self.koreader_url}/syncs/progress/{document}"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                res = await client.get(url, headers=self._get_koreader_headers())
                if res.status_code == 200:
                    data = res.json()
                    return ProgressRecord(
                        document=data.get("document", document),
                        progress=str(data.get("progress", "")),
                        percentage=float(data.get("percentage", 0.0)),
                        timestamp=int(data.get("timestamp", 0)),
                        device=data.get("device", "Kavita"),
                    )
                return None
        except Exception as e:
            logger.error(f"Error fetching progress from Kavita for {document}: {e}")
            return None

    async def update_progress(self, record: ProgressRecord) -> bool:
        """Pushes reading progress to Kavita's KOReader sync endpoint."""
        url = f"{self.koreader_url}/syncs/progress"
        payload = {
            "document": record.document,
            "progress": record.progress,
            "percentage": record.percentage,
            "device": record.device or "Calibre",
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
                res = await client.put(url, json=payload, headers=self._get_koreader_headers())
                if res.status_code in (200, 201, 202):
                    logger.debug(f"Pushed progress to Kavita for {record.document} ({record.percentage})")
                    return True
                logger.warning(f"Kavita push progress returned status {res.status_code}: {res.text[:100]}")
                return False
        except Exception as e:
            logger.error(f"Exception pushing progress to Kavita for {record.document}: {e}")
            return False

    async def get_on_deck_reads(self, limit: int = 30) -> List[KavitaRecentRead]:
        """
        Retrieves recent 'On Deck' (in-progress) reading items from Kavita,
        extracting Calibre IDs from filenames formatted with {id}.
        """
        recent_reads: List[KavitaRecentRead] = []
        url = f"{self.raw_base_url}/api/series/on-deck"
        params = {"libraryId": 0, "pageNumber": 1, "pageSize": limit}

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                res = await client.post(url, params=params, headers=self._get_rest_headers())
                if res.status_code != 200:
                    logger.debug(f"Kavita on-deck status {res.status_code}: {res.text[:100]}")
                    return recent_reads

                series_list = res.json()
                if isinstance(series_list, dict) and "result" in series_list:
                    series_list = series_list["result"]

                if not isinstance(series_list, list):
                    return recent_reads

                for s in series_list:
                    series_id = s.get("id")
                    series_name = s.get("name", "Unknown")
                    if not series_id:
                        continue

                    # Fetch volume / chapter details for this series
                    vol_url = f"{self.raw_base_url}/api/series/volumes"
                    vol_res = await client.get(
                        vol_url,
                        params={"seriesId": series_id},
                        headers=self._get_rest_headers(),
                    )
                    if vol_res.status_code != 200:
                        continue

                    volumes = vol_res.json()
                    if not isinstance(volumes, list):
                        continue

                    for vol in volumes:
                        chapters = vol.get("chapters", [])
                        for ch in chapters:
                            # Chapter contains files list or filename
                            files = ch.get("files", [])
                            fn = ch.get("fileName") or (files[0].get("fileName") if files else "")
                            if not fn and files:
                                fn = files[0].get("filePath", "")

                            calibre_id = extract_calibre_id(fn)
                            pages_read = ch.get("pagesRead", 0)
                            total_pages = ch.get("pages", 0)
                            pct = (pages_read / total_pages) if total_pages > 0 else 0.0

                            if calibre_id is not None:
                                recent_reads.append(
                                    KavitaRecentRead(
                                        series_id=series_id,
                                        series_name=series_name,
                                        chapter_id=ch.get("id"),
                                        filename=fn,
                                        calibre_id=calibre_id,
                                        pages_read=pages_read,
                                        total_pages=total_pages,
                                        percentage=pct,
                                        last_read_utc=ch.get("lastReadingProgressUtc"),
                                    )
                                )

        except Exception as e:
            logger.error(f"Error fetching on-deck items from Kavita: {e}")

        logger.info(f"Retrieved {len(recent_reads)} on-deck read items with Calibre IDs from Kavita.")
        return recent_reads
