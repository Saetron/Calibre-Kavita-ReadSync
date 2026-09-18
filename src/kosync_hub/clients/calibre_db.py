"""Direct Calibre metadata.db adapter with Calibre ID support and custom columns."""

import logging
import os
import re
import sqlite3
import unicodedata
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

from ..hasher import compute_all_book_hashes, compute_koreader_partial_md5
from ..models import CalibreBookRecord, ProgressRecord
from .base import BaseSyncClient

logger = logging.getLogger("kosync_hub.calibre_db")


def normalize_string(s: Optional[str]) -> str:
    """Normalizes a string for fuzzy/robust matching (lowercased, alphanumeric only)."""
    if not s:
        return ""
    s = s.split(":")[0].split(" - ")[0]
    s = unicodedata.normalize("NFKD", s).encode("ASCII", "ignore").decode("utf-8")
    return re.sub(r"[^\w\s]", "", s.lower()).strip()


class CalibreDbClient(BaseSyncClient):
    """Interacts directly with a Calibre library's metadata.db and book files."""

    def __init__(
        self,
        library_path: str,
        read_pct_column: str = "#read_pct",
        read_status_column: str = "#read_status",
        last_read_column: str = "#last_read",
        progress_column: str = "#koreader_progress",
        auto_create_columns: bool = True,
        mark_read_threshold: float = 0.98,
    ):
        self.library_path = Path(library_path)
        self.db_path = self.library_path / "metadata.db"
        self.read_pct_label = read_pct_column.lstrip("#")
        self.read_status_label = read_status_column.lstrip("#")
        self.last_read_label = last_read_column.lstrip("#")
        self.progress_label = progress_column.lstrip("#")
        self.auto_create_columns = auto_create_columns
        self.mark_read_threshold = mark_read_threshold
        self._column_cache: Dict[str, Tuple[int, str]] = {}
        self._hash_cache: Dict[str, int] = {}
        self._id_hash_cache: Dict[int, str] = {}

    @property
    def name(self) -> str:
        return "Calibre DB"

    def _register_calibre_functions(self, conn: sqlite3.Connection):
        """Registers Calibre user-defined SQLite functions so triggers (like title_sort) work."""
        def _title_sort(title: Optional[str]) -> str:
            if not title:
                return ""
            return re.sub(r"^(A|An|The|Der|Die|Das|Ein|Eine)\s+", "", str(title), flags=re.IGNORECASE).strip()

        for name, n_args, func in [
            ("title_sort", 1, _title_sort),
            ("uuid4", 0, lambda: str(uuid.uuid4())),
            ("sort_authors", 1, lambda a: str(a or "")),
            ("books_list_filter", 1, lambda v: 1),
            ("fncase", 1, lambda v: str(v or "").lower()),
        ]:
            try:
                conn.create_function(name, n_args, func)
            except Exception:
                pass

    def _get_connection(self) -> sqlite3.Connection:
        if not self.db_path.is_file():
            raise FileNotFoundError(f"Calibre database not found at {self.db_path}")
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        self._register_calibre_functions(conn)
        return conn

    async def test_connection(self) -> bool:
        """Verifies access to metadata.db and initializes required custom columns."""
        if not self.db_path.is_file():
            logger.error(f"Calibre metadata.db does not exist at {self.db_path}")
            return False

        try:
            with self._get_connection() as conn:
                cursor = conn.execute("SELECT COUNT(*) FROM books")
                count = cursor.fetchone()[0]
                logger.info(f"Connected to Calibre DB ({count} books found).")

                if self.auto_create_columns:
                    self._ensure_custom_columns(conn)

                self._refresh_column_cache(conn)
                return True
        except Exception as e:
            logger.error(f"Failed to access Calibre database at {self.db_path}: {e}")
            return False

    def _refresh_column_cache(self, conn: sqlite3.Connection):
        """Loads custom columns into memory."""
        cursor = conn.execute("SELECT id, label, datatype FROM custom_columns WHERE mark_for_delete = 0")
        self._column_cache = {row["label"]: (row["id"], row["datatype"]) for row in cursor.fetchall()}

    def _ensure_custom_columns(self, conn: sqlite3.Connection):
        """Ensures that required custom columns exist in metadata.db, creating them if missing."""
        self._refresh_column_cache(conn)

        columns_to_ensure = [
            (self.read_pct_label, "Read Progress (%)", "float", "{}"),
            (self.read_status_label, "Read Status", "bool", "{}"),
            (self.last_read_label, "Last Read Date", "datetime", "{}"),
            (self.progress_label, "KOReader Progress CFI", "comments", "{}"),
        ]

        for label, name, datatype, display in columns_to_ensure:
            if not label:
                continue
            if label not in self._column_cache:
                logger.info(f"Creating custom column '#{label}' ({datatype}) in Calibre DB...")
                cursor = conn.execute(
                    """
                    INSERT INTO custom_columns (label, name, datatype, mark_for_delete, editable, display, is_multiple, normalized)
                    VALUES (?, ?, ?, 0, 1, ?, 0, 0)
                    """,
                    (label, name, datatype, display),
                )
                col_id = cursor.lastrowid
                table_name = f"custom_column_{col_id}"

                sql_type = "REAL" if datatype == "float" else "INTEGER" if datatype in ("bool", "int") else "TEXT"
                conn.execute(f"""
                    CREATE TABLE IF NOT EXISTS {table_name} (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        book INTEGER NOT NULL,
                        value {sql_type} NOT NULL,
                        UNIQUE(book)
                    )
                """)
                conn.execute(f"CREATE INDEX IF NOT EXISTS {table_name}_idx ON {table_name}(book)")
                conn.commit()
                logger.info(f"Created Calibre table {table_name} for '#{label}'.")

        self._refresh_column_cache(conn)

    def get_or_compute_koreader_hash(self, book_id: int) -> Optional[str]:
        """Returns KOReader hash for a Calibre book, computing from file if needed."""
        if book_id in self._id_hash_cache:
            return self._id_hash_cache[book_id]

        with self._get_connection() as conn:
            # Check identifiers
            cursor = conn.execute(
                "SELECT val FROM identifiers WHERE book = ? AND type = 'koreader' LIMIT 1",
                (book_id,),
            )
            row = cursor.fetchone()
            if row and row["val"]:
                hsh = row["val"]
                self._id_hash_cache[book_id] = hsh
                self._hash_cache[hsh] = book_id
                return hsh

            # Compute from ebook file
            cursor = conn.execute(
                """
                SELECT b.path, d.name, d.format
                FROM books b
                JOIN data d ON b.id = d.book
                WHERE b.id = ? AND d.format IN ('EPUB', 'MOBI', 'AZW3', 'PDF')
                LIMIT 1
                """,
                (book_id,),
            )
            row = cursor.fetchone()
            if row:
                file_path = self.library_path / row["path"] / f"{row['name']}.{row['format'].lower()}"
                if file_path.is_file():
                    hsh = compute_koreader_partial_md5(file_path)
                    if hsh:
                        self.link_book_hash(book_id, hsh)
                        return hsh
        return None

    def find_book_by_hash(self, document_hash: str) -> Optional[int]:
        """Looks up a book ID by KOReader or MD5 identifier."""
        if not document_hash:
            return None
        if document_hash in self._hash_cache:
            return self._hash_cache[document_hash]

        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT book FROM identifiers
                WHERE (type = 'koreader' OR type = 'md5') AND val = ?
                LIMIT 1
                """,
                (document_hash,),
            )
            row = cursor.fetchone()
            if row:
                book_id = row["book"]
                self._hash_cache[document_hash] = book_id
                self._id_hash_cache[book_id] = document_hash
                return book_id
        return None

    def link_book_hash(self, book_id: int, document_hash: str):
        """Records identifier (type='koreader', val=document_hash) in Calibre DB."""
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO identifiers (book, type, val)
                VALUES (?, 'koreader', ?)
                """,
                (book_id, document_hash),
            )
            conn.commit()
            self._hash_cache[document_hash] = book_id
            self._id_hash_cache[book_id] = document_hash

    def match_book(
        self,
        document_hash: str,
        filename: Optional[str] = None,
        title: Optional[str] = None,
        authors: Optional[str] = None,
    ) -> Optional[int]:
        """Matches a book using filename or normalized title and authors."""
        with self._get_connection() as conn:
            cursor = conn.execute("""
                SELECT b.id, b.title, d.name, d.format
                FROM books b
                LEFT JOIN data d ON b.id = d.book
                ORDER BY b.id DESC
            """)
            books_data = cursor.fetchall()

            if filename:
                clean_target_fn = Path(filename).name.lower()
                clean_stem = Path(filename).stem.lower()
                for row in books_data:
                    calibre_fn = f"{row['name']}.{row['format'].lower()}".lower() if row["name"] and row["format"] else ""
                    if calibre_fn == clean_target_fn or (row["name"] and row["name"].lower() == clean_stem):
                        return row["id"]

            if title:
                norm_title = normalize_string(title)
                norm_authors = normalize_string(authors) if authors else ""
                for row in books_data:
                    row_title = normalize_string(row["title"])
                    if norm_title and (norm_title == row_title or norm_title in row_title or row_title in norm_title):
                        if norm_authors:
                            author_cursor = conn.execute(
                                """
                                SELECT a.name FROM authors a
                                JOIN books_authors_link bal ON a.id = bal.author
                                WHERE bal.book = ?
                                """,
                                (row["id"],),
                            )
                            book_authors = " ".join(normalize_string(a["name"]) for a in author_cursor.fetchall())
                            if any(part in book_authors for part in norm_authors.split()):
                                return row["id"]
                        else:
                            return row["id"]

        return None

    def get_book_by_id(self, book_id: int) -> Optional[CalibreBookRecord]:
        """Retrieves complete Calibre book record including custom columns by Calibre ID."""
        with self._get_connection() as conn:
            self._refresh_column_cache(conn)

            cursor = conn.execute(
                """
                SELECT b.id, b.title, b.path, b.last_modified, d.name, d.format
                FROM books b
                LEFT JOIN data d ON b.id = d.book
                WHERE b.id = ?
                LIMIT 1
                """,
                (book_id,),
            )
            row = cursor.fetchone()
            if not row:
                return None

            author_cursor = conn.execute(
                """
                SELECT a.name FROM authors a
                JOIN books_authors_link bal ON a.id = bal.author
                WHERE bal.book = ?
                """,
                (book_id,),
            )
            authors = ", ".join(a["name"] for a in author_cursor.fetchall())

            # Read percentage
            pct = 0.0
            if self.read_pct_label in self._column_cache:
                col_id, _ = self._column_cache[self.read_pct_label]
                c_row = conn.execute(f"SELECT value FROM custom_column_{col_id} WHERE book = ?", (book_id,)).fetchone()
                if c_row and c_row["value"] is not None:
                    val = float(c_row["value"])
                    pct = val / 100.0 if val > 1.0 else val

            # Read status
            is_read = False
            if self.read_status_label in self._column_cache:
                col_id, _ = self._column_cache[self.read_status_label]
                c_row = conn.execute(f"SELECT value FROM custom_column_{col_id} WHERE book = ?", (book_id,)).fetchone()
                if c_row and c_row["value"] is not None:
                    is_read = bool(c_row["value"])

            # Last read
            last_read = None
            if self.last_read_label in self._column_cache:
                col_id, _ = self._column_cache[self.last_read_label]
                c_row = conn.execute(f"SELECT value FROM custom_column_{col_id} WHERE book = ?", (book_id,)).fetchone()
                if c_row and c_row["value"]:
                    last_read = str(c_row["value"])

            # Progress string
            progress_str = None
            if self.progress_label in self._column_cache:
                col_id, _ = self._column_cache[self.progress_label]
                c_row = conn.execute(f"SELECT value FROM custom_column_{col_id} WHERE book = ?", (book_id,)).fetchone()
                if c_row and c_row["value"]:
                    progress_str = str(c_row["value"])

            file_rel = f"{row['path']}/{row['name']}.{row['format'].lower()}" if row["name"] and row["format"] else ""
            file_path = str(self.library_path / file_rel) if file_rel else None

            # Get hash
            hsh = self.get_or_compute_koreader_hash(book_id)

            return CalibreBookRecord(
                book_id=book_id,
                title=row["title"] or "Unknown",
                authors=authors or "Unknown",
                path=row["path"] or "",
                file_path=file_path,
                format=row["format"],
                percentage=pct,
                last_read=last_read,
                last_modified=row["last_modified"],
                koreader_progress=progress_str,
                is_read=is_read,
                koreader_hash=hsh,
            )

    def get_recently_read_books(self, limit: int = 50) -> List[CalibreBookRecord]:
        """Fetches books from Calibre that have reading progress, have KOReader hashes, or were recently modified."""
        books = []
        seen_ids = set()
        with self._get_connection() as conn:
            self._refresh_column_cache(conn)

            # 1. Books with reading progress
            if self.read_pct_label in self._column_cache:
                col_id, _ = self._column_cache[self.read_pct_label]
                cursor = conn.execute(
                    f"""
                    SELECT book FROM custom_column_{col_id}
                    WHERE value > 0
                    ORDER BY id DESC LIMIT ?
                    """,
                    (limit,),
                )
                for r in cursor.fetchall():
                    bid = r["book"]
                    if bid not in seen_ids:
                        seen_ids.add(bid)
                        b = self.get_book_by_id(bid)
                        if b:
                            books.append(b)

            # 2. Books with KOReader identifier
            try:
                cursor = conn.execute(
                    """
                    SELECT book FROM identifiers
                    WHERE type = 'koreader'
                    ORDER BY id DESC LIMIT ?
                    """,
                    (limit,),
                )
                for r in cursor.fetchall():
                    bid = r["book"]
                    if bid not in seen_ids:
                        seen_ids.add(bid)
                        b = self.get_book_by_id(bid)
                        if b:
                            books.append(b)
            except Exception as e:
                logger.debug(f"Could not query identifiers for koreader hashes: {e}")

            # 3. Recently modified/added books in Calibre
            cursor = conn.execute("SELECT id FROM books ORDER BY last_modified DESC LIMIT ?", (limit,))
            for r in cursor.fetchall():
                bid = r["id"]
                if bid not in seen_ids:
                    seen_ids.add(bid)
                    b = self.get_book_by_id(bid)
                    if b:
                        books.append(b)

        return books

    def update_book_progress(
        self,
        book_id: int,
        percentage: float,
        progress_str: Optional[str] = None,
        timestamp: Optional[int] = None,
    ) -> bool:
        """Writes reading progress into Calibre's metadata.db custom columns for a specific Calibre book ID."""
        with self._get_connection() as conn:
            self._refresh_column_cache(conn)
            if timestamp:
                now_iso = datetime.utcfromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S+00:00")
            else:
                now_iso = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S+00:00")

            # 1. Update Read Percentage (#read_pct)
            if self.read_pct_label in self._column_cache:
                col_id, _ = self._column_cache[self.read_pct_label]
                table = f"custom_column_{col_id}"
                val = round(percentage * 100, 2)
                conn.execute(
                    f"""
                    INSERT INTO {table} (book, value) VALUES (?, ?)
                    ON CONFLICT(book) DO UPDATE SET value = excluded.value
                    """,
                    (book_id, val),
                )

            # 2. Update Read Status Boolean (#read_status)
            if self.read_status_label in self._column_cache:
                col_id, _ = self._column_cache[self.read_status_label]
                table = f"custom_column_{col_id}"
                is_read = 1 if percentage >= self.mark_read_threshold else 0
                conn.execute(
                    f"""
                    INSERT INTO {table} (book, value) VALUES (?, ?)
                    ON CONFLICT(book) DO UPDATE SET value = excluded.value
                    """,
                    (book_id, is_read),
                )

            # 3. Update Last Read Date (#last_read)
            if self.last_read_label in self._column_cache:
                col_id, _ = self._column_cache[self.last_read_label]
                table = f"custom_column_{col_id}"
                conn.execute(
                    f"""
                    INSERT INTO {table} (book, value) VALUES (?, ?)
                    ON CONFLICT(book) DO UPDATE SET value = excluded.value
                    """,
                    (book_id, now_iso),
                )

            # 4. Update KOReader Progress CFI (#koreader_progress)
            if progress_str and self.progress_label in self._column_cache:
                col_id, _ = self._column_cache[self.progress_label]
                table = f"custom_column_{col_id}"
                conn.execute(
                    f"""
                    INSERT INTO {table} (book, value) VALUES (?, ?)
                    ON CONFLICT(book) DO UPDATE SET value = excluded.value
                    """,
                    (book_id, progress_str),
                )

            # 5. Update book last_modified
            try:
                conn.execute("UPDATE books SET last_modified = ? WHERE id = ?", (now_iso, book_id))
            except Exception as e:
                logger.warning(f"Could not update last_modified on books for book #{book_id}: {e}")

            conn.commit()

            logger.info(f"Updated Calibre DB book ID {book_id}: progress={round(percentage * 100, 1)}%")
            return True

    async def get_progress(self, document: str) -> Optional[ProgressRecord]:
        """BaseSyncClient compatibility: get progress by document hash."""
        book_id = self.find_book_by_hash(document)
        if not book_id:
            return None
        book = self.get_book_by_id(book_id)
        if not book:
            return None
        ts = int(datetime.utcnow().timestamp())
        if book.last_read:
            try:
                dt = datetime.fromisoformat(book.last_read.replace("Z", "+00:00"))
                ts = int(dt.timestamp())
            except Exception:
                pass
        return ProgressRecord(
            document=document,
            progress=book.koreader_progress or f"page:{book.percentage}",
            percentage=book.percentage,
            timestamp=ts,
            device="Calibre",
            calibre_id=book_id,
            title=book.title,
            authors=book.authors,
        )

    async def update_progress(self, record: ProgressRecord) -> bool:
        """BaseSyncClient compatibility: update progress by record."""
        book_id = record.calibre_id or self.find_book_by_hash(record.document)
        if not book_id:
            if record.filename:
                # Check for {id} in filename
                match = re.search(r"\{(\d+)\}", record.filename)
                if match:
                    book_id = int(match.group(1))

            if not book_id:
                # Check filename or title/author fallback
                bm = self.match_book(
                    document_hash=record.document,
                    filename=record.filename,
                    title=record.title,
                    authors=record.authors,
                )
                if bm:
                    book_id = bm

        if not book_id:
            return False

        if record.document and not record.document.startswith("calibre_"):
            self.link_book_hash(book_id, record.document)

        return self.update_book_progress(
            book_id=book_id,
            percentage=record.percentage,
            progress_str=record.progress,
            timestamp=record.timestamp,
        )
