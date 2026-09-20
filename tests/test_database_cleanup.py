"""Tests for SQLite database cleanup, pruning, and distinct devices counting."""

import sqlite3
import time
from pathlib import Path

import pytest

from kosync_hub.db import InternalDatabase
from kosync_hub.models import ProgressRecord


@pytest.fixture
def temp_db(tmp_path):
    db_file = tmp_path / "test_cleanup.sqlite3"
    db = InternalDatabase(str(db_file))
    return db


def test_database_size_and_table_counts(temp_db):
    counts = temp_db.get_table_counts()
    assert "filename_hashes" in counts
    assert "sync_events" in counts
    assert "tracked_documents" in counts
    assert "document_aliases" in counts
    assert counts["filename_hashes"] == 0

    size = temp_db.get_database_size_bytes()
    assert size > 0


def test_cleanup_sync_events(temp_db):
    now = int(time.time())
    one_day = 86400

    # Insert 10 events: 5 recent, 5 older than 15 days
    with temp_db._get_connection() as conn:
        for i in range(5):
            conn.execute(
                """INSERT INTO sync_events (document, calibre_id, source, target, progress, percentage, success, message, timestamp)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (f"doc_old_{i}", i, "hub", "calibre", "1.0", 1.0, 1, "synced", now - (20 * one_day)),
            )
        for i in range(5):
            conn.execute(
                """INSERT INTO sync_events (document, calibre_id, source, target, progress, percentage, success, message, timestamp)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (f"doc_new_{i}", i + 10, "hub", "calibre", "1.0", 1.0, 1, "synced", now - (2 * one_day)),
            )
        conn.commit()

    assert temp_db.get_table_counts()["sync_events"] == 10

    # Prune older than 14 days
    deleted = temp_db.cleanup_sync_events(retention_days=14, max_events=100)
    assert deleted == 5
    assert temp_db.get_table_counts()["sync_events"] == 5

    # Test max_events cap
    deleted_cap = temp_db.cleanup_sync_events(retention_days=30, max_events=2)
    assert deleted_cap == 3
    assert temp_db.get_table_counts()["sync_events"] == 2


def test_clear_filename_hashes(temp_db):
    with temp_db._get_connection() as conn:
        conn.execute("INSERT INTO filename_hashes (hash, calibre_id) VALUES (?, ?)", ("h1", 101))
        conn.execute("INSERT INTO filename_hashes (hash, calibre_id) VALUES (?, ?)", ("h2", 102))
        conn.commit()

    assert temp_db.get_table_counts()["filename_hashes"] == 2
    deleted = temp_db.clear_filename_hashes()
    assert deleted == 2
    assert temp_db.get_table_counts()["filename_hashes"] == 0


def test_cleanup_all_and_vacuum(temp_db):
    with temp_db._get_connection() as conn:
        conn.execute("INSERT INTO filename_hashes (hash, calibre_id) VALUES (?, ?)", ("h1", 101))
        conn.commit()

    res = temp_db.cleanup_all(retention_days=14, max_events=100, clear_hashes=True, vacuum_db=True)
    assert res["status"] == "success"
    assert res["hashes_deleted"] == 1
    assert "size_initial_mb" in res
    assert "size_final_mb" in res
    assert "reclaimed_mb" in res


def test_distinct_devices_count(temp_db):
    # Track documents from different devices and same device multiple times
    temp_db.upsert_progress(
        ProgressRecord(
            document="doc1",
            percentage=0.5,
            progress="50",
            device="Kindle Paperwhite",
        )
    )
    temp_db.upsert_progress(
        ProgressRecord(
            document="doc2",
            percentage=0.8,
            progress="80",
            device="Kindle Paperwhite",
        )
    )
    temp_db.upsert_progress(
        ProgressRecord(
            document="doc3",
            percentage=0.2,
            progress="20",
            device="Kobo Clara",
        )
    )
    # Alias on existing device and new device
    temp_db.link_document_alias("doc1_alias", 10, "Kindle Paperwhite")
    temp_db.link_document_alias("doc4_alias", 20, "Xteink X3")

    stats = temp_db.get_reading_stats()
    assert stats["devices_count"] == 3
    assert set(stats["device_names"]) == {"Kindle Paperwhite", "Kobo Clara", "Xteink X3"}
