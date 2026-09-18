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
