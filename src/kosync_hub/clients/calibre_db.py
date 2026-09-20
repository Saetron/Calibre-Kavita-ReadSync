"""Direct Calibre metadata.db adapter with Calibre ID support and custom columns."""

import logging
import os
import re
import sqlite3
import time
import unicodedata
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from ..hasher import compute_all_book_hashes, compute_filename_md5, compute_koreader_partial_md5
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


def get_sanitized_name_variants(text: str) -> List[str]:
    """
    Generates filesystem-sanitized variations of a series or title.
    Calibre and FAT32/e-reader filesystems replace characters like +, &, :, ?, *, ", <, >, |
    with underscores (_) or spaces when exporting files or saving to devices.
    """
    if not text:
        return []
    variants = {text}

    # 1. Plus sign (+ -> _, + -> space, + -> &)
    if "+" in text:
        variants.add(text.replace("+", "_"))
        variants.add(text.replace(" + ", " _ "))
        variants.add(re.sub(r"\s*\+\s*", " _ ", text))
        variants.add(re.sub(r"\s*\+\s*", " ", text))
        variants.add(text.replace("+", " "))

    # 2. Ampersand (& -> _, & -> and)
    if "&" in text:
        variants.add(text.replace("&", "_"))
        variants.add(text.replace("&", "and"))
        variants.add(text.replace(" & ", " _ "))

    # 3. Colon (: -> -, : -> _, : -> space)
    if ":" in text:
        variants.add(text.replace(":", " -"))
        variants.add(text.replace(":", " _"))
        variants.add(text.replace(":", "_"))
        variants.add(text.replace(":", " "))
        variants.add(text.replace(":", ""))

    # 4. Standard FAT/Calibre illegal filename chars: ? * " < > | / \
    for ch in ['?', '*', '"', '<', '>', '|']:
        if ch in text:
            variants.add(text.replace(ch, "_"))
            variants.add(text.replace(ch, ""))

    # 5. Cleaned variants: collapsed underscores and normalized spaces
    cleaned = set(variants)
    for v in list(variants):
        c1 = re.sub(r"_+", "_", v).strip()
        c2 = re.sub(r"\s+", " ", v).strip()
        c3 = re.sub(r"\s*_\s*", " _ ", v).strip()
        cleaned.add(c1)
        cleaned.add(c2)
        cleaned.add(c3)

    return [v for v in cleaned if v]


