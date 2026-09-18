"""Internal SQLite storage for tracking synchronized books, hashes, and audit history."""

import os
import sqlite3
from pathlib import Path
from typing import List, Optional
from .models import ProgressRecord, SyncEvent


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
            try:
                conn.execute("ALTER TABLE sync_events ADD COLUMN calibre_id INTEGER")
            except sqlite3.OperationalError:
                pass

            conn.execute("CREATE INDEX IF NOT EXISTS idx_tracked_timestamp ON tracked_documents(timestamp)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_events_timestamp ON sync_events(timestamp DESC)")
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
