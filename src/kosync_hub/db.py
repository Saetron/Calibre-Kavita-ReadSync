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

        # Merge any existing duplicate entries sharing the same Calibre ID
        self.merge_duplicate_calibre_entries()

        with self._get_connection() as conn:
            try:
                conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_tracked_calibre_unique ON tracked_documents(calibre_book_id) WHERE calibre_book_id IS NOT NULL")
                conn.commit()
            except sqlite3.OperationalError:
                pass

    def merge_duplicate_calibre_entries(self) -> int:
        """
        Finds any tracked_documents with duplicate calibre_book_id and merges them into a single row,
        preserving the highest progress, valid metadata, and storing alternative hashes in document_aliases.
        """
        import time
        now_ts = int(time.time())
        merged_count = 0

        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT calibre_book_id
                FROM tracked_documents
                WHERE calibre_book_id IS NOT NULL
                GROUP BY calibre_book_id
                HAVING COUNT(*) > 1
                """
            )
            duplicate_ids = [row["calibre_book_id"] for row in cursor.fetchall()]

            for cal_id in duplicate_ids:
                cur = conn.execute(
                    "SELECT * FROM tracked_documents WHERE calibre_book_id = ? ORDER BY timestamp DESC",
                    (cal_id,),
                )
                rows = [dict(r) for r in cur.fetchall()]
                if len(rows) <= 1:
                    continue

                # Find canonical KOReader hash if known in book_mappings
                cur_map = conn.execute(
                    "SELECT koreader_hash FROM book_mappings WHERE calibre_id = ?",
                    (cal_id,),
                )
                map_row = cur_map.fetchone()
                canonical_hash = map_row["koreader_hash"] if map_row and map_row["koreader_hash"] else None

                winner = None
                if canonical_hash:
                    for r in rows:
                        if r["document"] == canonical_hash:
                            winner = r
                            break

                # Otherwise select row with highest percentage, then latest timestamp
                if not winner:
                    winner = max(
                        rows,
                        key=lambda r: (float(r.get("percentage") or 0.0), int(r.get("timestamp") or 0)),
                    )

                # Highest percentage and its progress string
                best_pct = max(float(r.get("percentage") or 0.0) for r in rows)
                best_pct_row = max(
                    (r for r in rows if float(r.get("percentage") or 0.0) == best_pct),
                    key=lambda r: int(r.get("timestamp") or 0),
                )

                # Metadata: choose non-empty title/author/filename
                best_title = next(
                    (r["title"] for r in rows if r.get("title") and r["title"] != "Unknown"),
                    winner.get("title"),
                )
                best_authors = next((r["authors"] for r in rows if r.get("authors")), winner.get("authors"))
                best_filename = next((r["filename"] for r in rows if r.get("filename")), winner.get("filename"))

                # Sync timestamps and status
                best_kavita_sync = max((int(r.get("kavita_synced_at") or 0) for r in rows), default=None) or None
                best_calibre_sync = max((int(r.get("calibre_synced_at") or 0) for r in rows), default=None) or None
                statuses = [r.get("last_sync_status") for r in rows if r.get("last_sync_status")]
                best_status = (
                    "synced_to_kavita"
                    if "synced_to_kavita" in statuses
                    else (statuses[0] if statuses else "synced")
                )

                # Register all hashes into document_aliases
                for r in rows:
                    doc_hash = r["document"]
                    conn.execute(
                        """
                        INSERT INTO document_aliases (document, calibre_id, device, created_at)
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT(document) DO UPDATE SET calibre_id = excluded.calibre_id
                        """,
                        (doc_hash, cal_id, r.get("device"), now_ts),
                    )

                # Delete all duplicate rows for this cal_id
                conn.execute("DELETE FROM tracked_documents WHERE calibre_book_id = ?", (cal_id,))

                # Insert the unified record
                conn.execute(
                    """
                    INSERT INTO tracked_documents (
                        document, title, authors, filename, calibre_book_id,
                        progress, percentage, timestamp, device, device_id,
                        kavita_synced_at, calibre_synced_at, last_sync_status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        winner["document"],
                        best_title,
                        best_authors,
                        best_filename,
                        cal_id,
                        best_pct_row.get("progress") or winner.get("progress"),
                        best_pct,
                        max(int(r.get("timestamp") or 0) for r in rows),
                        best_pct_row.get("device") or winner.get("device"),
                        best_pct_row.get("device_id") or winner.get("device_id"),
                        best_kavita_sync,
                        best_calibre_sync,
                        best_status,
                    ),
                )
                merged_count += len(rows) - 1

            conn.commit()

        if merged_count > 0:
            logger.info(f"Merged {merged_count} duplicate document entries sharing the same Calibre ID.")
        return merged_count

    def upsert_progress(
        self,
        record: ProgressRecord,
        calibre_book_id: Optional[int] = None,
        kavita_synced_at: Optional[int] = None,
        calibre_synced_at: Optional[int] = None,
        sync_status: Optional[str] = None,
    ):
        """
        Inserts or updates reading progress for a document.
        Guarantees that multiple document hashes for the same Calibre book ID
        are merged into a single unified entry in tracked_documents.
        """
        import time
        now_ts = int(time.time())
        cal_id = calibre_book_id or record.calibre_id
        if not cal_id and record.document:
            cal_id = self.get_calibre_id_for_document(record.document)

        with self._get_connection() as conn:
            # 1. If we have a Calibre ID, ensure this document hash is linked in document_aliases
            if cal_id and record.document:
                conn.execute(
                    """
                    INSERT INTO document_aliases (document, calibre_id, device, created_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(document) DO UPDATE SET calibre_id = excluded.calibre_id
                    """,
                    (record.document, cal_id, record.device, now_ts),
                )

            # 2. Check if a row already exists in tracked_documents for this Calibre ID
            existing_row = None
            if cal_id:
                cursor = conn.execute(
                    "SELECT * FROM tracked_documents WHERE calibre_book_id = ?",
                    (cal_id,),
                )
                existing_row = cursor.fetchone()

            # 3. If not found by calibre_id, check by document hash
            if not existing_row and record.document:
                cursor = conn.execute(
                    "SELECT * FROM tracked_documents WHERE document = ?",
                    (record.document,),
                )
                existing_row = cursor.fetchone()

            if existing_row:
                existing_pct = float(existing_row["percentage"] or 0.0)
                existing_prog = existing_row["progress"] or ""
                existing_ts = int(existing_row["timestamp"] or 0)
                existing_device = existing_row["device"]
                existing_device_id = existing_row["device_id"]
                target_doc = existing_row["document"]

                # Ensure existing document is also recorded in document_aliases
                if cal_id and target_doc:
                    conn.execute(
                        """
                        INSERT INTO document_aliases (document, calibre_id, device, created_at)
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT(document) DO UPDATE SET calibre_id = excluded.calibre_id
                        """,
                        (target_doc, cal_id, existing_device, now_ts),
                    )

                # Determine winning progress & percentage
                if record.percentage > existing_pct:
                    use_pct = record.percentage
                    use_prog = record.progress
                    use_device = record.device or existing_device
                    use_device_id = record.device_id or existing_device_id
                    use_ts = record.timestamp
                elif record.percentage == 0.0 and existing_pct > 0.0:
                    # Do not overwrite read progress with 0% (e.g. freshly opened book)
                    use_pct = existing_pct
                    use_prog = existing_prog
                    use_device = existing_device
                    use_device_id = existing_device_id
                    use_ts = max(record.timestamp, existing_ts)
                elif record.timestamp >= existing_ts:
                    use_pct = record.percentage
                    use_prog = record.progress
                    use_device = record.device or existing_device
                    use_device_id = record.device_id or existing_device_id
                    use_ts = record.timestamp
                else:
                    use_pct = existing_pct
                    use_prog = existing_prog
                    use_device = existing_device
                    use_device_id = existing_device_id
                    use_ts = existing_ts

                # Determine winning metadata
                title = record.title or existing_row["title"]
                authors = record.authors or existing_row["authors"]
                filename = record.filename or existing_row["filename"]
                final_cal_id = cal_id or existing_row["calibre_book_id"]

                # Update the existing row
                conn.execute(
                    """
                    UPDATE tracked_documents SET
                        title = COALESCE(?, title),
                        authors = COALESCE(?, authors),
                        filename = COALESCE(?, filename),
                        calibre_book_id = COALESCE(?, calibre_book_id),
                        progress = ?,
                        percentage = ?,
                        timestamp = ?,
                        device = ?,
                        device_id = ?,
                        kavita_synced_at = COALESCE(?, kavita_synced_at),
                        calibre_synced_at = COALESCE(?, calibre_synced_at),
                        last_sync_status = COALESCE(?, last_sync_status)
                    WHERE document = ?
                    """,
                    (
                        title,
                        authors,
                        filename,
                        final_cal_id,
                        use_prog,
                        use_pct,
                        use_ts,
                        use_device,
                        use_device_id,
                        kavita_synced_at,
                        calibre_synced_at,
                        sync_status,
                        target_doc,
                    ),
                )
            else:
                # Insert brand new row
                conn.execute(
                    """
                    INSERT INTO tracked_documents (
                        document, title, authors, filename, calibre_book_id,
                        progress, percentage, timestamp, device, device_id,
                        kavita_synced_at, calibre_synced_at, last_sync_status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.document,
                        record.title,
                        record.authors,
                        record.filename,
                        cal_id,
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

        if cal_id:
            self.merge_duplicate_calibre_entries()

    def link_calibre_book_id(self, document: str, calibre_book_id: int):
        """Associates a KOReader document hash with a Calibre book ID and merges if duplicate."""
        with self._get_connection() as conn:
            conn.execute(
                "UPDATE tracked_documents SET calibre_book_id = ? WHERE document = ?",
                (calibre_book_id, document),
            )
            conn.commit()
        self.link_document_alias(document, calibre_book_id)
        self.merge_duplicate_calibre_entries()

    def get_aliases_for_calibre_id(self, calibre_id: int) -> List[str]:
        """Returns all document hashes linked to a Calibre book ID."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT document FROM document_aliases WHERE calibre_id = ?",
                (calibre_id,),
            )
            return [row["document"] for row in cursor.fetchall()]

    def get_document(self, document: str) -> Optional[ProgressRecord]:
        """Retrieves progress record for a specific document hash, checking aliases and calibre_id."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM tracked_documents WHERE document = ?",
                (document,),
            )
            row = cursor.fetchone()
            if row:
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
                    calibre_id=row["calibre_book_id"],
                )

            # Check if this document hash is an alias to a Calibre book ID
            calibre_id = self.get_calibre_id_for_document(document)
            if calibre_id:
                cursor = conn.execute(
                    "SELECT * FROM tracked_documents WHERE calibre_book_id = ?",
                    (calibre_id,),
                )
                row = cursor.fetchone()
                if row:
                    return ProgressRecord(
                        document=document,
                        progress=row["progress"],
                        percentage=row["percentage"],
                        timestamp=row["timestamp"],
                        device=row["device"],
                        device_id=row["device_id"],
                        title=row["title"],
                        authors=row["authors"],
                        filename=row["filename"],
                        calibre_id=row["calibre_book_id"],
                    )

            return None

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
            cursor = conn.execute("SELECT COUNT(DISTINCT COALESCE(calibre_book_id, document)) FROM tracked_documents")
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

            # Fallback to book_mappings
            cursor = conn.execute(
                "SELECT calibre_id FROM book_mappings WHERE koreader_hash = ?",
                (document,),
            )
            row = cursor.fetchone()
            if row and row["calibre_id"]:
                return row["calibre_id"]

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
                    SELECT COUNT(DISTINCT COALESCE(d.calibre_book_id, d.document))
                    FROM tracked_documents d
                    LEFT JOIN book_mappings m ON d.calibre_book_id = m.calibre_id
                    LEFT JOIN document_aliases a ON d.calibre_book_id = a.calibre_id
                    WHERE lower(COALESCE(NULLIF(NULLIF(d.title, 'Unknown'), ''), m.title, '')) LIKE ?
                       OR lower(COALESCE(NULLIF(d.authors, ''), m.authors, '')) LIKE ?
                       OR CAST(d.calibre_book_id AS TEXT) LIKE ?
                       OR lower(d.document) LIKE ?
                       OR lower(COALESCE(a.document, '')) LIKE ?
                    """,
                    (term, term, term, term, term),
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
                    LEFT JOIN document_aliases a ON d.calibre_book_id = a.calibre_id
                    WHERE lower(COALESCE(NULLIF(NULLIF(d.title, 'Unknown'), ''), m.title, '')) LIKE ?
                       OR lower(COALESCE(NULLIF(d.authors, ''), m.authors, '')) LIKE ?
                       OR CAST(d.calibre_book_id AS TEXT) LIKE ?
                       OR lower(d.document) LIKE ?
                       OR lower(COALESCE(a.document, '')) LIKE ?
                    GROUP BY COALESCE(d.calibre_book_id, d.document)
                    ORDER BY d.timestamp DESC
                    LIMIT ? OFFSET ?
                    """,
                    (term, term, term, term, term, page_size, offset),
                )
                rows = cursor.fetchall()
            else:
                count_cursor = conn.execute("SELECT COUNT(DISTINCT COALESCE(calibre_book_id, document)) FROM tracked_documents")
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
                    GROUP BY COALESCE(d.calibre_book_id, d.document)
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
            tracked_count = conn.execute(
                "SELECT COUNT(DISTINCT coalesce(calibre_book_id, document)) FROM tracked_documents"
            ).fetchone()[0]

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
