import sqlite3
import tempfile
from pathlib import Path
import pytest
from kosync_hub.clients.calibre_db import CalibreDbClient, normalize_string
from kosync_hub.models import ProgressRecord


def create_mock_calibre_db(library_dir: Path) -> Path:
    """Creates a mock Calibre metadata.db with standard tables and sample books."""
    db_path = library_dir / "metadata.db"
    conn = sqlite3.connect(str(db_path))

    conn.executescript("""
        CREATE TABLE books (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            path TEXT NOT NULL,
            series_index REAL DEFAULT 1.0,
            last_modified TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE authors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            sort TEXT
        );

        CREATE TABLE books_authors_link (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            book INTEGER NOT NULL,
            author INTEGER NOT NULL
        );

        CREATE TABLE series (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            sort TEXT
        );

        CREATE TABLE books_series_link (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            book INTEGER NOT NULL,
            series INTEGER NOT NULL
        );

        CREATE TABLE data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            book INTEGER NOT NULL,
            format TEXT NOT NULL,
            uncompressed_size INTEGER,
            name TEXT NOT NULL
        );

        CREATE TABLE identifiers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            book INTEGER NOT NULL,
            type TEXT NOT NULL,
            val TEXT NOT NULL
        );

        CREATE TABLE custom_columns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT NOT NULL,
            name TEXT NOT NULL,
            datatype TEXT NOT NULL,
            mark_for_delete INTEGER DEFAULT 0,
            editable INTEGER DEFAULT 1,
            display TEXT DEFAULT '{}',
            is_multiple INTEGER DEFAULT 0,
            normalized INTEGER DEFAULT 0
        );

        -- Insert sample book 1: Dune
        INSERT INTO books (id, title, path) VALUES (1, 'Dune', 'Frank Herbert/Dune (1)');
        INSERT INTO authors (id, name) VALUES (1, 'Frank Herbert');
        INSERT INTO books_authors_link (book, author) VALUES (1, 1);
        INSERT INTO data (book, format, name) VALUES (1, 'EPUB', 'Dune - Frank Herbert');
        INSERT INTO identifiers (book, type, val) VALUES (1, 'koreader', 'dune_hash_12345');

        -- Insert sample book 2: The Hobbit
        INSERT INTO books (id, title, path) VALUES (2, 'The Hobbit: An Unexpected Journey', 'J.R.R. Tolkien/The Hobbit (2)');
        INSERT INTO authors (id, name) VALUES (2, 'J.R.R. Tolkien');
        INSERT INTO books_authors_link (book, author) VALUES (2, 2);
        INSERT INTO data (book, format, name) VALUES (2, 'EPUB', 'The Hobbit - J.R.R. Tolkien');

        -- Insert sample book 3: High School DxD (series: High School DxD, index: 1.0, id: 56134)
        INSERT INTO books (id, title, path, series_index) VALUES (56134, 'High School DxD, Vol. 1', 'Ichiei Ishibumi/High School DxD (56134)', 1.0);
        INSERT INTO authors (id, name) VALUES (3, 'Ichiei Ishibumi');
        INSERT INTO books_authors_link (book, author) VALUES (56134, 3);
        INSERT INTO series (id, name) VALUES (10, 'High School DxD');
        INSERT INTO books_series_link (book, series) VALUES (56134, 10);
        INSERT INTO data (book, format, name) VALUES (56134, 'EPUB', 'High School DxD Vol 1 - Ichiei Ishibumi');
        INSERT INTO identifiers (book, type, val) VALUES (56134, 'koreader', 'dxd_canonical_hash_9876');

        -- Add Calibre-like triggers that call title_sort and uuid4
        CREATE TRIGGER books_update_trg AFTER UPDATE ON books
        BEGIN
            UPDATE books SET title=title_sort(NEW.title) WHERE id=NEW.id AND OLD.title <> NEW.title;
        END;
    """)
    conn.commit()
    conn.close()
    return db_path


