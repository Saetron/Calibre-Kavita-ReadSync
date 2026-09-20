"""FUSE-based Virtual File System for Kavita.

Mounts a dynamic in-memory read-only filesystem reflecting Calibre metadata
in the Kavita folder structure:
language/type/series/series Vol. volume Ch. chapter {calibre_id}.ext
"""

from __future__ import annotations

import errno
import logging
import os
import stat
import time
from typing import Any, Dict, List, Optional, Tuple

from .calibre_db import BookFileRecord, CalibreDBReader
from .formatter import build_vfs_relpath

logger = logging.getLogger(__name__)

try:
    from fuse import FUSE, FuseOSError, Operations
    HAS_FUSE = True
except (ImportError, Exception):
    HAS_FUSE = False
    Operations = object  # type: ignore
    FuseOSError = Exception  # type: ignore


class KavitaFuseFS(Operations):
    """Read-only FUSE filesystem presenting Calibre books formatted for Kavita."""

    def __init__(
        self,
        calibre_reader: CalibreDBReader,
        default_language: str = "unknown",
        default_type: str = "Unknown",
    ):
        if not HAS_FUSE:
            raise RuntimeError(
                "fusepy is not installed or FUSE is not available on this system. "
                "Use VFS mode 'hardlink' or 'symlink' instead, or install fusepy and libfuse."
            )
        self.reader = calibre_reader
        self.default_language = default_language
        self.default_type = default_type
        self.mount_time = time.time()

        # Cache trees
        self._tree: Dict[str, Dict[str, Any]] = {}
        self._file_sources: Dict[str, str] = {}
        self._last_db_mtime: float = 0.0

        self.refresh()

    def refresh(self) -> None:
        """Reload Calibre DB records into virtual directory tree."""
        current_mtime = self.reader.get_last_modified()
        if current_mtime == self._last_db_mtime and self._tree:
            return

        records = self.reader.get_all_book_files()
        new_tree: Dict[str, Dict[str, Any]] = {"/": {"type": "dir", "children": set()}}
        file_sources: Dict[str, str] = {}
        used_relpaths: Dict[str, int] = {}

        for rec in records:
            _, ext = os.path.splitext(rec.source_path)
            relpath = build_vfs_relpath(
                language=rec.language,
                type_=rec.type_,
                series=rec.series,
                volume=rec.volume,
                chapter=rec.chapter,
                calibre_id=rec.book_id,
                extension=ext,
                title=rec.title,
                default_language=self.default_language,
                default_type=self.default_type,
            )

            if relpath in used_relpaths:
                base, dot_ext = os.path.splitext(relpath)
                relpath = f"{base} ({rec.book_id}){dot_ext}"
            used_relpaths[relpath] = rec.book_id

            # Break into path segments: lang / type / series / filename
            parts = relpath.strip("/").split("/")
            curr_path = ""
            for i, part in enumerate(parts):
                parent_path = curr_path if curr_path else "/"
                curr_path = f"{curr_path}/{part}" if curr_path else f"/{part}"

                if i == len(parts) - 1:
                    # File node
                    new_tree[curr_path] = {
                        "type": "file",
                        "source": rec.source_path,
                    }
                    file_sources[curr_path] = rec.source_path
                    new_tree[parent_path]["children"].add(part)
                else:
                    # Directory node
                    if curr_path not in new_tree:
                        new_tree[curr_path] = {"type": "dir", "children": set()}
                    new_tree[parent_path]["children"].add(part)

        self._tree = new_tree
        self._file_sources = file_sources
        self._last_db_mtime = current_mtime
        logger.info("FUSE tree refreshed. Total items: %d", len(file_sources))

    def getattr(self, path: str, fh: Optional[int] = None) -> Dict[str, Any]:
        self.refresh()
        if path not in self._tree:
            raise FuseOSError(errno.ENOENT)

        node = self._tree[path]
        if node["type"] == "dir":
            return {
                "st_mode": stat.S_IFDIR | 0o755,
                "st_nlink": 2,
                "st_size": 4096,
                "st_ctime": self.mount_time,
                "st_mtime": self._last_db_mtime or self.mount_time,
                "st_atime": self.mount_time,
            }

        source = node["source"]
        try:
            st = os.stat(source)
            return {
                "st_mode": stat.S_IFREG | 0o644,
                "st_nlink": 1,
                "st_size": st.st_size,
                "st_ctime": st.st_ctime,
                "st_mtime": st.st_mtime,
                "st_atime": st.st_atime,
            }
        except OSError:
            raise FuseOSError(errno.ENOENT)

    def readdir(self, path: str, fh: Optional[int] = None) -> List[str]:
        self.refresh()
        if path not in self._tree or self._tree[path]["type"] != "dir":
            raise FuseOSError(errno.ENOENT)

        children = [".", ".."] + sorted(list(self._tree[path]["children"]))
        return children

    def open(self, path: str, flags: int) -> int:
        if path not in self._tree or self._tree[path]["type"] != "file":
            raise FuseOSError(errno.ENOENT)
        # Read-only check
        accmode = flags & (os.O_RDONLY | os.O_WRONLY | os.O_RDWR)
        if accmode != os.O_RDONLY:
            raise FuseOSError(errno.EROFS)
        source = self._tree[path]["source"]
        try:
            return os.open(source, os.O_RDONLY)
        except OSError as e:
            raise FuseOSError(e.errno)

    def read(self, path: str, size: int, offset: int, fh: int) -> bytes:
        try:
            os.lseek(fh, offset, os.SEEK_SET)
            return os.read(fh, size)
        except OSError as e:
            raise FuseOSError(e.errno)

    def release(self, path: str, fh: int) -> int:
        try:
            os.close(fh)
            return 0
        except OSError as e:
            raise FuseOSError(e.errno)


def mount_fuse(
    calibre_reader: CalibreDBReader,
    mount_point: str,
    default_language: str = "unknown",
    default_type: str = "Unknown",
    foreground: bool = True,
) -> None:
    """Mount the FUSE virtual filesystem."""
    if not HAS_FUSE:
        raise RuntimeError("FUSE is not available. Please use VFS mode 'hardlink' or 'symlink'.")
    os.makedirs(mount_point, exist_ok=True)
    fs = KavitaFuseFS(
        calibre_reader=calibre_reader,
        default_language=default_language,
        default_type=default_type,
    )
    logger.info("Mounting FUSE filesystem at %s", mount_point)
    FUSE(fs, mount_point, nothreads=False, foreground=foreground, ro=True, allow_other=True)
