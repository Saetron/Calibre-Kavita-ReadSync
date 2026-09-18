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

    async def ensure_authenticated(self) -> bool:
        """Ensures that REST API authentication has been performed."""
        if self.jwt_token:
            return True
        return await self.authenticate_rest_api()

    async def get_on_deck_reads(self, limit: int = 50) -> List[KavitaRecentRead]:
        """
        Retrieves recent reading items from Kavita using multiple discovery strategies:
        1. /api/Series/on-deck (POST)
        2. /api/Series/currently-reading (GET)
        3. /api/Series/recently-updated-series (POST)
        4. /api/Opds/{apiKey}/on-deck (GET) fallback

        Extracts Calibre IDs from {id} in file paths, titles, or series names,
        and checks Kavita's KOReader sync progress for each file hash.
        """
        await self.ensure_authenticated()

        recent_reads: List[KavitaRecentRead] = []
        seen_calibre_ids = set()

        # 1. Fetch series from REST endpoints
        series_items = await self._fetch_recent_series_from_rest(limit=limit)

        # 2. If REST returned empty, try OPDS fallback
        if not series_items:
            logger.info("REST series queries returned 0 items. Trying OPDS on-deck feed...")
            opds_reads = await self._fetch_recent_from_opds()
            for r in opds_reads:
                if r.calibre_id not in seen_calibre_ids:
                    seen_calibre_ids.add(r.calibre_id)
                    recent_reads.append(r)

        # Process REST series items
        for s in series_items:
            series_id = s.get("seriesId") or s.get("id")
            series_name = s.get("seriesName") or s.get("name", "Unknown")
            folder_path = s.get("folderPath") or s.get("lowestFolderPath") or ""

            if not series_id:
                continue

            # Check if series name or folder contains Calibre ID
            series_calibre_id = extract_calibre_id(folder_path) or extract_calibre_id(series_name)

            # Fetch volume / chapter details for this series
            volumes = await self._fetch_series_volumes(series_id)
            if not volumes:
                continue

            for vol in volumes:
                vol_name = vol.get("name") or ""
                vol_calibre_id = extract_calibre_id(vol_name) or series_calibre_id

                chapters = vol.get("chapters", [])
                for ch in chapters:
                    files = ch.get("files", [])
                    ch_title = ch.get("title") or ch.get("titleName") or ""

                    # Check each file in the chapter
                    candidates = []
                    if files:
                        for f in files:
                            fp = f.get("filePath") or f.get("fileName") or ""
                            candidates.append((fp, f.get("koreaderHash")))
                    else:
                        candidates.append((ch.get("fileName") or ch_title, None))

                    pages_read = ch.get("pagesRead", 0)
                    total_pages = ch.get("pages", 0)
                    pct = (pages_read / total_pages) if total_pages > 0 else 0.0

                    for candidate_fn, koreader_hash in candidates:
                        calibre_id = (
                            extract_calibre_id(candidate_fn)
                            or extract_calibre_id(ch_title)
                            or vol_calibre_id
                        )

                        if calibre_id is None:
                            continue

                        # If Kavita file has a KOReader hash, check KOReader progress endpoint
                        if koreader_hash:
                            ko_rec = await self.get_progress(koreader_hash)
                            if ko_rec and ko_rec.percentage > pct:
                                pct = ko_rec.percentage
                                logger.debug(
                                    f"Using newer KOReader sync progress for hash {koreader_hash}: "
                                    f"{round(pct * 100, 1)}%"
                                )

                        if calibre_id not in seen_calibre_ids:
                            seen_calibre_ids.add(calibre_id)
                            item = KavitaRecentRead(
                                series_id=series_id,
                                series_name=series_name,
                                chapter_id=ch.get("id"),
                                filename=candidate_fn or series_name,
                                calibre_id=calibre_id,
                                pages_read=pages_read,
                                total_pages=total_pages,
                                percentage=pct,
                                last_read_utc=ch.get("lastReadingProgressUtc") or ch.get("lastReadingProgress"),
                            )
                            recent_reads.append(item)
                            logger.info(
                                f"Found recent read in Kavita: '{series_name}' "
                                f"[Calibre ID: #{calibre_id}, Progress: {round(pct * 100, 1)}%]"
                            )

        # 2. If no matching items found from REST, try OPDS fallback
        if not recent_reads:
            logger.info("No recent reads found via REST series. Trying OPDS on-deck feed...")
            opds_reads = await self._fetch_recent_from_opds()
            for r in opds_reads:
                if r.calibre_id not in seen_calibre_ids:
                    seen_calibre_ids.add(r.calibre_id)
                    recent_reads.append(r)

        logger.info(f"Retrieved {len(recent_reads)} recent read items with Calibre IDs from Kavita.")
        return recent_reads

    async def _fetch_recent_series_from_rest(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Queries multiple Kavita REST endpoints to collect recent or currently reading series."""
        headers = self._get_rest_headers()

        all_series: List[Dict[str, Any]] = []
        seen_ids = set()

        endpoints = [
            (
                "POST",
                f"{self.raw_base_url}/api/Series/on-deck",
                "On-Deck (POST)",
                {"PageNumber": 1, "pageNumber": 1, "PageSize": limit, "pageSize": limit, "libraryId": 0, "LibraryId": 0},
            ),
            (
                "GET",
                f"{self.raw_base_url}/api/Series/currently-reading",
                "Currently Reading (GET)",
                {"PageNumber": 1, "pageNumber": 1, "PageSize": limit, "pageSize": limit},
            ),
            (
                "POST",
                f"{self.raw_base_url}/api/Series/recently-updated-series",
                "Recently Updated (POST)",
                {"PageNumber": 1, "pageNumber": 1, "PageSize": limit, "pageSize": limit},
            ),
        ]

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                for method, url, label, params in endpoints:
                    try:
                        if method == "POST":
                            res = await client.post(url, params=params, headers=headers)
                        else:
                            res = await client.get(url, params=params, headers=headers)

                        if res.status_code == 200:
                            data = res.json()
                            if isinstance(data, dict) and "result" in data:
                                data = data["result"]
                            if isinstance(data, list):
                                logger.info(f"Kavita {label} returned {len(data)} items.")
                                for item in data:
                                    sid = item.get("seriesId") or item.get("id")
                                    sname = item.get("seriesName") or item.get("name")
                                    if sid and sid not in seen_ids:
                                        seen_ids.add(sid)
                                        item["id"] = sid
                                        if sname:
                                            item["name"] = sname
                                        all_series.append(item)
                        else:
                            logger.warning(
                                f"Kavita {label} endpoint returned status {res.status_code} "
                                f"({res.text[:120] if res.text else 'empty body'})"
                            )
                    except Exception as e:
                        logger.warning(f"Error querying Kavita {label}: {e}")

        except Exception as e:
            logger.error(f"Error in REST series discovery: {e}")

        return all_series

    async def _fetch_series_volumes(self, series_id: int) -> List[Dict[str, Any]]:
        """Fetches volumes and chapters for a series."""
        url = f"{self.raw_base_url}/api/Series/volumes"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                res = await client.get(
                    url,
                    params={"seriesId": series_id},
                    headers=self._get_rest_headers(),
                )
                if res.status_code == 200:
                    data = res.json()
                    if isinstance(data, list):
                        return data
                else:
                    logger.debug(f"Volumes query for series {series_id} returned status {res.status_code}")
        except Exception as e:
            logger.warning(f"Error fetching volumes for series {series_id}: {e}")
        return []

    async def _fetch_recent_from_opds(self) -> List[KavitaRecentRead]:
        """Queries Kavita's OPDS On-Deck feed as a zero-token fallback."""
        opds_reads: List[KavitaRecentRead] = []
        url = f"{self.raw_base_url}/api/Opds/{self.api_key}/on-deck"

        try:
            import xml.etree.ElementTree as ET

            async with httpx.AsyncClient(timeout=self.timeout) as client:
                res = await client.get(url, headers={"Accept": "application/atom+xml, application/xml"})
                if res.status_code != 200:
                    logger.debug(f"OPDS on-deck query returned status {res.status_code}")
                    return opds_reads

                root = ET.fromstring(res.text)
                # Atom namespace is typically http://www.w3.org/2005/Atom
                ns = {"atom": "http://www.w3.org/2005/Atom"}

                for entry in root.findall("atom:entry", ns):
                    title_elem = entry.find("atom:title", ns)
                    title = title_elem.text if title_elem is not None and title_elem.text else ""

                    # Check links for download or stream URLs containing filenames
                    filename = title
                    for link in entry.findall("atom:link", ns):
                        href = link.attrib.get("href", "")
                        title_attr = link.attrib.get("title", "")
                        if extract_calibre_id(href):
                            filename = href
                            break
                        if extract_calibre_id(title_attr):
                            filename = title_attr
                            break

                    calibre_id = extract_calibre_id(filename) or extract_calibre_id(title)
                    if calibre_id:
                        opds_reads.append(
                            KavitaRecentRead(
                                series_id=0,
                                series_name=title,
                                chapter_id=None,
                                filename=filename,
                                calibre_id=calibre_id,
                                pages_read=1,
                                total_pages=1,
                                percentage=0.0,
                            )
                        )
                        logger.info(f"Discovered book in OPDS On-Deck feed: '{title}' [Calibre ID #{calibre_id}]")

        except Exception as e:
            logger.warning(f"Error reading Kavita OPDS on-deck feed: {e}")

        return opds_reads
