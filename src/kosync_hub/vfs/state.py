"""VFS state management and statistics calculation.

Calculates aggregated chapter and volume counts, tracks collisions,
and maintains thread-safe state for the WebUI and API.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_RANGE_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)$")


def calculate_units(val: Any) -> float:
    """Calculate the number of units represented by a volume or chapter value.

    Examples:
    - 1 -> 1.0
    - "1-3" -> 3.0 (i.e. 3 - 1 + 1)
    - "1-25" -> 25.0
    - "10.5" -> 1.0
    - "Special" -> 1.0
    - None or "" -> 0.0
    """
    if val is None:
        return 0.0

    if isinstance(val, (int, float)):
        return 1.0

    s = str(val).strip()
    if not s:
        return 0.0

    # Strip any accidental prefix like "Vol." or "Ch."
    s_clean = re.sub(r"^(?:vol(?:ume)?|ch(?:apter)?|v|c)\.?\s*", "", s, flags=re.IGNORECASE).strip()

    # Check for range: e.g. "1-3" or "10 - 20"
    m = _RANGE_RE.match(s_clean)
    if m:
        try:
            start = float(m.group(1))
            end = float(m.group(2))
            if end >= start:
                return end - start + 1.0
            return 1.0
        except ValueError:
            return 1.0

    # Single number or label
    try:
        float(s_clean)
        return 1.0
    except ValueError:
        return 1.0 if s_clean else 0.0


class VFSState:
    def __init__(self, db: Optional[Any] = None):
        self._lock = threading.Lock()
        self.db = db
        self.last_sync_time: Optional[float] = None
        self.sync_in_progress: bool = False
        self.last_error: Optional[str] = None
        self.mode: str = "hardlink"
        self.calibre_dir: str = ""
        self.vfs_dir: str = ""

        # Aggregate stats
        self.total_books: int = 0
        self.total_series: int = 0
        self.total_volumes: float = 0.0
        self.total_chapters: float = 0.0
        self.books_with_volume: int = 0
        self.books_with_chapter: int = 0

        # Distributions
        self.type_counts: Dict[str, int] = {}
        self.language_counts: Dict[str, int] = {}

        # Collisions and item records
        self.collisions: List[Dict[str, Any]] = []
        self.items: List[Dict[str, Any]] = []

    def set_db(self, db: Any) -> None:
        with self._lock:
            self.db = db
            if hasattr(db, "get_summary_stats"):
                try:
                    stats = db.get_summary_stats()
                    if stats.get("total_books", 0) > 0 and self.total_books == 0:
                        self.total_books = stats["total_books"]
                        self.total_series = stats["total_series"]
                        self.total_volumes = stats["total_volumes"]
                        self.total_chapters = stats["total_chapters"]
                        self.books_with_volume = stats["books_with_volume"]
                        self.books_with_chapter = stats["books_with_chapter"]
                        self.type_counts = dict(stats["type_counts"])
                        self.language_counts = dict(stats["language_counts"])
                except Exception as e:
                    logger.debug("Could not read initial stats from database: %s", e)

    def set_syncing(self, syncing: bool, error: Optional[str] = None) -> None:
        with self._lock:
            self.sync_in_progress = syncing
            if error:
                self.last_error = error

    def update_sync_results(
        self,
        records: List[Any],
        desired_map: Dict[str, str],
        collisions: List[Dict[str, Any]],
        mode: str,
        calibre_dir: str,
        vfs_dir: str,
    ) -> None:
        with self._lock:
            self.last_sync_time = time.time()
            self.sync_in_progress = False
            self.last_error = None
            self.mode = mode
            self.calibre_dir = calibre_dir
            self.vfs_dir = vfs_dir
            self.collisions = list(collisions)

            self.total_books = len(records)
            series_set = set()
            type_map: Dict[str, int] = {}
            lang_map: Dict[str, int] = {}

            vol_sum = 0.0
            ch_sum = 0.0
            books_with_vol = 0
            books_with_ch = 0

            items = []
            target_to_vfs = {src: tgt for tgt, src in desired_map.items()} if not self.db else {}

            for rec in records:
                series_name = rec.series or rec.title or "Unknown"
                series_set.add(series_name)

                t_name = rec.type_ or "Unknown"
                type_map[t_name] = type_map.get(t_name, 0) + 1

                l_name = rec.language or "unknown"
                lang_map[l_name] = lang_map.get(l_name, 0) + 1

                vol_units = calculate_units(rec.volume)
                ch_units = calculate_units(rec.chapter)

                if rec.volume is not None and str(rec.volume).strip():
                    books_with_vol += 1
                    vol_sum += vol_units

                if rec.chapter is not None and str(rec.chapter).strip():
                    books_with_ch += 1
                    ch_sum += ch_units

                if not self.db:
                    vfs_path = target_to_vfs.get(rec.source_path, "")
                    items.append({
                        "book_id": rec.book_id,
                        "title": rec.title,
                        "series": rec.series or "",
                        "language": rec.language or "",
                        "type": rec.type_ or "",
                        "volume": str(rec.volume) if rec.volume is not None else "",
                        "chapter": str(rec.chapter) if rec.chapter is not None else "",
                        "format": rec.format,
                        "source_path": rec.source_path,
                        "vfs_path": vfs_path,
                    })

            self.total_series = len(series_set)
            self.total_volumes = vol_sum
            self.total_chapters = ch_sum
            self.books_with_volume = books_with_vol
            self.books_with_chapter = books_with_ch
            self.type_counts = type_map
            self.language_counts = lang_map
            self.items = items

    def get_summary(self) -> Dict[str, Any]:
        with self._lock:
            clean_vols = int(self.total_volumes) if isinstance(self.total_volumes, float) and self.total_volumes.is_integer() else (int(self.total_volumes) if isinstance(self.total_volumes, int) else round(self.total_volumes, 1))
            clean_chs = int(self.total_chapters) if isinstance(self.total_chapters, float) and self.total_chapters.is_integer() else (int(self.total_chapters) if isinstance(self.total_chapters, int) else round(self.total_chapters, 1))

            return {
                "last_sync_time": self.last_sync_time,
                "sync_in_progress": self.sync_in_progress,
                "last_error": self.last_error,
                "mode": self.mode,
                "calibre_dir": self.calibre_dir,
                "vfs_dir": self.vfs_dir,
                "total_books": self.total_books,
                "total_series": self.total_series,
                "total_volumes": clean_vols,
                "total_chapters": clean_chs,
                "books_with_volume": self.books_with_volume,
                "books_with_chapter": self.books_with_chapter,
                "type_counts": dict(self.type_counts),
                "language_counts": dict(self.language_counts),
                "collision_count": len(self.collisions),
                "collisions": list(self.collisions),
            }

    def get_items(self, query: str = "", limit: int = 100, offset: int = 0) -> Dict[str, Any]:
        with self._lock:
            if self.db is not None:
                return self.db.query_items(query=query, limit=limit, offset=offset)

            filtered = self.items
            if query:
                q = query.lower()
                filtered = [
                    item for item in self.items
                    if q in item["title"].lower()
                    or q in item["series"].lower()
                    or q in str(item["book_id"])
                    or q in item["type"].lower()
                    or q in item["vfs_path"].lower()
                ]

            total_filtered = len(filtered)
            paginated = filtered[offset : offset + limit]

            return {
                "total": total_filtered,
                "offset": offset,
                "limit": limit,
                "items": paginated,
            }


# Default singleton instance
state = VFSState()
