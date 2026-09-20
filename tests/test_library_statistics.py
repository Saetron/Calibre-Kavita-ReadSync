"""Tests for Calibre library statistics and reading history analytics."""

import sqlite3
from pathlib import Path

import pytest

from kosync_hub.clients.calibre_db import CalibreDbClient
from kosync_hub.db import InternalDatabase


def create_rich_mock_calibre_db(library_dir: Path) -> Path:
    """Creates a mock Calibre metadata.db with full tables for analytics testing."""
    db_path = library_dir / "metadata.db"
    conn = sqlite3.connect(str(db_path))

    conn.executescript("""
        CREATE TABLE books (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            path TEXT NOT NULL,
            series_index REAL DEFAULT 1.0,
            pubdate TEXT,
            last_modified TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE authors (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, sort TEXT);
        CREATE TABLE books_authors_link (id INTEGER PRIMARY KEY AUTOINCREMENT, book INTEGER NOT NULL, author INTEGER NOT NULL);
        CREATE TABLE series (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, sort TEXT);
        CREATE TABLE books_series_link (id INTEGER PRIMARY KEY AUTOINCREMENT, book INTEGER NOT NULL, series INTEGER NOT NULL);
        CREATE TABLE tags (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL);
        CREATE TABLE books_tags_link (id INTEGER PRIMARY KEY AUTOINCREMENT, book INTEGER NOT NULL, tag INTEGER NOT NULL);
        CREATE TABLE publishers (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, sort TEXT);
        CREATE TABLE books_publishers_link (id INTEGER PRIMARY KEY AUTOINCREMENT, book INTEGER NOT NULL, publisher INTEGER NOT NULL);
        CREATE TABLE languages (id INTEGER PRIMARY KEY AUTOINCREMENT, lang_code TEXT NOT NULL);
        CREATE TABLE books_languages_link (id INTEGER PRIMARY KEY AUTOINCREMENT, book INTEGER NOT NULL, lang_code INTEGER NOT NULL);
        CREATE TABLE data (id INTEGER PRIMARY KEY AUTOINCREMENT, book INTEGER NOT NULL, format TEXT NOT NULL, uncompressed_size INTEGER, name TEXT NOT NULL);
        CREATE TABLE identifiers (id INTEGER PRIMARY KEY AUTOINCREMENT, book INTEGER NOT NULL, type TEXT NOT NULL, val TEXT NOT NULL);
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
    """)

    # Populate books
    conn.execute("INSERT INTO books (id, title, path, pubdate) VALUES (1, 'Book One', 'Author A/Book One', '2024-01-15')")
    conn.execute("INSERT INTO books (id, title, path, pubdate) VALUES (2, 'Book Two', 'Author A/Book Two', '2025-05-10')")
    conn.execute("INSERT INTO books (id, title, path, pubdate) VALUES (3, 'Book Three', 'Author B/Book Three', '2026-02-20')")

    # Authors
    conn.execute("INSERT INTO authors (id, name) VALUES (1, 'Brandon Sanderson')")
    conn.execute("INSERT INTO authors (id, name) VALUES (2, 'Robert Jordan')")
    conn.execute("INSERT INTO books_authors_link (book, author) VALUES (1, 1)")
    conn.execute("INSERT INTO books_authors_link (book, author) VALUES (2, 1)")
    conn.execute("INSERT INTO books_authors_link (book, author) VALUES (3, 2)")

    # Series
    conn.execute("INSERT INTO series (id, name) VALUES (1, 'The Stormlight Archive')")
    conn.execute("INSERT INTO books_series_link (book, series) VALUES (1, 1)")
    conn.execute("INSERT INTO books_series_link (book, series) VALUES (2, 1)")

    # Tags
    conn.execute("INSERT INTO tags (id, name) VALUES (1, 'Fantasy')")
    conn.execute("INSERT INTO tags (id, name) VALUES (2, 'Sci-Fi')")
    conn.execute("INSERT INTO books_tags_link (book, tag) VALUES (1, 1)")
    conn.execute("INSERT INTO books_tags_link (book, tag) VALUES (2, 1)")
    conn.execute("INSERT INTO books_tags_link (book, tag) VALUES (3, 2)")

    # Publishers
    conn.execute("INSERT INTO publishers (id, name) VALUES (1, 'Tor Books')")
    conn.execute("INSERT INTO publishers (id, name) VALUES (2, 'Orbit')")
    conn.execute("INSERT INTO books_publishers_link (book, publisher) VALUES (1, 1)")
    conn.execute("INSERT INTO books_publishers_link (book, publisher) VALUES (2, 1)")
    conn.execute("INSERT INTO books_publishers_link (book, publisher) VALUES (3, 2)")

    # Languages
    conn.execute("INSERT INTO languages (id, lang_code) VALUES (1, 'eng')")
    conn.execute("INSERT INTO books_languages_link (book, lang_code) VALUES (1, 1)")
    conn.execute("INSERT INTO books_languages_link (book, lang_code) VALUES (2, 1)")
    conn.execute("INSERT INTO books_languages_link (book, lang_code) VALUES (3, 1)")

    # Formats & sizes
    conn.execute("INSERT INTO data (book, format, uncompressed_size, name) VALUES (1, 'EPUB', 2097152, 'Book One')")
    conn.execute("INSERT INTO data (book, format, uncompressed_size, name) VALUES (2, 'EPUB', 3145728, 'Book Two')")
    conn.execute("INSERT INTO data (book, format, uncompressed_size, name) VALUES (3, 'KEPUB', 1048576, 'Book Three')")

    conn.commit()
    conn.close()
    return db_path


