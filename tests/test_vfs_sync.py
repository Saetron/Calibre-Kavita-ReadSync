import os
import tempfile
import pytest

from kosync_hub.vfs.calibre_db import BookFileRecord
from kosync_hub.vfs.database import VFSDatabase
from kosync_hub.vfs.symlink import SymlinkVFS


@pytest.fixture
def sync_env():
    with tempfile.TemporaryDirectory() as temp_calibre, tempfile.TemporaryDirectory() as temp_vfs:
        source1 = os.path.join(temp_calibre, "book1.cbz")
        with open(source1, "w") as f:
            f.write("content 1")

        source2 = os.path.join(temp_calibre, "book2.cbz")
        with open(source2, "w") as f:
            f.write("content 2")

        yield {
            "calibre_dir": temp_calibre,
            "vfs_dir": temp_vfs,
            "source1": source1,
            "source2": source2,
        }


def test_sync_creation_and_pruning(sync_env):
    vfs = SymlinkVFS(
        vfs_dir=sync_env["vfs_dir"],
        link_type="symlink",
        default_language="eng",
        default_type="Manga",
    )

    records = [
        BookFileRecord(
            book_id=1,
            title="Naruto 1",
            series="Naruto",
            language="eng",
            type_="Manga",
            volume=1,
            chapter=1,
            format="CBZ",
            source_path=sync_env["source1"],
        ),
        BookFileRecord(
            book_id=2,
            title="Naruto 2",
            series="Naruto",
            language="eng",
            type_="Manga",
            volume=1,
            chapter=2,
            format="CBZ",
            source_path=sync_env["source2"],
        ),
    ]

    created, updated, deleted = vfs.sync(records)
    assert created == 2
    assert updated == 0
    assert deleted == 0

    file1 = os.path.join(sync_env["vfs_dir"], "eng/Manga/Naruto/Naruto Vol. 1 Ch. 1 {1}.cbz")
    file2 = os.path.join(sync_env["vfs_dir"], "eng/Manga/Naruto/Naruto Vol. 1 Ch. 2 {2}.cbz")

    assert os.path.islink(file1)
    assert os.path.islink(file2)
    assert os.path.realpath(file1) == os.path.realpath(sync_env["source1"])
    assert os.path.realpath(file2) == os.path.realpath(sync_env["source2"])

    # Re-run sync with same records: no change
    created, updated, deleted = vfs.sync(records)
    assert created == 0
    assert updated == 0
    assert deleted == 0

    # Prune book 2
    created, updated, deleted = vfs.sync([records[0]])
    assert created == 0
    assert updated == 0
    assert deleted == 1
    assert not os.path.exists(file2)
    assert os.path.exists(file1)


def test_hardlink_creation_and_sync(sync_env):
    vfs = SymlinkVFS(
        vfs_dir=sync_env["vfs_dir"],
        link_type="hardlink",
        default_language="eng",
        default_type="Manga",
    )

    record = BookFileRecord(
        book_id=1,
        title="Naruto 1",
        series="Naruto",
        language="eng",
        type_="Manga",
        volume=1,
        chapter=1,
        format="CBZ",
        source_path=sync_env["source1"],
    )

    created, updated, deleted = vfs.sync([record])
    assert created == 1

    file1 = os.path.join(sync_env["vfs_dir"], "eng/Manga/Naruto/Naruto Vol. 1 Ch. 1 {1}.cbz")
    assert os.path.exists(file1)
    assert not os.path.islink(file1)
    assert os.stat(file1).st_ino == os.stat(sync_env["source1"]).st_ino


def test_relative_symlinks(sync_env):
    vfs = SymlinkVFS(
        vfs_dir=sync_env["vfs_dir"],
        link_type="symlink",
        relative_links=True,
        default_language="eng",
        default_type="Manga",
    )

    record = BookFileRecord(
        book_id=1,
        title="Naruto 1",
        series="Naruto",
        language="eng",
        type_="Manga",
        volume=1,
        chapter=1,
        format="CBZ",
        source_path=sync_env["source1"],
    )

    vfs.sync([record])
    target = os.path.join(sync_env["vfs_dir"], "eng/Manga/Naruto/Naruto Vol. 1 Ch. 1 {1}.cbz")
    assert os.path.islink(target)
    link_dest = os.readlink(target)
    assert not os.path.isabs(link_dest)
    assert os.path.realpath(target) == os.path.realpath(sync_env["source1"])


def test_cleanup_unregistered_and_mode_switching(sync_env):
    vfs_sym = SymlinkVFS(
        vfs_dir=sync_env["vfs_dir"],
        link_type="symlink",
        default_language="eng",
        default_type="Manga",
    )
    record = BookFileRecord(
        book_id=1,
        title="Naruto 1",
        series="Naruto",
        language="eng",
        type_="Manga",
        volume=1,
        chapter=1,
        format="CBZ",
        source_path=sync_env["source1"],
    )
    vfs_sym.sync([record])
    target = os.path.join(sync_env["vfs_dir"], "eng/Manga/Naruto/Naruto Vol. 1 Ch. 1 {1}.cbz")
    assert os.path.islink(target)

    # Add orphan file and directory
    stale_dir = os.path.join(sync_env["vfs_dir"], "old_folder")
    os.makedirs(stale_dir, exist_ok=True)
    stale_file = os.path.join(stale_dir, "orphan.cbz")
    with open(stale_file, "w") as f:
        f.write("orphan data")

    # Switch to hardlink mode and cleanup
    vfs_hard = SymlinkVFS(
        vfs_dir=sync_env["vfs_dir"],
        link_type="hardlink",
        default_language="eng",
        default_type="Manga",
    )
    res = vfs_hard.cleanup_unregistered([record])
    assert res["removed_count"] == 1
    assert not os.path.exists(stale_file)
    assert not os.path.exists(stale_dir)

    # Converted to hardlink
    assert os.path.exists(target)
    assert not os.path.islink(target)
    assert os.stat(target).st_ino == os.stat(sync_env["source1"]).st_ino


def test_delta_sync_with_database(sync_env):
    db_file = os.path.join(sync_env["vfs_dir"], ".vfs_cache.db")
    db = VFSDatabase(db_file)

    vfs = SymlinkVFS(
        vfs_dir=sync_env["vfs_dir"],
        link_type="symlink",
        default_language="eng",
        default_type="Manga",
        db=db,
    )

    record1 = BookFileRecord(
        book_id=1,
        title="Naruto 1",
        series="Naruto",
        language="eng",
        type_="Manga",
        volume=1,
        chapter=1,
        format="CBZ",
        source_path=sync_env["source1"],
    )

    c, u, d = vfs.sync([record1])
    assert (c, u, d) == (1, 0, 0)
    assert len(db.get_tracked_map()) == 1

    # Repeat with no changes
    c2, u2, d2 = vfs.sync([record1])
    assert (c2, u2, d2) == (0, 0, 0)

    # Add record2
    record2 = BookFileRecord(
        book_id=2,
        title="Naruto 2",
        series="Naruto",
        language="eng",
        type_="Manga",
        volume=2,
        chapter=2,
        format="CBZ",
        source_path=sync_env["source2"],
    )
    c3, u3, d3 = vfs.sync([record1, record2])
    assert (c3, u3, d3) == (1, 0, 0)
    assert len(db.get_tracked_map()) == 2

    # Remove record1
    c4, u4, d4 = vfs.sync([record2])
    assert (c4, u4, d4) == (0, 0, 1)
    assert len(db.get_tracked_map()) == 1

    db.close()