class CalibreDbClient(BaseSyncClient):
    """Interacts directly with a Calibre library's metadata.db and book files."""

    def __init__(
        self,
        library_path: str,
        read_pct_column: str = "#read_pct",
        read_status_column: str = "#read_status",
        last_read_column: str = "#last_read",
        progress_column: str = "#koreader_progress",
        pages_column: str = "#pages",
        auto_create_columns: bool = True,
        mark_read_threshold: float = 0.98,
        internal_db: Optional[Any] = None,
    ):
        self.library_path = Path(library_path)
        self.db_path = self.library_path / "metadata.db"
        self.read_pct_label = read_pct_column.lstrip("#")
        self.read_status_label = read_status_column.lstrip("#")
        self.last_read_label = last_read_column.lstrip("#")
        self.progress_label = progress_column.lstrip("#")
        self.pages_label = (pages_column or "#pages").lstrip("#")
        self.auto_create_columns = auto_create_columns
        self.mark_read_threshold = mark_read_threshold
        self.internal_db = internal_db
        self._column_cache: Dict[str, Tuple[int, str]] = {}
        self._hash_cache: Dict[str, int] = {}
        self._id_hash_cache: Dict[int, str] = {}
        self._filename_hash_cache: Dict[str, int] = {}
        self._filename_cache_loaded: bool = False

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

    def _resolve_column(self, label: str) -> Optional[tuple[int, str]]:
        """Resolves a custom column label to its (col_id, table_name)."""
        if not label:
            return None
        clean_label = label.lstrip("#")
        with self._get_connection() as conn:
            if clean_label not in self._column_cache:
                self._refresh_column_cache(conn)
            if clean_label in self._column_cache:
                col_id, _ = self._column_cache[clean_label]
                return (col_id, f"custom_column_{col_id}")
        return None

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

    def index_filename_hashes(
        self,
        internal_db: Optional[Any] = None,
        incremental: bool = True,
    ) -> int:
        """
        Indexes filename MD5s for books in Calibre DB into internal_db or memory cache.
        If incremental is True and internal_db has indexed previously, only books modified
        since last_filename_index_time are processed.
        """
        t0 = time.time()
        db = internal_db or getattr(self, "internal_db", None)

        with self._get_connection() as conn:
            last_index = None
            if db and incremental:
                try:
                    if hasattr(db, "count_filename_hashes") and db.count_filename_hashes() > 0:
                        if hasattr(db, "get_metadata") and db.get_metadata("filename_index_version") != "2":
                            last_index = None
                        else:
                            last_index = db.get_metadata("last_filename_index_time")
                except Exception:
                    last_index = None

            # Check if last_modified column exists in books table
            has_last_mod = False
            try:
                col_cur = conn.execute("PRAGMA table_info(books)")
                cols = [c[1] for c in col_cur.fetchall()]
                has_last_mod = "last_modified" in cols
            except Exception:
                pass

            query_where = ""
            params = []
            if last_index and has_last_mod:
                query_where = "WHERE b.last_modified > ?"
                params.append(last_index)

            last_mod_col = "b.last_modified, " if has_last_mod else ""
            try:
                cursor = conn.execute(f"""
                    SELECT b.id, b.title, b.path, b.series_index, {last_mod_col}d.name, d.format, s.name as series_name
                    FROM books b
                    LEFT JOIN data d ON b.id = d.book
                    LEFT JOIN books_series_link bsl ON b.id = bsl.book
                    LEFT JOIN series s ON bsl.series = s.id
                    {query_where}
                """, params)
                rows = cursor.fetchall()
            except sqlite3.OperationalError:
                cursor = conn.execute(f"""
                    SELECT b.id, b.title, b.path, {last_mod_col}d.name, d.format
                    FROM books b
                    LEFT JOIN data d ON b.id = d.book
                    {query_where}
                """, params)
                rows = cursor.fetchall()

            if not rows and last_index:
                logger.debug("No modified books in Calibre DB since last filename index.")
                return 0

            author_cursor = conn.execute("""
                SELECT bal.book, a.name
                FROM books_authors_link bal
                JOIN authors a ON bal.author = a.id
            """)
            book_authors: Dict[int, List[str]] = {}
            for ar in author_cursor.fetchall():
                book_authors.setdefault(ar["book"], []).append(ar["name"])

            batch = []
            indexed_count = 0
            for r in rows:
                bid = r["id"]
                title = r["title"] or ""
                b_path = r["path"] or ""
                folder_name = Path(b_path).name if b_path else ""
                d_name = r["name"] or ""
                fmt = (r["format"] or "epub").lower()
                authors_list = book_authors.get(bid, [])
                authors_str = ", ".join(authors_list)
                series_name = r["series_name"] if "series_name" in r.keys() and r["series_name"] else ""
                series_index = r["series_index"] if "series_index" in r.keys() else None

                candidates = set()
                # 1. Calibre internal filename
                if d_name:
                    candidates.add(f"{d_name}.{fmt}")
                    candidates.add(f"{d_name}.{fmt.upper()}")
                    candidates.add(f"{d_name}.kepub.epub")
                    candidates.add(d_name)
                # 2. Folder name (e.g. Title (123))
                if folder_name:
                    candidates.add(f"{folder_name}.{fmt}")
                    candidates.add(f"{folder_name}.kepub.epub")
                    candidates.add(folder_name)
                # 3. Series template variations: series/series - series_index {id}
                if series_name:
                    s_reprs = []
                    if series_index is not None:
                        try:
                            s_flt = float(series_index)
                            if s_flt.is_integer():
                                s_int = int(s_flt)
                                s_reprs.extend([str(s_int), f"{s_int:02d}", f"{s_flt:g}", f"{s_flt:.1f}"])
                            else:
                                s_reprs.extend([f"{s_flt:g}", str(s_flt)])
                        except (ValueError, TypeError):
                            s_reprs.append(str(series_index))
                    else:
                        s_reprs.extend(["1", "01"])

                    series_variants = get_sanitized_name_variants(series_name)
                    for s_var in series_variants:
                        for s_repr in s_reprs:
                            # Exact user template: series - series_index {id}.epub
                            candidates.add(f"{s_var} - {s_repr} {{{bid}}}.{fmt}")
                            candidates.add(f"{s_var} - {s_repr} {{{bid}}}")
                            candidates.add(f"{s_var} - {s_repr} {{{bid}}}.epub")
                            candidates.add(f"{s_var}/{s_var} - {s_repr} {{{bid}}}.{fmt}")
                            candidates.add(f"{s_var}/{s_var} - {s_repr} {{{bid}}}")
                            candidates.add(f"{s_var}\\{s_var} - {s_repr} {{{bid}}}.{fmt}")
                            candidates.add(f"{s_var}\\{s_var} - {s_repr} {{{bid}}}")
                            if s_var != series_name:
                                candidates.add(f"{series_name}/{s_var} - {s_repr} {{{bid}}}.{fmt}")
                                candidates.add(f"{s_var}/{series_name} - {s_repr} {{{bid}}}.{fmt}")

                            # Common variants
                            candidates.add(f"{s_var} - {s_repr} ({bid}).{fmt}")
                            candidates.add(f"{s_var} - {s_repr} [{bid}].{fmt}")
                            candidates.add(f"{s_var} - {s_repr}.{fmt}")
                            candidates.add(f"{s_var} {s_repr} {{{bid}}}.{fmt}")
                            candidates.add(f"{s_var} - Volume {s_repr} {{{bid}}}.{fmt}")
                            candidates.add(f"{s_var} - Vol. {s_repr} {{{bid}}}.{fmt}")
                            candidates.add(f"{s_var} - Vol {s_repr} {{{bid}}}.{fmt}")

                        if title:
                            candidates.add(f"{s_var} - {title} {{{bid}}}.{fmt}")
                            candidates.add(f"{s_var} - {title}.{fmt}")

                # 4. Title variations
                if title:
                    title_variants = get_sanitized_name_variants(title)
                    for t_var in title_variants:
                        candidates.add(f"{t_var}.{fmt}")
                        candidates.add(f"{t_var}.kepub.epub")
                        candidates.add(t_var)
                        candidates.add(f"{t_var} ({bid}).{fmt}")
                        candidates.add(f"{t_var} {{{bid}}}.{fmt}")
                        candidates.add(f"{t_var} [{bid}].{fmt}")
                        candidates.add(f"{t_var} - {bid}.{fmt}")
                        candidates.add(f"{t_var}_{bid}.{fmt}")
                        candidates.add(f"{bid} - {t_var}.{fmt}")
                        candidates.add(f"{bid}_{t_var}.{fmt}")
                # 5. Standalone ID patterns
                candidates.add(f"{bid}.{fmt}")
                candidates.add(f"{bid}.kepub.epub")
                # 6. Author + title patterns
                if authors_str and title:
                    candidates.add(f"{title} - {authors_str}.{fmt}")
                    candidates.add(f"{title} - {authors_str} ({bid}).{fmt}")
                    candidates.add(f"{title} - {authors_str} {{{bid}}}.{fmt}")
                    candidates.add(f"{title} - {authors_str} [{bid}].{fmt}")
                    candidates.add(f"{authors_str} - {title}.{fmt}")
                    candidates.add(f"{authors_str} - {title} ({bid}).{fmt}")
                    candidates.add(f"{authors_str} - {title} {{{bid}}}.{fmt}")
                    candidates.add(f"{authors_str} - {title} [{bid}].{fmt}")

                for c in candidates:
                    if c:
                        h = compute_filename_md5(c)
                        self._filename_hash_cache[h] = bid
                        batch.append((h, bid))
                        h_lower = compute_filename_md5(c.lower())
                        self._filename_hash_cache[h_lower] = bid
                        batch.append((h_lower, bid))
                        if "/" in c or "\\" in c:
                            import hashlib
                            h_full = hashlib.md5(c.encode("utf-8")).hexdigest()
                            self._filename_hash_cache[h_full] = bid
                            batch.append((h_full, bid))
                            h_full_lower = hashlib.md5(c.lower().encode("utf-8")).hexdigest()
                            self._filename_hash_cache[h_full_lower] = bid
                            batch.append((h_full_lower, bid))

                indexed_count += 1
                if db and hasattr(db, "save_filename_hashes_batch") and len(batch) >= 50000:
                    db.save_filename_hashes_batch(batch)
                    batch.clear()

            if db and hasattr(db, "save_filename_hashes_batch") and batch:
                db.save_filename_hashes_batch(batch)
                batch.clear()

            if db and hasattr(db, "set_metadata"):
                import datetime
                max_mod = max((str(r["last_modified"]) for r in rows if "last_modified" in r.keys() and r["last_modified"]), default=None)
                db.set_metadata("filename_index_version", "2")
                db.set_metadata("last_filename_index_time", max_mod or datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"))

            self._filename_cache_loaded = True
            total_db = db.count_filename_hashes() if db and hasattr(db, "count_filename_hashes") else len(self._filename_hash_cache)
            logger.info(
                f"Indexed filename hashes for {indexed_count} books ({total_db} total stored in DB) in {time.time() - t0:.2f}s"
            )
            return indexed_count

    def _load_filename_hashes(self, conn: sqlite3.Connection):
        """Backward-compatible loader."""
        self.index_filename_hashes(getattr(self, "internal_db", None), incremental=False)

    def find_book_by_filename_hash(self, document_hash: str) -> Optional[int]:
        """Looks up a book ID by the MD5 hash of candidate filenames."""
        if not document_hash:
            return None

        # 1. Fast indexed lookup from internal DB (< 0.1ms)
        db = getattr(self, "internal_db", None)
        if db and hasattr(db, "get_calibre_id_by_filename_hash"):
            bid = db.get_calibre_id_by_filename_hash(document_hash)
            if bid:
                return bid

        # 2. Check in-memory cache
        if document_hash in self._filename_hash_cache:
            return self._filename_hash_cache[document_hash]

        # 3. Only if DB/cache is completely uninitialized, run initial index
        if not self._filename_cache_loaded and (not db or (hasattr(db, "count_filename_hashes") and db.count_filename_hashes() == 0)):
            self.index_filename_hashes(db, incremental=False)
            if db and hasattr(db, "get_calibre_id_by_filename_hash"):
                bid = db.get_calibre_id_by_filename_hash(document_hash)
                if bid:
                    return bid
            return self._filename_hash_cache.get(document_hash)

        return None

    def find_book_by_hash(self, document_hash: str) -> Optional[int]:
        """Looks up a book ID by KOReader or MD5 identifier or filename hash."""
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

        # Fallback to filename hash
        fn_match = self.find_book_by_filename_hash(document_hash)
        if fn_match:
            self._hash_cache[document_hash] = fn_match
            return fn_match

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
                    pct = max(0.0, min(1.0, val / 100.0))

            # Read status
            is_read = False
            has_read_status_col = False
            if self.read_status_label in self._column_cache:
                col_id, _ = self._column_cache[self.read_status_label]
                c_row = conn.execute(f"SELECT value FROM custom_column_{col_id} WHERE book = ?", (book_id,)).fetchone()
                if c_row and c_row["value"] is not None:
                    has_read_status_col = True
                    is_read = bool(c_row["value"])

            # If read status column is present and explicitly False, ensure pct cannot be >= 0.98 (or 1.0)
            if has_read_status_col and not is_read and pct >= 0.98:
                pct = 0.95
            elif is_read and pct == 0.0:
                pct = 1.0

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

    def get_all_books_with_progress(self) -> List[CalibreBookRecord]:
        """Fetches ALL books from Calibre that have reading progress (> 0% or marked read)."""
        books = []
        seen_ids = set()
        with self._get_connection() as conn:
            self._refresh_column_cache(conn)

            # 1. Books with read percentage > 0
            if self.read_pct_label in self._column_cache:
                col_id, _ = self._column_cache[self.read_pct_label]
                cursor = conn.execute(f"SELECT book FROM custom_column_{col_id} WHERE value > 0")
                for r in cursor.fetchall():
                    bid = r["book"]
                    if bid not in seen_ids:
                        seen_ids.add(bid)
                        b = self.get_book_by_id(bid)
                        if b:
                            books.append(b)

            # 2. Books with read status = 1 (true)
            if self.read_status_label in self._column_cache:
                col_id, _ = self._column_cache[self.read_status_label]
                cursor = conn.execute(f"SELECT book FROM custom_column_{col_id} WHERE value = 1")
                for r in cursor.fetchall():
                    bid = r["book"]
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
        if not book_id and record.document:
            book_id = self.find_book_by_filename_hash(record.document)

        if not book_id and record.filename:
            # Check for id in filename: {123}, (123), [123], _123, - 123
            for pat in [r"\{(\d+)\}", r"\((\d+)\)", r"\[(\d+)\]", r"[-_](\d+)\b"]:
                m = re.search(pat, record.filename)
                if m:
                    cand_id = int(m.group(1))
                    if self.get_book_by_id(cand_id):
                        book_id = cand_id
                        break

            if not book_id:
                # Check filename hash of the given filename string directly
                fn_hash = compute_filename_md5(record.filename)
                book_id = self.find_book_by_filename_hash(fn_hash)

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

    # -------------------------------------------------------------------------
    # Calibre Library & Reading History Analytics
    # -------------------------------------------------------------------------

    def get_library_statistics(self) -> Dict[str, Any]:
        """Calculates comprehensive library analytics from Calibre's metadata.db."""
        if not self.db_path.is_file():
            return {"error": "Calibre database not found"}

        with self._get_connection() as conn:
            def _scalar(sql: str, default=0):
                try:
                    res = conn.execute(sql).fetchone()
                    return res[0] if res and res[0] is not None else default
                except Exception:
                    return default

            total_books = _scalar("SELECT COUNT(*) FROM books")
            total_authors = _scalar("SELECT COUNT(*) FROM authors")
            total_series = _scalar("SELECT COUNT(*) FROM series")
            total_tags = _scalar("SELECT COUNT(*) FROM tags")
            total_publishers = _scalar("SELECT COUNT(*) FROM publishers")
            total_languages = _scalar("SELECT COUNT(*) FROM languages")
            total_size = _scalar("SELECT SUM(uncompressed_size) FROM data")

            # Formats breakdown
            formats = []
            try:
                cur = conn.execute(
                    """
                    SELECT format, COUNT(*) AS count, SUM(uncompressed_size) AS size
                    FROM data
                    WHERE format IS NOT NULL AND format != ''
                    GROUP BY format
                    ORDER BY count DESC
                    """
                )
                for r in cur.fetchall():
                    sz = r["size"] or 0
                    formats.append({
                        "format": r["format"].upper(),
                        "count": r["count"],
                        "size_bytes": sz,
                        "size_mb": round(sz / (1024 * 1024), 2),
                    })
            except Exception as e:
                logger.debug(f"Error fetching formats: {e}")

            # Identify completed book IDs from Calibre custom columns and internal_db
            completed_book_ids = set()
            read_col = self._resolve_column(self.read_pct_label)
            status_col = self._resolve_column(self.read_status_label)

            if status_col:
                try:
                    cur = conn.execute(f"SELECT book FROM {status_col[1]} WHERE value = 1")
                    for r in cur.fetchall():
                        completed_book_ids.add(r[0])
                except Exception as e:
                    logger.debug(f"Error fetching read_status books: {e}")

            if read_col:
                try:
                    cur = conn.execute(f"SELECT book, value FROM {read_col[1]} WHERE value IS NOT NULL")
                    for r in cur.fetchall():
                        try:
                            val = float(r[1])
                            pct = val / 100.0 if val > 1.0 else val
                            if pct >= self.mark_read_threshold:
                                completed_book_ids.add(r[0])
                        except Exception:
                            pass
                except Exception as e:
                    logger.debug(f"Error fetching read_pct books: {e}")

            if self.internal_db:
                try:
                    with self.internal_db._get_connection() as ic:
                        cur = ic.execute(
                            "SELECT calibre_book_id FROM tracked_documents WHERE percentage >= ? AND calibre_book_id IS NOT NULL",
                            (self.mark_read_threshold,)
                        )
                        for r in cur.fetchall():
                            completed_book_ids.add(r[0])
                except Exception as e:
                    logger.debug(f"Error fetching internal_db completed books: {e}")

            # Top 10 Authors (All and Read Only)
            author_counts_all: Dict[str, int] = {}
            author_counts_read: Dict[str, int] = {}
            try:
                cur = conn.execute(
                    """
                    SELECT a.name, bal.book
                    FROM books_authors_link bal
                    JOIN authors a ON bal.author = a.id
                    """
                )
                for r in cur.fetchall():
                    name = r["name"]
                    bid = r["book"]
                    author_counts_all[name] = author_counts_all.get(name, 0) + 1
                    if bid in completed_book_ids:
                        author_counts_read[name] = author_counts_read.get(name, 0) + 1
            except Exception as e:
                logger.debug(f"Error fetching top authors: {e}")

            top_authors = [{"name": k, "count": v} for k, v in sorted(author_counts_all.items(), key=lambda x: x[1], reverse=True)[:10]]
            top_authors_read = [{"name": k, "count": v} for k, v in sorted(author_counts_read.items(), key=lambda x: x[1], reverse=True)[:10]]

            # Top 10 Series (All and Read Only)
            series_counts_all: Dict[str, int] = {}
            series_counts_read: Dict[str, int] = {}
            try:
                cur = conn.execute(
                    """
                    SELECT s.name, bsl.book
                    FROM books_series_link bsl
                    JOIN series s ON bsl.series = s.id
                    """
                )
                for r in cur.fetchall():
                    name = r["name"]
                    bid = r["book"]
                    series_counts_all[name] = series_counts_all.get(name, 0) + 1
                    if bid in completed_book_ids:
                        series_counts_read[name] = series_counts_read.get(name, 0) + 1
            except Exception as e:
                logger.debug(f"Error fetching top series: {e}")

            top_series = [{"name": k, "count": v} for k, v in sorted(series_counts_all.items(), key=lambda x: x[1], reverse=True)[:10]]
            top_series_read = [{"name": k, "count": v} for k, v in sorted(series_counts_read.items(), key=lambda x: x[1], reverse=True)[:10]]

            # Top 15 Tags / Genres
            top_tags = []
            try:
                cur = conn.execute(
                    """
                    SELECT t.name, COUNT(btl.book) AS book_count
                    FROM books_tags_link btl
                    JOIN tags t ON btl.tag = t.id
                    GROUP BY t.id
                    ORDER BY book_count DESC
                    LIMIT 15
                    """
                )
                top_tags = [{"name": r["name"], "count": r["book_count"]} for r in cur.fetchall()]
            except Exception as e:
                logger.debug(f"Error fetching top tags: {e}")

            # Top 10 Publishers (All and Read Only)
            pub_counts_all: Dict[str, int] = {}
            pub_counts_read: Dict[str, int] = {}
            try:
                cur = conn.execute(
                    """
                    SELECT p.name, bpl.book
                    FROM books_publishers_link bpl
                    JOIN publishers p ON bpl.publisher = p.id
                    """
                )
                for r in cur.fetchall():
                    name = r["name"]
                    bid = r["book"]
                    pub_counts_all[name] = pub_counts_all.get(name, 0) + 1
                    if bid in completed_book_ids:
                        pub_counts_read[name] = pub_counts_read.get(name, 0) + 1
            except Exception as e:
                logger.debug(f"Error fetching top publishers: {e}")

            top_publishers = [{"name": k, "count": v} for k, v in sorted(pub_counts_all.items(), key=lambda x: x[1], reverse=True)[:10]]
            top_publishers_read = [{"name": k, "count": v} for k, v in sorted(pub_counts_read.items(), key=lambda x: x[1], reverse=True)[:10]]

            # Languages distribution
            languages = []
            try:
                cur = conn.execute(
                    """
                    SELECT l.lang_code, COUNT(bll.book) AS book_count
                    FROM books_languages_link bll
                    JOIN languages l ON bll.lang_code = l.id
                    GROUP BY l.id
                    ORDER BY book_count DESC
                    LIMIT 15
                    """
                )
                languages = [{"code": r["lang_code"], "count": r["book_count"]} for r in cur.fetchall()]
            except Exception as e:
                logger.debug(f"Error fetching languages: {e}")

            # Publication Years Distribution (recent 15 years with books)
            pub_years = []
            try:
                cur = conn.execute(
                    """
                    SELECT strftime('%Y', pubdate) AS yr, COUNT(*) AS count
                    FROM books
                    WHERE pubdate IS NOT NULL AND yr > '1900' AND yr <= strftime('%Y', 'now')
                    GROUP BY yr
                    ORDER BY yr DESC
                    LIMIT 15
                    """
                )
                pub_years = [{"year": r["yr"], "count": r["count"]} for r in cur.fetchall()]
            except Exception as e:
                logger.debug(f"Error fetching pub years: {e}")

            return {
                "total_books": total_books,
                "total_authors": total_authors,
                "total_series": total_series,
                "total_tags": total_tags,
                "total_publishers": total_publishers,
                "total_languages": total_languages,
                "completed_books_count": len(completed_book_ids),
                "total_size_bytes": total_size,
                "total_size_gb": round(total_size / (1024 * 1024 * 1024), 2) if total_size else 0.0,
                "formats": formats,
                "top_authors": top_authors,
                "top_authors_read": top_authors_read,
                "top_series": top_series,
                "top_series_read": top_series_read,
                "top_publishers": top_publishers,
                "top_publishers_read": top_publishers_read,
                "top_tags": top_tags,
                "languages": languages,
                "pub_years": pub_years,
            }

    def get_reading_history_analytics(self, year: Optional[int] = None) -> Dict[str, Any]:
        """
        Generates reading history analytics and Year-in-Review metrics.
        Combines Calibre custom columns (#read_pct, #read_status, #last_read)
        with internal database tracked_documents.
        """
        if not self.db_path.is_file():
            return {"error": "Calibre database not found"}

        import datetime

        now_year = datetime.datetime.utcnow().year
        selected_year = year or now_year

        read_col = self._resolve_column(self.read_pct_label)
        last_read_col = self._resolve_column(self.last_read_label)
        status_col = self._resolve_column(self.read_status_label)
        pages_col = (
            self._resolve_column(self.pages_label)
            or self._resolve_column("pages")
            or self._resolve_column("page_count")
            or self._resolve_column("pages_count")
        )

        completed_books = []
        available_years = set()
        in_progress_count = 0
        total_finished_all_time = 0

        # 1. Read from Calibre DB
        with self._get_connection() as conn:
            author_cursor = conn.execute("""
                SELECT bal.book, a.name
                FROM books_authors_link bal
                JOIN authors a ON bal.author = a.id
            """)
            book_authors: Dict[int, List[str]] = {}
            for ar in author_cursor.fetchall():
                book_authors.setdefault(ar["book"], []).append(ar["name"])

            tags_cursor = conn.execute("""
                SELECT btl.book, t.name
                FROM books_tags_link btl
                JOIN tags t ON btl.tag = t.id
            """)
            book_tags: Dict[int, List[str]] = {}
            for tr in tags_cursor.fetchall():
                book_tags.setdefault(tr["book"], []).append(tr["name"])

            has_native_pages = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='books_pages_link'"
            ).fetchone() is not None

            has_books_pages = any(
                col["name"] == "pages"
                for col in conn.execute("PRAGMA table_info(books)").fetchall()
            )

            query_parts = ["SELECT b.id, b.title"]
            joins = []
            if read_col:
                query_parts.append(f"c_pct.value AS read_pct")
                joins.append(f"LEFT JOIN {read_col[1]} c_pct ON b.id = c_pct.book")
            if last_read_col:
                query_parts.append(f"c_date.value AS last_read")
                joins.append(f"LEFT JOIN {last_read_col[1]} c_date ON b.id = c_date.book")
            if status_col:
                query_parts.append(f"c_stat.value AS read_status")
                joins.append(f"LEFT JOIN {status_col[1]} c_stat ON b.id = c_stat.book")
            if pages_col:
                query_parts.append(f"c_pages.value AS custom_pages")
                joins.append(f"LEFT JOIN {pages_col[1]} c_pages ON b.id = c_pages.book")
            if has_native_pages:
                query_parts.append("bpl.pages AS native_pages")
                joins.append("LEFT JOIN books_pages_link bpl ON b.id = bpl.book")
            elif has_books_pages:
                query_parts.append("b.pages AS native_pages")

            sql = f"{', '.join(query_parts)} FROM books b {' '.join(joins)}"
            cursor = conn.execute(sql)
            rows = cursor.fetchall()

            for r in rows:
                bid = r["id"]
                title = r["title"] or "Untitled"
                authors_str = ", ".join(book_authors.get(bid, []))
                tags_list = book_tags.get(bid, [])

                pct = 0.0
                if read_col and "read_pct" in r.keys() and r["read_pct"] is not None:
                    try:
                        p_val = float(r["read_pct"])
                        pct = p_val / 100.0 if p_val > 1.0 else p_val
                    except (ValueError, TypeError):
                        pct = 0.0

                is_status_read = False
                if status_col and "read_status" in r.keys() and r["read_status"]:
                    is_status_read = bool(r["read_status"])

                is_completed = (pct >= self.mark_read_threshold) or is_status_read
                if is_completed:
                    total_finished_all_time += 1
                elif pct > 0.0:
                    in_progress_count += 1

                # Parse finish date
                date_str = None
                book_yr = None
                book_month = None
                if last_read_col and "last_read" in r.keys() and r["last_read"]:
                    raw_dt = str(r["last_read"])
                    try:
                        # Calibre dates can be 'YYYY-MM-DD HH:MM:SS' or ISO or timestamp
                        if raw_dt.isdigit():
                            dt = datetime.datetime.fromtimestamp(int(raw_dt))
                        else:
                            clean_dt = raw_dt.split("+")[0].split(".")[0].strip()
                            dt = datetime.datetime.fromisoformat(clean_dt)
                        book_yr = dt.year
                        book_month = dt.strftime("%b")
                        date_str = dt.strftime("%Y-%m-%d")
                        available_years.add(book_yr)
                    except Exception:
                        pass

                book_pages = None
                # 1. Custom column first (#pages, #page_count)
                if pages_col and "custom_pages" in r.keys() and r["custom_pages"] is not None:
                    try:
                        val = int(float(r["custom_pages"]))
                        if val > 0:
                            book_pages = val
                    except (ValueError, TypeError):
                        pass

                # 2. Calibre 9.0+ inbuilt books_pages_link or books.pages
                if not book_pages and "native_pages" in r.keys() and r["native_pages"] is not None:
                    try:
                        val = int(float(r["native_pages"]))
                        if val > 0:
                            book_pages = val
                    except (ValueError, TypeError):
                        pass

                if is_completed and book_yr:
                    completed_books.append({
                        "id": bid,
                        "title": title,
                        "authors": authors_str,
                        "tags": tags_list,
                        "year": book_yr,
                        "month": book_month,
                        "date": date_str,
                        "percentage": 100.0,
                        "pages": book_pages,
                    })

        # 2. Also check internal_db tracked_documents to enrich completed books
        if self.internal_db:
            try:
                tracked = self.internal_db.get_all_tracked_documents()
                for d in tracked:
                    d_pct = float(d["percentage"] or 0.0)
                    if d_pct >= self.mark_read_threshold:
                        ts = d["timestamp"]
                        if ts:
                            dt = datetime.datetime.fromtimestamp(ts)
                            available_years.add(dt.year)
                            b_id = d["calibre_book_id"]
                            # Check if already present
                            if not any(b["id"] == b_id for b in completed_books if b_id):
                                b_pages = None
                                if b_id:
                                    try:
                                        with self._get_connection() as c_conn:
                                            if pages_col:
                                                p_row = c_conn.execute(f"SELECT value FROM {pages_col[1]} WHERE book = ?", (b_id,)).fetchone()
                                                if p_row and p_row[0] is not None:
                                                    val = int(float(p_row[0]))
                                                    if val > 0:
                                                        b_pages = val
                                            if not b_pages and has_native_pages:
                                                p_row = c_conn.execute("SELECT pages FROM books_pages_link WHERE book = ?", (b_id,)).fetchone()
                                                if p_row and p_row[0] is not None:
                                                    val = int(float(p_row[0]))
                                                    if val > 0:
                                                        b_pages = val
                                            elif not b_pages and has_books_pages:
                                                p_row = c_conn.execute("SELECT pages FROM books WHERE id = ?", (b_id,)).fetchone()
                                                if p_row and p_row[0] is not None:
                                                    val = int(float(p_row[0]))
                                                    if val > 0:
                                                        b_pages = val
                                    except Exception:
                                        pass
                                completed_books.append({
                                    "id": b_id or d["document"][:8],
                                    "title": d["title"] or "Untitled",
                                    "authors": d["authors"] or "",
                                    "tags": [],
                                    "year": dt.year,
                                    "month": dt.strftime("%b"),
                                    "date": dt.strftime("%Y-%m-%d"),
                                    "percentage": round(d_pct * 100, 1),
                                    "pages": b_pages,
                                })
            except Exception as e:
                logger.debug(f"Error enriching from internal_db: {e}")

        # Always include current year and previous year in available_years
        available_years.add(now_year)
        available_years.add(now_year - 1)
        sorted_years = sorted(list(available_years), reverse=True)

        # Filter completed books for the selected year
        if selected_year == "all" or selected_year == 0:
            year_books = completed_books
            display_year_str = "All Time"
        else:
            try:
                selected_year = int(selected_year)
            except (ValueError, TypeError):
                selected_year = now_year
            year_books = [b for b in completed_books if b["year"] == selected_year]
            display_year_str = str(selected_year)

        # Monthly breakdown (Jan - Dec)
        months_order = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
        monthly_counts = {m: 0 for m in months_order}
        for b in year_books:
            if b.get("month") in monthly_counts:
                monthly_counts[b["month"]] += 1

        # Peak month
        peak_month = "-"
        max_month_books = 0
        for m, cnt in monthly_counts.items():
            if cnt > max_month_books:
                max_month_books = cnt
                peak_month = f"{m} ({cnt} books)"

        # Top Authors read in selected year
        year_authors = {}
        year_tags = {}
        for b in year_books:
            if b.get("authors"):
                for a in [x.strip() for x in b["authors"].split(",")]:
                    if a:
                        year_authors[a] = year_authors.get(a, 0) + 1
            if b.get("tags"):
                for t in b["tags"]:
                    year_tags[t] = year_tags.get(t, 0) + 1

        top_read_authors = [
            {"name": k, "count": v}
            for k, v in sorted(year_authors.items(), key=lambda item: item[1], reverse=True)[:5]
        ]
        top_read_tags = [
            {"name": k, "count": v}
            for k, v in sorted(year_tags.items(), key=lambda item: item[1], reverse=True)[:8]
        ]

        # Pages read: sum actual page counts from Calibre if available, fallback to 320 avg
        total_pages = 0
        books_with_actual_pages = 0
        for b in year_books:
            pgs = b.get("pages")
            if pgs and pgs > 0:
                total_pages += pgs
                books_with_actual_pages += 1
            else:
                total_pages += 320  # Fallback estimate per book without custom page count

        is_exact = (len(year_books) > 0 and books_with_actual_pages == len(year_books))

        return {
            "selected_year": selected_year,
            "display_year": display_year_str,
            "available_years": sorted_years,
            "books_completed": len(year_books),
            "total_finished_all_time": total_finished_all_time,
            "in_progress": in_progress_count,
            "estimated_pages": total_pages,
            "is_exact_pages": is_exact,
            "books_with_page_count": books_with_actual_pages,
            "has_native_pages": has_native_pages or has_books_pages,
            "peak_month": peak_month,
            "monthly_counts": monthly_counts,
            "top_read_authors": top_read_authors,
            "top_read_tags": top_read_tags,
            "books": sorted(year_books, key=lambda b: b.get("date") or "", reverse=True),
        }

