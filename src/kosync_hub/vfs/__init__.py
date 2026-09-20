"""Kavita VFS subsystem for kosync-hub."""

from .calibre_db import BookFileRecord, CalibreDBReader, CustomColumnInfo
from .database import VFSDatabase
from .formatter import build_kavita_filename, build_vfs_relpath, sanitize_filename_component
from .manager import VFSManager
from .state import VFSState, calculate_units, state
from .symlink import SymlinkVFS

__all__ = [
    "BookFileRecord",
    "CalibreDBReader",
    "CustomColumnInfo",
    "VFSDatabase",
    "VFSManager",
    "VFSState",
    "SymlinkVFS",
    "build_kavita_filename",
    "build_vfs_relpath",
    "calculate_units",
    "sanitize_filename_component",
    "state",
]
