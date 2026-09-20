"""Calibre SQLite database reader for Kavita VFS.

Extracts book metadata, formats, and custom columns ('type', 'chapter', 'volume').
"""

from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class CustomColumnInfo:
    id: int
    label: str
    name: str
    datatype: str
    normalized: bool


@dataclass
class BookFileRecord:
    book_id: int
    title: str
    series: Optional[str]
    language: Optional[str]
    type_: Optional[str]
    volume: Any
    chapter: Any
    format: str
    source_path: str


class CalibreDBReader:
    def __init__(
        self,
        calibre_dir: str,
        custom_type_column: str = "type",
        custom_volume_column: str = "volume",
        custom_chapter_column: str = "chapter",
    ):
        self.calibre_dir = os.path.abspath(calibre_dir)
        self.db_path = os.path.join(self.calibre_dir, "metadata.db")
        self.custom_type_column = custom_type_column
        self.custom_volume_column = custom_volume_column
        self.custom_chapter_column = custom_chapter_column

    def exists(self) -> bool:
        return os.path.isfile(self.db_path)

    def get_last_modified(self) -> float:
        """Return database last modification timestamp, or 0.0 if not found."""
        try:
            return os.path.getmtime(self.db_path)
        except OSError:
            return 0.0

    def _open_connection(self) -> sqlite3.Connection:
        if not self.exists():
            raise FileNotFoundError(f"Calibre database not found at {self.db_path}")
        # Open in URI read-only mode to avoid locking issues with running Calibre instances
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    def discover_custom_columns(
        self, conn: sqlite3.Connection
    ) -> Dict[str, CustomColumnInfo]:
        """Discover custom columns for 'type', 'chapter', and 'volume'."""
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='custom_columns'")
        if not cursor.fetchone():
            logger.debug("No custom_columns table found in Calibre DB.")
            return {}

        cursor.execute("SELECT id, label, name, datatype, normalized FROM custom_columns")
        columns: Dict[str, CustomColumnInfo] = {}
        target_labels = {
            self.custom_type_column.lower().lstrip("#"): "type",
            self.custom_chapter_column.lower().lstrip("#"): "chapter",
            self.custom_volume_column.lower().lstrip("#"): "volume",
            "type": "type",
            "chapter": "chapter",
            "volume": "volume",
        }

        for row in cursor.fetchall():
            col_id = row["id"]
            raw_label = (row["label"] or "").lower().lstrip("#")
            col_name = row["name"]
            datatype = row["datatype"] or "text"
            normalized = bool(row["normalized"])

            if raw_label in target_labels:
                canonical_key = target_labels[raw_label]
                columns[canonical_key] = CustomColumnInfo(
                    id=col_id,
                    label=raw_label,
                    name=col_name,
                    datatype=datatype,
                    normalized=normalized,
                )
                logger.info(
                    "Found custom column '%s' (id=%d, label=%s, datatype=%s, normalized=%s)",
                    raw_label,
                    col_id,
                    row["label"],
                    datatype,
                    normalized,
                )

        return columns

    def _load_custom_column_values(
        self, conn: sqlite3.Connection, col_info: Optional[CustomColumnInfo]
    ) -> Dict[int, Any]:
        """Load mapping of book_id -> custom column value."""
        if not col_info:
            return {}

        result: Dict[int, Any] = {}
        cursor = conn.cursor()

        table_name = f"custom_column_{col_info.id}"
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        )
        if not cursor.fetchone():
            return {}

        if col_info.normalized:
            link_table = f"books_custom_column_{col_info.id}_link"
            cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (link_table,),
            )
            if not cursor.fetchone():
                return {}

            # Series custom column types might have an extra 'extra' column for index
            query = f"""
                SELECT link.book, c.value
                FROM {link_table} link
                JOIN {table_name} c ON link.value = c.id
            """
            cursor.execute(query)
            for row in cursor.fetchall():
                result[row["book"]] = row["value"]
        else:
            query = f"SELECT book, value FROM {table_name}"
            cursor.execute(query)
            for row in cursor.fetchall():
                result[row["book"]] = row["value"]

        return result

    def get_all_book_files(self) -> List[BookFileRecord]:
        """Query Calibre DB and return all book files with their metadata."""
        with self._open_connection() as conn:
            custom_cols = self.discover_custom_columns(conn)
            type_vals = self._load_custom_column_values(conn, custom_cols.get("type"))
            chapter_vals = self._load_custom_column_values(conn, custom_cols.get("chapter"))
            volume_vals = self._load_custom_column_values(conn, custom_cols.get("volume"))

            cursor = conn.cursor()

            # Query books, primary series, primary language, and data formats
            # Note: A book might have multiple formats (e.g. EPUB and CBZ) in `data`
            query = """
                SELECT
                    b.id AS book_id,
                    b.title AS title,
                    b.path AS book_path,
                    s.name AS series_name,
                    l.lang_code AS lang_code,
                    d.format AS format,
                    d.name AS file_stem
                FROM books b
                JOIN data d ON b.id = d.book
                LEFT JOIN books_series_link bsl ON b.id = bsl.book
                LEFT JOIN series s ON bsl.series = s.id
                LEFT JOIN books_languages_link bll ON b.id = bll.book
                LEFT JOIN languages l ON bll.lang_code = l.id
                ORDER BY b.id, d.id
            """
            cursor.execute(query)
            rows = cursor.fetchall()

            records: List[BookFileRecord] = []
            seen_files = set()

            for row in rows:
                book_id = row["book_id"]
                title = row["title"] or ""
                book_path = row["book_path"] or ""
                series = row["series_name"]
                lang_code = row["lang_code"]
                fmt = row["format"] or ""
                file_stem = row["file_stem"] or ""

                if not fmt or not file_stem:
                    continue

                # Calibre formats in the filesystem are typically lowercase
                # e.g., /calibre_dir/Author/Title (id)/Title - Author.epub
                ext = fmt.lower()
                rel_file_path = os.path.join(book_path, f"{file_stem}.{ext}")
                full_source_path = os.path.join(self.calibre_dir, rel_file_path)

                # Fallback check if filesystem is case-sensitive and file has uppercase extension
                if not os.path.exists(full_source_path):
                    alt_path = os.path.join(self.calibre_dir, book_path, f"{file_stem}.{fmt}")
                    if os.path.exists(alt_path):
                        full_source_path = alt_path

                file_key = (book_id, full_source_path)
                if file_key in seen_files:
                    continue
                seen_files.add(file_key)

                records.append(
                    BookFileRecord(
                        book_id=book_id,
                        title=title,
                        series=series,
                        language=lang_code,
                        type_=type_vals.get(book_id),
                        volume=volume_vals.get(book_id),
                        chapter=chapter_vals.get(book_id),
                        format=fmt,
                        source_path=full_source_path,
                    )
                )

            return records
