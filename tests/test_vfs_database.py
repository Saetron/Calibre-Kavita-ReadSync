import os
import tempfile
import pytest
from kosync_hub.vfs.database import VFSDatabase


@pytest.fixture
def vfs_db():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test_vfs.db")
        db = VFSDatabase(db_path)
        yield db
        db.close()


def test_init_and_tracked_map(vfs_db):
    tracked = vfs_db.get_tracked_map()
    assert tracked == {}


def test_upsert_and_query(vfs_db):
    entries = [
        {
            "vfs_path": "/vfs/eng/Manga/One Piece/One Piece Vol. 1 {1}.cbz",
            "vfs_relpath": "eng/Manga/One Piece/One Piece Vol. 1 {1}.cbz",
            "source_path": "/calibre/author/book1.cbz",
            "book_id": 1,
            "title": "One Piece 1",
            "series": "One Piece",
            "volume": "1",
            "chapter": "",
            "type": "Manga",
            "language": "eng",
            "format": "CBZ",
            "link_type": "symlink",
            "target_source": "/calibre/author/book1.cbz",
            "last_synced": 1000.0,
        },
        {
            "vfs_path": "/vfs/eng/Manga/One Piece/One Piece Vol. 2 {2}.cbz",
            "vfs_relpath": "eng/Manga/One Piece/One Piece Vol. 2 {2}.cbz",
            "source_path": "/calibre/author/book2.cbz",
            "book_id": 2,
            "title": "One Piece 2",
            "series": "One Piece",
            "volume": "2",
            "chapter": "",
            "type": "Manga",
            "language": "eng",
            "format": "CBZ",
            "link_type": "symlink",
            "target_source": "/calibre/author/book2.cbz",
            "last_synced": 1000.0,
        },
        {
            "vfs_path": "/vfs/eng/Comic/Batman/Batman Ch. 5 {10}.cbr",
            "vfs_relpath": "eng/Comic/Batman/Batman Ch. 5 {10}.cbr",
            "source_path": "/calibre/author/batman.cbr",
            "book_id": 10,
            "title": "Batman Year One",
            "series": "Batman",
            "volume": "",
            "chapter": "5",
            "type": "Comic",
            "language": "eng",
            "format": "CBR",
            "link_type": "symlink",
            "target_source": "/calibre/author/batman.cbr",
            "last_synced": 1000.0,
        },
    ]
    vfs_db.upsert_entries(entries)

    tracked = vfs_db.get_tracked_map()
    assert len(tracked) == 3
    assert "/vfs/eng/Manga/One Piece/One Piece Vol. 1 {1}.cbz" in tracked

    # Pagination & Query tests
    all_res = vfs_db.query_items(limit=2, offset=0)
    assert all_res["total"] == 3
    assert len(all_res["items"]) == 2
    assert all_res["items"][0]["book_id"] == 1
    assert all_res["items"][1]["book_id"] == 2

    page2 = vfs_db.query_items(limit=2, offset=2)
    assert len(page2["items"]) == 1
    assert page2["items"][0]["book_id"] == 10

    # Search query tests
    batman_res = vfs_db.query_items(query="batman")
    assert batman_res["total"] == 1
    assert batman_res["items"][0]["series"] == "Batman"

    id_res = vfs_db.query_items(query="10")
    assert id_res["total"] == 1
    assert id_res["items"][0]["book_id"] == 10

    # Summary stats test
    stats = vfs_db.get_summary_stats()
    assert stats["total_books"] == 3
    assert stats["total_series"] == 2
    assert stats["total_volumes"] == 3.0  # 1 + 2
    assert stats["total_chapters"] == 5.0  # 5
    assert stats["type_counts"]["Manga"] == 2
    assert stats["type_counts"]["Comic"] == 1


def test_delete_and_clear(vfs_db):
    entries = [
        {
            "vfs_path": "/vfs/p1.cbz",
            "vfs_relpath": "p1.cbz",
            "source_path": "/calibre/p1.cbz",
            "book_id": 1,
            "title": "T1",
            "series": "",
            "volume": "",
            "chapter": "",
            "type": "",
            "language": "",
            "format": "CBZ",
            "link_type": "symlink",
            "target_source": "/calibre/p1.cbz",
            "last_synced": 1000.0,
        },
        {
            "vfs_path": "/vfs/p2.cbz",
            "vfs_relpath": "p2.cbz",
            "source_path": "/calibre/p2.cbz",
            "book_id": 2,
            "title": "T2",
            "series": "",
            "volume": "",
            "chapter": "",
            "type": "",
            "language": "",
            "format": "CBZ",
            "link_type": "symlink",
            "target_source": "/calibre/p2.cbz",
            "last_synced": 1000.0,
        },
    ]
    vfs_db.upsert_entries(entries)
    assert len(vfs_db.get_tracked_map()) == 2

    vfs_db.delete_entries(["/vfs/p1.cbz"])
    tracked = vfs_db.get_tracked_map()
    assert len(tracked) == 1
    assert "/vfs/p1.cbz" not in tracked

    vfs_db.clear()
    assert len(vfs_db.get_tracked_map()) == 0