@pytest.fixture
def calibre_env(tmp_path):
    lib_dir = tmp_path / "CalibreLibrary"
    lib_dir.mkdir()
    create_rich_mock_calibre_db(lib_dir)

    internal_db_file = tmp_path / "internal.sqlite3"
    internal_db = InternalDatabase(str(internal_db_file))

    client = CalibreDbClient(
        library_path=str(lib_dir),
        auto_create_columns=True,
        read_pct_column="readpct",
        read_status_column="readstatus",
        last_read_column="lastread",
        progress_column="koreader_progress",
        internal_db=internal_db,
    )
    return client, internal_db


def test_get_library_statistics(calibre_env):
    client, _ = calibre_env
    stats = client.get_library_statistics()

    assert stats["total_books"] == 3
    assert stats["total_authors"] == 2
    assert stats["total_series"] == 1
    assert stats["total_tags"] == 2
    assert stats["total_publishers"] == 2
    assert stats["total_languages"] == 1
    assert stats["total_size_bytes"] > 0
    assert "total_size_gb" in stats

    # Top authors
    assert len(stats["top_authors"]) >= 1
    assert stats["top_authors"][0]["name"] == "Brandon Sanderson"
    assert stats["top_authors"][0]["count"] == 2

    # Top series
    assert len(stats["top_series"]) == 1
    assert stats["top_series"][0]["name"] == "The Stormlight Archive"
    assert stats["top_series"][0]["count"] == 2

    # Top publishers
    assert len(stats["top_publishers"]) == 2
    assert stats["top_publishers"][0]["name"] == "Tor Books"
    assert stats["top_publishers"][0]["count"] == 2

    # Languages
    assert len(stats["languages"]) == 1
    assert stats["languages"][0]["code"] == "eng"
    assert stats["languages"][0]["count"] == 3

    # Formats
    format_names = [f["format"] for f in stats["formats"]]
    assert "EPUB" in format_names
    assert "KEPUB" in format_names

    # Read-only lists exist
    assert "top_authors_read" in stats
    assert "top_series_read" in stats
    assert "top_publishers_read" in stats


@pytest.mark.asyncio
async def test_reading_history_analytics(calibre_env):
    client, internal_db = calibre_env
    await client.test_connection()

    pct_col = client._resolve_column(client.read_pct_label)
    status_col = client._resolve_column(client.read_status_label)
    last_read_col = client._resolve_column(client.last_read_label)

    assert pct_col is not None
    assert status_col is not None
    assert last_read_col is not None

    with client._get_connection() as conn:
        pct_tbl = pct_col[1]
        status_tbl = status_col[1]
        last_read_tbl = last_read_col[1]

        # Book 1: 100% read in 2026-03-15
        conn.execute(f"INSERT OR REPLACE INTO {pct_tbl} (book, value) VALUES (1, 100.0)")
        conn.execute(f"INSERT OR REPLACE INTO {status_tbl} (book, value) VALUES (1, 1)")
        conn.execute(f"INSERT OR REPLACE INTO {last_read_tbl} (book, value) VALUES (1, '2026-03-15 14:00:00')")

        # Book 2: 100% read in 2026-03-20
        conn.execute(f"INSERT OR REPLACE INTO {pct_tbl} (book, value) VALUES (2, 100.0)")
        conn.execute(f"INSERT OR REPLACE INTO {status_tbl} (book, value) VALUES (2, 1)")
        conn.execute(f"INSERT OR REPLACE INTO {last_read_tbl} (book, value) VALUES (2, '2026-03-20 18:30:00')")

        # Book 3: 100% read in 2025-11-05
        conn.execute(f"INSERT OR REPLACE INTO {pct_tbl} (book, value) VALUES (3, 100.0)")
        conn.execute(f"INSERT OR REPLACE INTO {status_tbl} (book, value) VALUES (3, 1)")
        conn.execute(f"INSERT OR REPLACE INTO {last_read_tbl} (book, value) VALUES (3, '2025-11-05 09:15:00')")
        conn.commit()

    # Query 2026
    analytics_2026 = client.get_reading_history_analytics(year=2026)
    assert analytics_2026["selected_year"] == 2026
    assert analytics_2026["books_completed"] == 2
    assert analytics_2026["monthly_counts"]["Mar"] == 2
    assert analytics_2026["peak_month"] == "Mar (2 books)"
    assert len(analytics_2026["books"]) == 2
    assert 2026 in analytics_2026["available_years"]
    assert 2025 in analytics_2026["available_years"]

    # Query All-time
    analytics_all = client.get_reading_history_analytics(year="all")
    assert analytics_all["selected_year"] == "all"
    assert analytics_all["books_completed"] == 3
