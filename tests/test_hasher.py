import tempfile
from pathlib import Path
import pytest
from kosync_hub.hasher import (
    compute_all_book_hashes,
    compute_filename_md5,
    compute_full_md5,
    compute_koreader_partial_md5,
)


def test_compute_filename_md5():
    h1 = compute_filename_md5("Dune.epub")
    h2 = compute_filename_md5("/path/to/Dune.epub")
    assert len(h1) == 32
    assert h1 == h2  # Basename match


def test_compute_koreader_partial_md5():
    with tempfile.NamedTemporaryFile(delete=False) as f:
        # Write 50KB of deterministic data
        data = b"Hello KOReader! " * 3500
        f.write(data)
        filepath = f.name

    try:
        h = compute_koreader_partial_md5(filepath)
        assert h is not None
        assert len(h) == 32

        # Verify idempotency
        h2 = compute_koreader_partial_md5(filepath)
        assert h == h2

        all_hashes = compute_all_book_hashes(filepath)
        assert "partial_md5" in all_hashes
        assert "full_md5" in all_hashes
        assert "filename_md5" in all_hashes
        assert all_hashes["partial_md5"] == h
    finally:
        Path(filepath).unlink(missing_ok=True)