@pytest.mark.asyncio
async def test_calibre_custom_columns_and_progress():
    with tempfile.TemporaryDirectory() as tmpdir:
        lib_dir = Path(tmpdir)
        create_mock_calibre_db(lib_dir)

        client = CalibreDbClient(
            library_path=str(lib_dir),
            read_pct_column="#read_pct",
            read_status_column="#read_status",
            auto_create_columns=True,
        )

        # 1. Test connection & auto-column creation
        ok = await client.test_connection()
        assert ok is True

        # Verify custom columns exist
        conn = sqlite3.connect(str(lib_dir / "metadata.db"))
        cursor = conn.execute("SELECT label, id FROM custom_columns")
        cols = {r[0]: r[1] for r in cursor.fetchall()}
        assert "read_pct" in cols
        assert "read_status" in cols
        assert "last_read" in cols
        assert "koreader_progress" in cols
        conn.close()

        # 2. Test updating progress for Book 1 (matched by pre-existing identifier)
        record = ProgressRecord(
            document="dune_hash_12345",
            progress="/6/4[chapter1]!/4/2/1:0",
            percentage=0.55,
            title="Dune",
            authors="Frank Herbert",
        )
        updated = await client.update_progress(record)
        assert updated is True

        # Check stored progress
        retrieved = await client.get_progress("dune_hash_12345")
        assert retrieved is not None
        assert abs(retrieved.percentage - 0.55) < 0.01

        # Check read status is not finished yet
        conn = sqlite3.connect(str(lib_dir / "metadata.db"))
        pct_col_id = cols["read_pct"]
        cursor = conn.execute(f"SELECT value FROM custom_column_{pct_col_id} WHERE book = 1")
        assert cursor.fetchone()[0] == 55.0  # 55.0%

        status_col_id = cols["read_status"]
        cursor = conn.execute(f"SELECT value FROM custom_column_{status_col_id} WHERE book = 1")
        assert cursor.fetchone()[0] == 0  # Not finished (< 0.98)
        conn.close()

        # 3. Test finishing book (>= 0.98)
        finish_record = ProgressRecord(
            document="dune_hash_12345",
            progress="/6/10[epilogue]!/4/2:0",
            percentage=1.0,
        )
        await client.update_progress(finish_record)
        conn = sqlite3.connect(str(lib_dir / "metadata.db"))
        cursor = conn.execute(f"SELECT value FROM custom_column_{status_col_id} WHERE book = 1")
        assert cursor.fetchone()[0] == 1  # Finished!
        conn.close()


@pytest.mark.asyncio
async def test_calibre_book_matching_fallback():
    with tempfile.TemporaryDirectory() as tmpdir:
        lib_dir = Path(tmpdir)
        create_mock_calibre_db(lib_dir)

        client = CalibreDbClient(library_path=str(lib_dir))
        await client.test_connection()

        # Book 2 ("The Hobbit") does not have a 'koreader' identifier initially
        new_hash = "hobbit_new_hash_9999"
        record = ProgressRecord(
            document=new_hash,
            progress="page:42",
            percentage=0.35,
            title="The Hobbit",
            authors="J.R.R. Tolkien",
            filename="The Hobbit - J.R.R. Tolkien.epub",
        )

        updated = await client.update_progress(record)
        assert updated is True

        # Verify that Book 2 was matched and linked to new_hash in identifiers table
        assert client.find_book_by_hash(new_hash) == 2


def test_normalize_string():
    assert normalize_string("Dune: A Novel") == "dune"
    assert normalize_string("The Hobbit - Special Edition") == "the hobbit"
    assert normalize_string("F. Scott Fitzgerald") == "f scott fitzgerald"


@pytest.mark.asyncio
async def test_calibre_filename_hash_lookup():
    with tempfile.TemporaryDirectory() as tmpdir:
        lib_dir = Path(tmpdir)
        create_mock_calibre_db(lib_dir)

        from kosync_hub.hasher import compute_filename_md5
        client = CalibreDbClient(library_path=str(lib_dir))
        await client.test_connection()

        # 1. Matches exact Calibre data filename "Dune - Frank Herbert.epub"
        h1 = compute_filename_md5("Dune - Frank Herbert.epub")
        assert client.find_book_by_filename_hash(h1) == 1

        # 2. Matches folder name with format "Dune (1).epub"
        h2 = compute_filename_md5("Dune (1).epub")
        assert client.find_book_by_filename_hash(h2) == 1

        # 3. Matches title with ID in curly braces "Dune {1}.epub"
        h3 = compute_filename_md5("Dune {1}.epub")
        assert client.find_book_by_filename_hash(h3) == 1

        # 4. Matches just title "Dune.epub"
        h4 = compute_filename_md5("Dune.epub")
        assert client.find_book_by_filename_hash(h4) == 1

        # 5. Matches user series template: series/series - series_index {id}
        # Exact hash from user's logs: 49e2f5c0f6f08f860a5268fa6b518623
        h_dxd = compute_filename_md5("High School DxD - 1 {56134}.epub")
        assert h_dxd == "49e2f5c0f6f08f860a5268fa6b518623"
        assert client.find_book_by_filename_hash("49e2f5c0f6f08f860a5268fa6b518623") == 56134
        assert client.find_book_by_hash("49e2f5c0f6f08f860a5268fa6b518623") == 56134

        # Also matches full path with folder "High School DxD/High School DxD - 1 {56134}.epub"
        import hashlib
        h_dxd_full = hashlib.md5("High School DxD/High School DxD - 1 {56134}.epub".encode("utf-8")).hexdigest()
        assert client.find_book_by_filename_hash(h_dxd_full) == 56134

        # 6. Non-existent book returns None
        h_none = compute_filename_md5("Unknown Book 999.epub")
        assert client.find_book_by_filename_hash(h_none) is None


