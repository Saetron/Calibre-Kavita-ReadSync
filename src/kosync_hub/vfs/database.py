"""Internal SQLite database for tracking VFS entries, delta syncing, and indexed search."""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class VFSDatabase:
    """Thread-safe SQLite database for VFS cache and search."""

    def __init__(self, db_path: str):
        self.db_path = os.path.abspath(db_path)
        dir_name = os.path.dirname(self.db_path)
        if dir_name:
            try:
                os.makedirs(dir_name, exist_ok=True)
            except (OSError, PermissionError):
                # Fallback to current directory or temp if permission denied (e.g. running outside container)
                self.db_path = os.path.abspath(os.path.basename(db_path))
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS vfs_entries (
                    vfs_path TEXT PRIMARY KEY,
                    vfs_relpath TEXT,
                    source_path TEXT,
                    book_id INTEGER,
                    title TEXT,
                    series TEXT,
                    volume TEXT,
                    chapter TEXT,
                    type TEXT,
                    language TEXT,
                    format TEXT,
                    link_type TEXT,
                    target_source TEXT,
                    last_synced REAL
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_vfs_search ON vfs_entries (title, series, book_id, type)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_vfs_book_id ON vfs_entries (book_id)"
            )

    def get_tracked_map(self) -> Dict[str, Dict[str, Any]]:
        """Retrieve all currently tracked VFS paths and their sync metadata."""
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                "SELECT vfs_path, vfs_relpath, source_path, link_type, target_source FROM vfs_entries"
            )
            rows = cur.fetchall()
            return {
                row["vfs_path"]: {
                    "vfs_relpath": row["vfs_relpath"],
                    "source_path": row["source_path"],
                    "link_type": row["link_type"],
                    "target_source": row["target_source"],
                }
                for row in rows
            }

    def upsert_entries(self, entries: List[Dict[str, Any]]) -> None:
        """Insert or replace a batch of VFS entry records."""
        if not entries:
            return
        sql = """
            INSERT OR REPLACE INTO vfs_entries (
                vfs_path, vfs_relpath, source_path, book_id, title, series,
                volume, chapter, type, language, format, link_type, target_source, last_synced
            ) VALUES (
                :vfs_path, :vfs_relpath, :source_path, :book_id, :title, :series,
                :volume, :chapter, :type, :language, :format, :link_type, :target_source, :last_synced
            )
        """
        with self._lock, self._conn:
            self._conn.executemany(sql, entries)

    def delete_entries(self, vfs_paths: List[str]) -> None:
        """Delete entries matching the given VFS paths in batches."""
        if not vfs_paths:
            return
        batch_size = 500
        with self._lock, self._conn:
            for i in range(0, len(vfs_paths), batch_size):
                batch = vfs_paths[i : i + batch_size]
                placeholders = ",".join("?" for _ in batch)
                self._conn.execute(
                    f"DELETE FROM vfs_entries WHERE vfs_path IN ({placeholders})",
                    batch,
                )

    def query_items(
        self, query: str = "", limit: int = 50, offset: int = 0
    ) -> Dict[str, Any]:
        """Search and paginate VFS entries."""
        query_str = query.strip().lower()
        limit = max(1, min(limit, 1000))
        offset = max(0, offset)

        with self._lock:
            cur = self._conn.cursor()
            if query_str:
                pattern = f"%{query_str}%"
                where_clause = """
                    WHERE lower(title) LIKE ?
                       OR lower(series) LIKE ?
                       OR CAST(book_id AS TEXT) LIKE ?
                       OR lower(type) LIKE ?
                       OR lower(vfs_relpath) LIKE ?
                """
                params = (pattern, pattern, pattern, pattern, pattern)
                count_sql = f"SELECT COUNT(*) FROM vfs_entries {where_clause}"
                cur.execute(count_sql, params)
                total = cur.fetchone()[0]

                data_sql = f"""
                    SELECT book_id, title, series, language, type, volume, chapter, format, source_path, vfs_path, vfs_relpath
                    FROM vfs_entries
                    {where_clause}
                    ORDER BY book_id ASC, vfs_relpath ASC
                    LIMIT ? OFFSET ?
                """
                cur.execute(data_sql, params + (limit, offset))
            else:
                cur.execute("SELECT COUNT(*) FROM vfs_entries")
                total = cur.fetchone()[0]

                cur.execute(
                    """
                    SELECT book_id, title, series, language, type, volume, chapter, format, source_path, vfs_path, vfs_relpath
                    FROM vfs_entries
                    ORDER BY book_id ASC, vfs_relpath ASC
                    LIMIT ? OFFSET ?
                    """,
                    (limit, offset),
                )

            rows = cur.fetchall()
            items = [
                {
                    "book_id": r["book_id"],
                    "title": r["title"] or "",
                    "series": r["series"] or "",
                    "language": r["language"] or "",
                    "type": r["type"] or "",
                    "volume": r["volume"] or "",
                    "chapter": r["chapter"] or "",
                    "format": r["format"] or "",
                    "source_path": r["source_path"] or "",
                    "vfs_path": r["vfs_path"] or "",
                    "vfs_relpath": r["vfs_relpath"] or "",
                }
                for r in rows
            ]

            return {
                "total": total,
                "offset": offset,
                "limit": limit,
                "items": items,
            }

    def get_summary_stats(self) -> Dict[str, Any]:
        """Compute aggregate summary stats from database entries."""
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("SELECT COUNT(*), COUNT(DISTINCT series) FROM vfs_entries")
            row = cur.fetchone()
            total_books = row[0] if row else 0
            total_series = row[1] if row else 0

            cur.execute("SELECT type, COUNT(*) FROM vfs_entries GROUP BY type")
            type_counts = {r[0] or "Unknown": r[1] for r in cur.fetchall()}

            cur.execute("SELECT language, COUNT(*) FROM vfs_entries GROUP BY language")
            language_counts = {r[0] or "unknown": r[1] for r in cur.fetchall()}

            cur.execute("SELECT volume, chapter FROM vfs_entries")
            rows = cur.fetchall()
            vol_sum = 0.0
            ch_sum = 0.0
            books_with_vol = 0
            books_with_ch = 0
            for r in rows:
                v = r["volume"]
                c = r["chapter"]
                if v and str(v).strip():
                    books_with_vol += 1
                    try:
                        val_str = str(v).strip().lower().replace("vol.", "").replace("vol", "").replace("v", "").strip()
                        if "-" in val_str:
                            vol_sum += float(val_str.split("-")[-1].strip())
                        else:
                            vol_sum += float(val_str)
                    except (ValueError, IndexError):
                        vol_sum += 1.0

                if c and str(c).strip():
                    books_with_ch += 1
                    try:
                        val_str = str(c).strip().lower().replace("ch.", "").replace("ch", "").replace("c", "").strip()
                        if "-" in val_str:
                            ch_sum += float(val_str.split("-")[-1].strip())
                        else:
                            ch_sum += float(val_str)
                    except (ValueError, IndexError):
                        ch_sum += 1.0

            return {
                "total_books": total_books,
                "total_series": total_series,
                "total_volumes": vol_sum,
                "total_chapters": ch_sum,
                "books_with_volume": books_with_vol,
                "books_with_chapter": books_with_ch,
                "type_counts": type_counts,
                "language_counts": language_counts,
            }

    def clear(self) -> None:
        """Clear all entries from the database."""
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM vfs_entries")

    def close(self) -> None:
        """Close the database connection."""
        with self._lock:
            self._conn.close()
