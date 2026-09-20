import pytest
from kosync_hub.vfs.calibre_db import BookFileRecord
from kosync_hub.vfs.state import VFSState, calculate_units


def test_calculate_units():
    assert calculate_units(None) == 0.0
    assert calculate_units("") == 0.0
    assert calculate_units(1) == 1.0
    assert calculate_units(1.0) == 1.0
    assert calculate_units("1") == 1.0
    assert calculate_units("10.5") == 1.0
    assert calculate_units("Special") == 1.0

    # Ranges
    assert calculate_units("1-3") == 3.0
    assert calculate_units("1 - 3") == 3.0
    assert calculate_units("1-25") == 25.0
    assert calculate_units("Vol. 1-5") == 5.0
    assert calculate_units("Ch. 1-10") == 10.0


def test_state_update_and_summary():
    state = VFSState()

    records = [
        BookFileRecord(
            book_id=1,
            title="Berserk 1-3",
            series="Berserk",
            language="eng",
            type_="Manga",
            volume="1-3",
            chapter=None,
            format="CBZ",
            source_path="/calibre/Berserk 1.cbz",
        ),
        BookFileRecord(
            book_id=2,
            title="Berserk 4",
            series="Berserk",
            language="eng",
            type_="Manga",
            volume=4,
            chapter="1-15",
            format="CBZ",
            source_path="/calibre/Berserk 4.cbz",
        ),
        BookFileRecord(
            book_id=3,
            title="Solo Leveling",
            series=None,
            language="eng",
            type_="Comic",
            volume=None,
            chapter=100,
            format="EPUB",
            source_path="/calibre/Solo.epub",
        ),
    ]

    desired_map = {
        "/vfs/eng/Manga/Berserk/Berserk Vol. 1-3 {1}.cbz": "/calibre/Berserk 1.cbz",
        "/vfs/eng/Manga/Berserk/Berserk Vol. 4 Ch. 1-15 {2}.cbz": "/calibre/Berserk 4.cbz",
        "/vfs/eng/Comic/Solo Leveling/Solo Leveling Ch. 100 {3}.epub": "/calibre/Solo.epub",
    }

    collisions = [
        {
            "relpath": "eng/Manga/Berserk/Berserk {1}.cbz",
            "existing_book_id": 1,
            "existing_source": "/calibre/1.cbz",
            "colliding_book_id": 99,
            "colliding_source": "/calibre/99.cbz",
            "resolved_path": "eng/Manga/Berserk/Berserk {1}_collision_1.cbz",
        }
    ]

    state.update_sync_results(
        records=records,
        desired_map=desired_map,
        collisions=collisions,
        mode="hardlink",
        calibre_dir="/calibre",
        vfs_dir="/vfs",
    )

    summary = state.get_summary()
    assert summary["total_books"] == 3
    assert summary["total_series"] == 2  # Berserk + Solo Leveling
    # Volumes: 1-3 (3) + 4 (1) = 4
    assert summary["total_volumes"] == 4
    # Chapters: 1-15 (15) + 100 (1) = 16
    assert summary["total_chapters"] == 16
    assert summary["books_with_volume"] == 2
    assert summary["books_with_chapter"] == 2
    assert summary["collision_count"] == 1
    assert len(summary["collisions"]) == 1
    assert summary["type_counts"]["Manga"] == 2
    assert summary["type_counts"]["Comic"] == 1

    items_resp = state.get_items(query="berserk")
    assert items_resp["total"] == 2
