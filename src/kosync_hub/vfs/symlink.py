"""Symlink and Hardlink VFS generator for Kavita.

Synchronizes a target directory with the structure expected by Kavita:
language/type/series/series Vol. volume Ch. chapter {calibre_id}.ext
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, List, Optional, Set, Tuple

from .calibre_db import BookFileRecord
from .formatter import build_vfs_relpath

logger = logging.getLogger(__name__)


class SymlinkVFS:
    def __init__(
        self,
        vfs_dir: str,
        link_type: str = "hardlink",  # Default: "hardlink" (or "symlink")
        relative_links: bool = False,
        default_language: str = "unknown",
        default_type: str = "Unknown",
        calibre_dir: str = "/calibre",
        target_calibre_dir: Optional[str] = None,
        db: Optional[Any] = None,
    ):
        self.vfs_dir = os.path.abspath(vfs_dir)
        self.link_type = link_type.lower()
        self.relative_links = relative_links
        self.default_language = default_language
        self.default_type = default_type
        self.calibre_dir = os.path.abspath(calibre_dir)
        self.target_calibre_dir = target_calibre_dir.strip() if target_calibre_dir else None
        self.db = db

        self.last_collisions: List[Dict[str, Any]] = []
        self.last_records_map: Dict[str, Tuple[BookFileRecord, str]] = {}

        if self.link_type not in ("symlink", "hardlink"):
            raise ValueError(f"Unsupported link_type: {link_type}. Must be 'symlink' or 'hardlink'")

    def build_desired_tree(
        self, records: List[BookFileRecord]
    ) -> Dict[str, str]:
        """Map absolute target file paths in VFS to source file paths in Calibre.

        Handles collisions by suffixing duplicate index if multiple files map to the same path.
        """
        self.last_collisions = []
        self.last_records_map = {}
        desired: Dict[str, str] = {}
        # Keep track of paths to detect and resolve collisions
        used_relpaths: Dict[str, Dict[str, Any]] = {}

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

            # Check for collision
            if relpath in used_relpaths:
                prior = used_relpaths[relpath]
                base, dot_ext = os.path.splitext(relpath)
                disambiguated = f"{base}_collision_{len(self.last_collisions) + 1}{dot_ext}"
                collision_info = {
                    "relpath": relpath,
                    "existing_book_id": prior["book_id"],
                    "existing_source": prior["source_path"],
                    "colliding_book_id": rec.book_id,
                    "colliding_source": rec.source_path,
                    "resolved_path": disambiguated,
                }
                self.last_collisions.append(collision_info)
                logger.warning(
                    "Collision detected for path '%s'! Books #%s and #%s. Resolved to '%s'",
                    relpath,
                    prior["book_id"],
                    rec.book_id,
                    disambiguated,
                )
                relpath = disambiguated

            used_relpaths[relpath] = {"book_id": rec.book_id, "source_path": rec.source_path}
            target_path = os.path.join(self.vfs_dir, relpath)
            desired[target_path] = rec.source_path
            self.last_records_map[target_path] = (rec, relpath)

        return desired

    def _compute_target_source(self, target_path: str, source_path: str) -> str:
        """Compute the link target path based on mode and settings."""
        if self.link_type == "symlink":
            if self.target_calibre_dir:
                rel_to_calibre = os.path.relpath(source_path, self.calibre_dir)
                return os.path.join(self.target_calibre_dir, rel_to_calibre)
            elif self.relative_links:
                parent_dir = os.path.dirname(target_path)
                return os.path.relpath(source_path, parent_dir)
            else:
                return source_path
        else:
            return source_path

    def _ensure_link(self, target_path: str, source_path: str, target_source: str) -> Tuple[bool, bool]:
        """Create or update link. Returns (created: bool, updated: bool)."""
        parent_dir = os.path.dirname(target_path)
        os.makedirs(parent_dir, exist_ok=True)

        needs_create = True
        updated = False
        created = False

        if self.link_type == "symlink":
            if os.path.islink(target_path):
                try:
                    if os.readlink(target_path) == target_source:
                        needs_create = False
                    else:
                        os.unlink(target_path)
                        updated = True
                except OSError as e:
                    logger.warning("Failed to remove link for update %s: %s", target_path, e)
            elif os.path.exists(target_path):
                try:
                    os.unlink(target_path)
                    updated = True
                except OSError as e:
                    logger.warning("Failed to remove non-symlink file for symlink update %s: %s", target_path, e)
        else:
            # hardlink
            if os.path.islink(target_path):
                try:
                    os.unlink(target_path)
                    updated = True
                except OSError as e:
                    logger.warning("Failed to remove symlink for hardlink update %s: %s", target_path, e)
            elif os.path.exists(target_path):
                try:
                    if os.stat(target_path).st_ino == os.stat(source_path).st_ino:
                        needs_create = False
                    else:
                        os.unlink(target_path)
                        updated = True
                except OSError:
                    pass

        if needs_create:
            try:
                if self.link_type == "symlink":
                    os.symlink(target_source, target_path)
                else:
                    os.link(source_path, target_path)
                created = True
                logger.debug("Linked: %s -> %s", target_path, target_source)
            except OSError as e:
                import errno
                if self.link_type == "hardlink" and e.errno == errno.EXDEV:
                    logger.error(
                        "Cannot create hardlink across different filesystems/mounts (%s -> %s). "
                        "Ensure both calibre library and VFS directory are on the same filesystem/disk, or use VFS mode 'symlink'.",
                        source_path, target_path,
                    )
                else:
                    logger.error("Failed to create %s %s -> %s: %s", self.link_type, target_path, target_source, e)

        return created, updated

    def sync(self, records: List[BookFileRecord]) -> Tuple[int, int, int]:
        """Synchronize the VFS directory with current Calibre records.

        Returns a tuple: (created_count, updated_count, deleted_count).
        """
        os.makedirs(self.vfs_dir, exist_ok=True)
        desired_map = self.build_desired_tree(records)
        created_count = 0
        updated_count = 0
        deleted_count = 0
        now = time.time()

        if self.db is not None:
            tracked = self.db.get_tracked_map()

            # 1. Delta deletions
            stale_paths = [p for p in tracked if p not in desired_map]
            for p in stale_paths:
                if os.path.lexists(p):
                    try:
                        os.unlink(p)
                        deleted_count += 1
                        logger.debug("Removed stale link: %s", p)
                    except OSError as e:
                        logger.warning("Failed to remove stale link %s: %s", p, e)
            if stale_paths:
                self.db.delete_entries(stale_paths)

            # 2. Initial startup scan if database is empty
            if not tracked:
                for root, dirs, files in os.walk(self.vfs_dir):
                    for file in files:
                        tp = os.path.join(root, file)
                        if getattr(self.db, "db_path", None) and os.path.abspath(tp) == os.path.abspath(self.db.db_path):
                            continue
                        if tp not in desired_map:
                            try:
                                os.unlink(tp)
                                deleted_count += 1
                            except OSError:
                                pass

            # 3. Delta creations/updates
            db_entries = []
            for target_path, source_path in desired_map.items():
                target_source = self._compute_target_source(target_path, source_path)
                prev = tracked.get(target_path)

                needs_action = True
                if prev is not None:
                    if (
                        prev["link_type"] == self.link_type
                        and prev["target_source"] == target_source
                        and os.path.lexists(target_path)
                    ):
                        needs_action = False

                if needs_action:
                    c, u = self._ensure_link(target_path, source_path, target_source)
                    if c:
                        created_count += 1
                    if u:
                        updated_count += 1

                rec_item = self.last_records_map.get(target_path)
                if rec_item:
                    rec, relpath = rec_item
                    db_entries.append({
                        "vfs_path": target_path,
                        "vfs_relpath": relpath,
                        "source_path": source_path,
                        "book_id": rec.book_id,
                        "title": rec.title,
                        "series": rec.series or "",
                        "volume": str(rec.volume) if rec.volume is not None else "",
                        "chapter": str(rec.chapter) if rec.chapter is not None else "",
                        "type": rec.type_ or self.default_type,
                        "language": rec.language or self.default_language,
                        "format": rec.format,
                        "link_type": self.link_type,
                        "target_source": target_source,
                        "last_synced": now,
                    })

            self.db.upsert_entries(db_entries)

        else:
            # Full filesystem scan when no DB is configured
            existing_links: Dict[str, str] = {}
            for root, dirs, files in os.walk(self.vfs_dir):
                for file in files:
                    target_path = os.path.join(root, file)
                    if os.path.islink(target_path):
                        existing_links[target_path] = os.readlink(target_path)
                    elif os.path.isfile(target_path):
                        existing_links[target_path] = "file"

            for existing_path in existing_links:
                if existing_path not in desired_map:
                    try:
                        os.unlink(existing_path)
                        deleted_count += 1
                        logger.debug("Removed stale link: %s", existing_path)
                    except OSError as e:
                        logger.warning("Failed to remove stale link %s: %s", existing_path, e)

            for target_path, source_path in desired_map.items():
                target_source = self._compute_target_source(target_path, source_path)
                c, u = self._ensure_link(target_path, source_path, target_source)
                if c:
                    created_count += 1
                if u:
                    updated_count += 1

        if created_count > 0 or updated_count > 0 or deleted_count > 0:
            self._prune_empty_dirs(self.vfs_dir)

        logger.info(
            "VFS Sync complete. Created: %d, Updated: %d, Deleted: %d (Total in VFS: %d)",
            created_count,
            updated_count,
            deleted_count,
            len(desired_map),
        )
        return created_count, updated_count, deleted_count

    def cleanup_unregistered(self, records: List[BookFileRecord]) -> Dict[str, Any]:
        """Scan VFS directory and remove any file, symlink, or broken link not in desired_map.

        Also cleans empty directories and ensures all links match current mode.
        """
        os.makedirs(self.vfs_dir, exist_ok=True)
        desired_map = self.build_desired_tree(records)
        removed_files = []

        for root, dirs, files in os.walk(self.vfs_dir):
            for file in files:
                target_path = os.path.join(root, file)
                if getattr(self.db, "db_path", None) and os.path.abspath(target_path) == os.path.abspath(self.db.db_path):
                    continue
                if target_path not in desired_map:
                    try:
                        is_link = os.path.islink(target_path)
                        os.unlink(target_path)
                        rel = os.path.relpath(target_path, self.vfs_dir)
                        removed_files.append({"path": rel, "is_symlink": is_link})
                        logger.info("Cleaned up unregistered file: %s (symlink=%s)", rel, is_link)
                    except OSError as e:
                        logger.warning("Failed to remove unregistered file %s: %s", target_path, e)

        empty_dirs_removed = self._prune_empty_dirs(self.vfs_dir)

        if self.db is not None:
            self.db.clear()

        # Run sync to guarantee mode conversion and repopulate DB
        created, updated, deleted = self.sync(records)

        return {
            "removed_files": removed_files,
            "removed_count": len(removed_files),
            "empty_dirs_removed": empty_dirs_removed,
            "sync_created": created,
            "sync_updated": updated,
            "sync_deleted": deleted,
        }

    def _prune_empty_dirs(self, root_dir: str) -> int:
        """Recursively remove empty directories. Returns count of removed directories."""
        removed = 0
        for root, dirs, files in os.walk(root_dir, topdown=False):
            if root == root_dir:
                continue
            if not os.listdir(root):
                try:
                    os.rmdir(root)
                    removed += 1
                    logger.debug("Removed empty directory: %s", root)
                except OSError:
                    pass
        return removed