@pytest.mark.asyncio
async def test_calibre_filename_hashes_stored_in_internal_db():
    """Verifies that filename hashes are persisted to internal.db, enabling instant lookups and incremental updates."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        lib_dir = tmp / "calibre"
        lib_dir.mkdir()
        create_mock_calibre_db(lib_dir)

        from kosync_hub.db import InternalDatabase
        internal_db = InternalDatabase(str(tmp / "internal.db"))

        assert internal_db.count_filename_hashes() == 0

        client = CalibreDbClient(library_path=str(lib_dir), internal_db=internal_db)
        await client.test_connection()

        # 1. First run indexes and populates internal_db
        count = client.index_filename_hashes(internal_db, incremental=False)
        assert count > 0
        total_stored = internal_db.count_filename_hashes()
        assert total_stored > 0

        # 2. Lookups directly via internal_db
        assert internal_db.get_calibre_id_by_filename_hash("49e2f5c0f6f08f860a5268fa6b518623") == 56134
        assert internal_db.get_calibre_id_for_document("49e2f5c0f6f08f860a5268fa6b518623") == 56134

        # 3. Incremental run should see no new books and return 0
        inc_count = client.index_filename_hashes(internal_db, incremental=True)
        assert inc_count == 0

        # 4. A new client instance with completely empty in-memory cache finds hash instantly via internal_db
        fresh_client = CalibreDbClient(library_path=str(lib_dir), internal_db=internal_db)
        assert len(fresh_client._filename_hash_cache) == 0
        assert fresh_client.find_book_by_filename_hash("49e2f5c0f6f08f860a5268fa6b518623") == 56134


@pytest.mark.asyncio
async def test_calibre_read_percentage_scale():
    """Verifies that Calibre #read_pct (0-100 scale) is correctly converted to 0.0-1.0 ratio."""
    with tempfile.TemporaryDirectory() as tmpdir:
        lib_dir = Path(tmpdir)
        create_mock_calibre_db(lib_dir)

        client = CalibreDbClient(library_path=str(lib_dir))
        await client.test_connection()

        conn = sqlite3.connect(str(lib_dir / "metadata.db"))
        cursor = conn.execute("SELECT label, id FROM custom_columns")
        cols = {r[0]: r[1] for r in cursor.fetchall()}
        pct_col_id = cols["read_pct"]
        status_col_id = cols["read_status"]

        # Test 1: Value 1.0 in Calibre means 1%, NOT 100%!
        conn.execute(f"INSERT OR REPLACE INTO custom_column_{pct_col_id} (book, value) VALUES (1, 1.0)")
        conn.execute(f"INSERT OR REPLACE INTO custom_column_{status_col_id} (book, value) VALUES (1, 0)")
        conn.commit()
        conn.close()

        book = client.get_book_by_id(1)
        assert book is not None
        assert abs(book.percentage - 0.01) < 0.001  # Must be 1% (0.01), not 1.0!
        assert book.is_read is False

        # Test 2: Value 0.5 in Calibre means 0.5% (0.005)
        conn = sqlite3.connect(str(lib_dir / "metadata.db"))
        conn.execute(f"UPDATE custom_column_{pct_col_id} SET value = 0.5 WHERE book = 1")
        conn.commit()
        conn.close()

        book = client.get_book_by_id(1)
        assert book is not None
        assert abs(book.percentage - 0.005) < 0.0001

        # Test 3: Value 100.0 in Calibre means 100% (1.0)
        conn = sqlite3.connect(str(lib_dir / "metadata.db"))
        conn.execute(f"UPDATE custom_column_{pct_col_id} SET value = 100.0 WHERE book = 1")
        conn.execute(f"UPDATE custom_column_{status_col_id} SET value = 1 WHERE book = 1")
        conn.commit()
        conn.close()

        book = client.get_book_by_id(1)
        assert book is not None
        assert book.percentage == 1.0
        assert book.is_read is True

        # Test 4: If user has 100% progress but explicitly unchecked read status (read_status = 0 / False),
        # book.percentage must be capped below mark_read_threshold (0.98) so it's not marked finished in Kavita
        conn = sqlite3.connect(str(lib_dir / "metadata.db"))
        conn.execute(f"UPDATE custom_column_{status_col_id} SET value = 0 WHERE book = 1")
        conn.commit()
        conn.close()

        book = client.get_book_by_id(1)
        assert book is not None
        assert book.percentage < 0.98
        assert book.is_read is False

