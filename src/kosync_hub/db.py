import logging
import os
import sqlite3
from pathlib import Path
from typing import List, Optional
from .models import ProgressRecord, SyncEvent

logger = logging.getLogger("kosync_hub.db")


class InternalDatabase:
    """Manages the internal state database of kosync-hub."""

    def __init__(self, db_path: str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=15.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._get_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tracked_documents (
                    document TEXT PRIMARY KEY,
                    title TEXT,
                    authors TEXT,
                    filename TEXT,
                    calibre_book_id INTEGER,
                    progress TEXT NOT NULL,
                    percentage REAL NOT NULL,
                    timestamp INTEGER NOT NULL,
                    device TEXT,
                    device_id TEXT,
                    kavita_synced_at INTEGER,
                    calibre_synced_at INTEGER,
                    last_sync_status TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sync_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    document TEXT NOT NULL,
                    calibre_id INTEGER,
                    source TEXT NOT NULL,
                    target TEXT NOT NULL,
                    progress TEXT NOT NULL,
                    percentage REAL NOT NULL,
                    timestamp INTEGER NOT NULL,
                    success INTEGER NOT NULL,
                    message TEXT
                )
            """)
            # Book metadata mapping cache (Calibre ID <-> Kavita IDs)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS book_mappings (
                    calibre_id INTEGER PRIMARY KEY,
                    kavita_series_id INTEGER,
                    kavita_series_name TEXT,
                    kavita_volume_id INTEGER,
                    kavita_chapter_id INTEGER,
                    kavita_library_id INTEGER,
                    pages INTEGER,
                    koreader_hash TEXT,
                    title TEXT,
                    authors TEXT,
                    filename TEXT,
                    updated_at INTEGER NOT NULL
                )
            """)
            # Alternative document hashes (e.g. compressed files on Xteink X3)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS document_aliases (
                    document TEXT PRIMARY KEY,
                    calibre_id INTEGER NOT NULL,
                    device TEXT,
                    created_at INTEGER NOT NULL
                )
            """)
            # Sync state tracking for conflict resolution
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sync_state (
                    calibre_id INTEGER PRIMARY KEY,
                    last_percentage REAL NOT NULL,
                    last_progress TEXT NOT NULL,
                    last_source TEXT,
                    last_synced_at INTEGER NOT NULL
                )
            """)
            try:
                conn.execute("ALTER TABLE sync_events ADD COLUMN calibre_id INTEGER")
            except sqlite3.OperationalError:
                pass

            conn.execute("CREATE INDEX IF NOT EXISTS idx_tracked_timestamp ON tracked_documents(timestamp)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_events_timestamp ON sync_events(timestamp DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_aliases_calibre ON document_aliases(calibre_id)")
            conn.commit()

    def upsert_progress(
        self,
        record: ProgressRecord,
        calibre_book_id: Optional[int] = None,
        kavita_synced_at: Optional[int] = None,
        calibre_synced_at: Optional[int] = None,
        sync_status: Optional[str] = None,
    ):
        """Inserts or updates reading progress for a document."""
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO tracked_documents (
                    document, title, authors, filename, calibre_book_id,
                    progress, percentage, timestamp, device, device_id,
                    kavita_synced_at, calibre_synced_at, last_sync_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(document) DO UPDATE SET
                    title = COALESCE(excluded.title, tracked_documents.title),
                    authors = COALESCE(excluded.authors, tracked_documents.authors),
                    filename = COALESCE(excluded.filename, tracked_documents.filename),
                    calibre_book_id = COALESCE(excluded.calibre_book_id, tracked_documents.calibre_book_id),
                    progress = excluded.progress,
                    percentage = excluded.percentage,
                    timestamp = excluded.timestamp,
                    device = excluded.device,
                    device_id = excluded.device_id,
                    kavita_synced_at = COALESCE(excluded.kavita_synced_at, tracked_documents.kavita_synced_at),
                    calibre_synced_at = COALESCE(excluded.calibre_synced_at, tracked_documents.calibre_synced_at),
                    last_sync_status = COALESCE(excluded.last_sync_status, tracked_documents.last_sync_status)
                """,
                (
                    record.document,
                    record.title,
                    record.authors,
                    record.filename,
                    calibre_book_id,
                    record.progress,
                    record.percentage,
                    record.timestamp,
                    record.device,
                    record.device_id,
                    kavita_synced_at,
                    calibre_synced_at,
                    sync_status,
                ),
            )
            conn.commit()

    def link_calibre_book_id(self, document: str, calibre_book_id: int):
        """Associates a KOReader document hash with a Calibre book ID."""
        with self._get_connection() as conn:
            conn.execute(
                "UPDATE tracked_documents SET calibre_book_id = ? WHERE document = ?",
                (calibre_book_id, document),
            )
            conn.commit()

    def get_document(self, document: str) -> Optional[ProgressRecord]:
        """Retrieves progress record for a specific document hash."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM tracked_documents WHERE document = ?",
                (document,),
            )
            row = cursor.fetchone()
            if not row:
                return None
            return ProgressRecord(
                document=row["document"],
                progress=row["progress"],
                percentage=row["percentage"],
                timestamp=row["timestamp"],
                device=row["device"],
                device_id=row["device_id"],
                title=row["title"],
                authors=row["authors"],
                filename=row["filename"],
            )

    def get_all_tracked_documents(self) -> List[sqlite3.Row]:
        """Returns all tracked documents."""
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT * FROM tracked_documents ORDER BY timestamp DESC")
            return cursor.fetchall()

    def log_sync_event(self, event: SyncEvent):
        """Logs a sync event into history."""
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO sync_events (
                    document, calibre_id, source, target, progress, percentage, timestamp, success, message
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.document,
                    event.calibre_id,
                    event.source,
                    event.target,
                    event.progress,
                    event.percentage,
                    event.timestamp,
                    1 if event.success else 0,
                    event.message,
                ),
            )
            conn.commit()

    def get_recent_events(self, limit: int = 50) -> List[sqlite3.Row]:
        """Returns recent sync events."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM sync_events ORDER BY timestamp DESC LIMIT ?",
                (limit,),
            )
            return cursor.fetchall()

    def count_tracked(self) -> int:
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT COUNT(*) FROM tracked_documents")
            return cursor.fetchone()[0]

    # -------------------------------------------------------------------------
    # Book Metadata Mappings (Calibre ID <-> Kavita IDs)
    # -------------------------------------------------------------------------

    def get_mapping_by_calibre_id(self, calibre_id: int) -> Optional[dict]:
        """Gets cached Kavita metadata for a Calibre book ID."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM book_mappings WHERE calibre_id = ?",
                (calibre_id,),
            )
            row = cursor.fetchone()
            if not row:
                return None
            return dict(row)

    def delete_mapping(self, calibre_id: int):
        """Removes cached Kavita metadata mapping for a Calibre book ID."""
        with self._get_connection() as conn:
            conn.execute("DELETE FROM book_mappings WHERE calibre_id = ?", (calibre_id,))
            conn.commit()

    def save_mapping(
        self,
        calibre_id: int,
        kavita_series_id: Optional[int] = None,
        kavita_series_name: Optional[str] = None,
        kavita_volume_id: Optional[int] = None,
        kavita_chapter_id: Optional[int] = None,
        kavita_library_id: Optional[int] = None,
        pages: Optional[int] = None,
        koreader_hash: Optional[str] = None,
        title: Optional[str] = None,
        authors: Optional[str] = None,
        filename: Optional[str] = None,
    ):
        """Caches resolved Kavita metadata mapping for a Calibre ID."""
        import time
        now = int(time.time())
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO book_mappings (
                    calibre_id, kavita_series_id, kavita_series_name, kavita_volume_id,
                    kavita_chapter_id, kavita_library_id, pages, koreader_hash,
                    title, authors, filename, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(calibre_id) DO UPDATE SET
                    kavita_series_id = COALESCE(excluded.kavita_series_id, book_mappings.kavita_series_id),
                    kavita_series_name = COALESCE(excluded.kavita_series_name, book_mappings.kavita_series_name),
                    kavita_volume_id = COALESCE(excluded.kavita_volume_id, book_mappings.kavita_volume_id),
                    kavita_chapter_id = COALESCE(excluded.kavita_chapter_id, book_mappings.kavita_chapter_id),
                    kavita_library_id = COALESCE(excluded.kavita_library_id, book_mappings.kavita_library_id),
                    pages = COALESCE(excluded.pages, book_mappings.pages),
                    koreader_hash = COALESCE(excluded.koreader_hash, book_mappings.koreader_hash),
                    title = COALESCE(excluded.title, book_mappings.title),
                    authors = COALESCE(excluded.authors, book_mappings.authors),
                    filename = COALESCE(excluded.filename, book_mappings.filename),
                    updated_at = excluded.updated_at
                """,
                (
                    calibre_id,
                    kavita_series_id,
                    kavita_series_name,
                    kavita_volume_id,
                    kavita_chapter_id,
                    kavita_library_id,
                    pages,
                    koreader_hash,
                    title,
                    authors,
                    filename,
                    now,
                ),
            )
            conn.commit()

    # -------------------------------------------------------------------------
    # Document Hash Aliases (e.g. CrossPoint compressed files)
    # -------------------------------------------------------------------------

    def link_document_alias(self, document: str, calibre_id: int, device: Optional[str] = None):
        """Associates an arbitrary document hash (e.g. from compressed e-reader files) with a Calibre ID."""
        import time
        now = int(time.time())
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO document_aliases (document, calibre_id, device, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(document) DO UPDATE SET
                    calibre_id = excluded.calibre_id,
                    device = COALESCE(excluded.device, document_aliases.device)
                """,
                (document, calibre_id, device, now),
            )
            conn.commit()

    def get_calibre_id_for_document(self, document: str) -> Optional[int]:
        """Resolves a document hash to its Calibre ID via document_aliases or tracked_documents."""
        with self._get_connection() as conn:
            # Check document_aliases first
            cursor = conn.execute(
                "SELECT calibre_id FROM document_aliases WHERE document = ?",
                (document,),
            )
            row = cursor.fetchone()
            if row and row["calibre_id"]:
                return row["calibre_id"]

            # Fallback to tracked_documents
            cursor = conn.execute(
                "SELECT calibre_book_id FROM tracked_documents WHERE document = ?",
                (document,),
            )
            row = cursor.fetchone()
            if row and row["calibre_book_id"]:
                return row["calibre_book_id"]

        return None

    def get_document_by_calibre_id(self, calibre_id: int) -> Optional[str]:
        """Returns the primary or recent document hash associated with a Calibre ID."""
        with self._get_connection() as conn:
            # Check tracked documents
            cursor = conn.execute(
                "SELECT document FROM tracked_documents WHERE calibre_book_id = ? ORDER BY timestamp DESC LIMIT 1",
                (calibre_id,),
            )
            row = cursor.fetchone()
            if row and row["document"]:
                return row["document"]

            # Check book mappings
            cursor = conn.execute(
                "SELECT koreader_hash FROM book_mappings WHERE calibre_id = ?",
                (calibre_id,),
            )
            row = cursor.fetchone()
            if row and row["koreader_hash"]:
                return row["koreader_hash"]

            # Check aliases
            cursor = conn.execute(
                "SELECT document FROM document_aliases WHERE calibre_id = ? ORDER BY created_at DESC LIMIT 1",
                (calibre_id,),
            )
            row = cursor.fetchone()
            if row and row["document"]:
                return row["document"]

        return None

    # -------------------------------------------------------------------------
    # Sync State Tracking (for conflict resolution)
    # -------------------------------------------------------------------------

    def get_sync_state(self, calibre_id: int) -> Optional[dict]:
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM sync_state WHERE calibre_id = ?",
                (calibre_id,),
            )
            row = cursor.fetchone()
            return dict(row) if row else None

    def update_sync_state(
        self,
        calibre_id: int,
        percentage: float,
        progress: str,
        source: str,
        synced_at: Optional[int] = None,
    ):
        import time
        now = synced_at or int(time.time())
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO sync_state (calibre_id, last_percentage, last_progress, last_source, last_synced_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(calibre_id) DO UPDATE SET
                    last_percentage = excluded.last_percentage,
                    last_progress = excluded.last_progress,
                    last_source = excluded.last_source,
                    last_synced_at = excluded.last_synced_at
                """,
                (calibre_id, percentage, progress, source, now),
            )
            conn.commit()

    # -------------------------------------------------------------------------
    # Paginated Views & Reading History Statistics
    # -------------------------------------------------------------------------

    def update_document_metadata(self, document: str, title: str, authors: Optional[str] = None):
        """Updates title and authors for an existing tracked document."""
        with self._get_connection() as conn:
            conn.execute(
                "UPDATE tracked_documents SET title = ?, authors = COALESCE(?, authors) WHERE document = ?",
                (title, authors, document),
            )
            conn.commit()

    def repair_missing_titles(self, calibre_get_book_by_id_fn) -> int:
        """
        Repairs any tracked_documents rows where title is NULL, empty, or 'Unknown'
        by querying Calibre for the book's title and authors.
        """
        repaired = 0
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT document, calibre_book_id FROM tracked_documents
                WHERE (title IS NULL OR title = '' OR title = 'Unknown')
                  AND calibre_book_id IS NOT NULL
                """
            )
            rows = cursor.fetchall()
            for r in rows:
                doc = r["document"]
                bid = r["calibre_book_id"]
                try:
                    book = calibre_get_book_by_id_fn(bid)
                    if book and book.title:
                        conn.execute(
                            """
                            UPDATE tracked_documents
                            SET title = ?, authors = COALESCE(?, authors)
                            WHERE document = ?
                            """,
                            (book.title, book.authors, doc),
                        )
                        repaired += 1
                except Exception as e:
                    logger.debug(f"Failed to repair title for #{bid}: {e}")
            conn.commit()
        if repaired > 0:
            logger.info(f"Successfully repaired {repaired} missing book titles from Calibre DB.")
        return repaired

    def get_paginated_documents(
        self,
        page: int = 1,
        page_size: int = 15,
        search: Optional[str] = None,
    ) -> tuple[List[sqlite3.Row], int]:
        """Returns paginated tracked documents and the total matching count."""
        page = max(1, page)
        offset = (page - 1) * page_size
        with self._get_connection() as conn:
            if search and search.strip():
                term = f"%{search.strip().lower()}%"
                count_cursor = conn.execute(
                    """
                    SELECT COUNT(*) FROM tracked_documents d
                    LEFT JOIN book_mappings m ON d.calibre_book_id = m.calibre_id
                    WHERE lower(COALESCE(NULLIF(NULLIF(d.title, 'Unknown'), ''), m.title, '')) LIKE ?
                       OR lower(COALESCE(NULLIF(d.authors, ''), m.authors, '')) LIKE ?
                       OR CAST(d.calibre_book_id AS TEXT) LIKE ?
                       OR lower(d.document) LIKE ?
                    """,
                    (term, term, term, term),
                )
                total = count_cursor.fetchone()[0]

                cursor = conn.execute(
                    """
                    SELECT 
                        d.document,
                        COALESCE(NULLIF(NULLIF(d.title, 'Unknown'), ''), m.title, m.kavita_series_name, 'Unknown') AS title,
                        COALESCE(NULLIF(d.authors, ''), m.authors, '') AS authors,
                        d.filename,
                        d.calibre_book_id,
                        d.progress,
                        d.percentage,
                        d.timestamp,
                        d.device,
                        d.device_id,
                        d.kavita_synced_at,
                        d.calibre_synced_at,
                        d.last_sync_status
                    FROM tracked_documents d
                    LEFT JOIN book_mappings m ON d.calibre_book_id = m.calibre_id
                    WHERE lower(COALESCE(NULLIF(NULLIF(d.title, 'Unknown'), ''), m.title, '')) LIKE ?
                       OR lower(COALESCE(NULLIF(d.authors, ''), m.authors, '')) LIKE ?
                       OR CAST(d.calibre_book_id AS TEXT) LIKE ?
                       OR lower(d.document) LIKE ?
                    ORDER BY d.timestamp DESC
                    LIMIT ? OFFSET ?
                    """,
                    (term, term, term, term, page_size, offset),
                )
                rows = cursor.fetchall()
            else:
                count_cursor = conn.execute("SELECT COUNT(*) FROM tracked_documents")
                total = count_cursor.fetchone()[0]

                cursor = conn.execute(
                    """
                    SELECT 
                        d.document,
                        COALESCE(NULLIF(NULLIF(d.title, 'Unknown'), ''), m.title, m.kavita_series_name, 'Unknown') AS title,
                        COALESCE(NULLIF(d.authors, ''), m.authors, '') AS authors,
                        d.filename,
                        d.calibre_book_id,
                        d.progress,
                        d.percentage,
                        d.timestamp,
                        d.device,
                        d.device_id,
                        d.kavita_synced_at,
                        d.calibre_synced_at,
                        d.last_sync_status
                    FROM tracked_documents d
                    LEFT JOIN book_mappings m ON d.calibre_book_id = m.calibre_id
                    ORDER BY d.timestamp DESC
                    LIMIT ? OFFSET ?
                    """,
                    (page_size, offset),
                )
                rows = cursor.fetchall()

            return rows, total

    def get_paginated_events(
        self,
        page: int = 1,
        page_size: int = 15,
    ) -> tuple[List[sqlite3.Row], int]:
        """Returns paginated sync events and total count."""
        page = max(1, page)
        offset = (page - 1) * page_size
        with self._get_connection() as conn:
            count_cursor = conn.execute("SELECT COUNT(*) FROM sync_events")
            total = count_cursor.fetchone()[0]

            cursor = conn.execute(
                "SELECT * FROM sync_events ORDER BY timestamp DESC LIMIT ? OFFSET ?",
                (page_size, offset),
            )
            rows = cursor.fetchall()
            return rows, total

    def get_reading_stats(self) -> dict:
        """Calculates reading history and synchronization statistics."""
        import time
        now = int(time.time())
        one_day_ago = now - (24 * 3600)
        one_week_ago = now - (7 * 24 * 3600)

        with self._get_connection() as conn:
            # Total tracked
            tracked_count = conn.execute("SELECT COUNT(*) FROM tracked_documents").fetchone()[0]

            # Completed books (>= 98%)
            completed_count = conn.execute(
                "SELECT COUNT(DISTINCT coalesce(calibre_book_id, document)) FROM tracked_documents WHERE percentage >= 0.98"
            ).fetchone()[0]

            # In progress books (> 0% and < 98%)
            in_progress_count = conn.execute(
                "SELECT COUNT(DISTINCT coalesce(calibre_book_id, document)) FROM tracked_documents WHERE percentage > 0.0 AND percentage < 0.98"
            ).fetchone()[0]

            # Sync events today
            syncs_today = conn.execute(
                "SELECT COUNT(*) FROM sync_events WHERE timestamp >= ?",
                (one_day_ago,),
            ).fetchone()[0]

            # Sync events this week
            syncs_week = conn.execute(
                "SELECT COUNT(*) FROM sync_events WHERE timestamp >= ?",
                (one_week_ago,),
            ).fetchone()[0]

            # Aliased devices count
            alias_count = conn.execute("SELECT COUNT(*) FROM document_aliases").fetchone()[0]

            return {
                "total_tracked": tracked_count,
                "completed_books": completed_count,
                "in_progress": in_progress_count,
                "syncs_today": syncs_today,
                "syncs_week": syncs_week,
                "aliases_count": alias_count,
            }
