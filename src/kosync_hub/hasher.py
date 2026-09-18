"""Hash calculation utilities matching KOReader and Kavita document digest methods."""

import hashlib
import os
from pathlib import Path
from typing import Dict, List, Optional, Union


def compute_koreader_partial_md5(filepath: Union[str, Path]) -> Optional[str]:
    """
    Computes the exact partial MD5 checksum used by KOReader (util.partialMD5).

    KOReader algorithm:
    Samples 1024 bytes at non-linear offsets starting from offset 0, 1024, 4096, ...
    Offsets formula:
      i = -1: offset 0
      i = 0..10: offset 1024 * 4^i (i.e. 1024 << (2 * i))
    """
    path = Path(filepath)
    if not path.is_file():
        return None

    step = 1024
    size = 1024
    hasher = hashlib.md5()

    try:
        file_size = path.stat().st_size
        with open(path, "rb") as f:
            for i in range(-1, 11):
                offset = 0 if i == -1 else (step << (2 * i))
                if offset >= file_size:
                    break
                f.seek(offset)
                chunk = f.read(size)
                if not chunk:
                    break
                hasher.update(chunk)
        return hasher.hexdigest()
    except (OSError, PermissionError):
        return None


def compute_full_md5(filepath: Union[str, Path], block_size: int = 65536) -> Optional[str]:
    """Computes standard full-file MD5 hash."""
    path = Path(filepath)
    if not path.is_file():
        return None
    hasher = hashlib.md5()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(block_size), b""):
                hasher.update(chunk)
        return hasher.hexdigest()
    except (OSError, PermissionError):
        return None


def compute_filename_md5(filename: str) -> str:
    """Computes MD5 of a filename string (matches KOReader CHECKSUM_METHOD.FILENAME)."""
    clean_name = os.path.basename(filename)
    return hashlib.md5(clean_name.encode("utf-8")).hexdigest()


def compute_all_book_hashes(filepath: Union[str, Path]) -> Dict[str, str]:
    """
    Calculates all relevant hashes for an ebook file:
    - koreader_partial_md5: KOReader standard binary hash
    - full_md5: Full file MD5 checksum
    - filename_md5: MD5 of the basename
    """
    path = Path(filepath)
    hashes: Dict[str, str] = {}

    partial = compute_koreader_partial_md5(path)
    if partial:
        hashes["partial_md5"] = partial

    full = compute_full_md5(path)
    if full:
        hashes["full_md5"] = full

    hashes["filename_md5"] = compute_filename_md5(path.name)
    return hashes
