"""VFS Lifecycle Manager for kosync-hub.

Coordinates Calibre DB reading, Symlink/Hardlink synchronization,
database caching, background polling, and WebUI actions.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict, Optional

from ..config import AppConfig
from .calibre_db import CalibreDBReader
from .database import VFSDatabase
from .state import VFSState, state
from .symlink import SymlinkVFS

logger = logging.getLogger("kosync_hub.vfs")


class VFSManager:
    """Manages the lifecycle, background tasks, and operations of the Kavita VFS subsystem."""

    def __init__(self, config: AppConfig, vfs_state: Optional[VFSState] = None):
        self.config = config
        self.state = vfs_state or state
        self._is_running = False
        self._task: Optional[asyncio.Task] = None
        self._manual_sync_event = asyncio.Event()

        # Database cache path
        db_path = self.config.vfs.db_path
        if not db_path:
            db_path = str(Path(self.config.data_dir) / "vfs_cache.db")

        self.db = VFSDatabase(db_path)
        self.state.set_db(self.db)

        # Calibre DB Reader
        self.calibre_reader = CalibreDBReader(
            calibre_dir=self.config.calibre.library_path,
            custom_type_column=self.config.vfs.custom_type_column,
            custom_volume_column=self.config.vfs.custom_volume_column,
            custom_chapter_column=self.config.vfs.custom_chapter_column,
        )

        # Symlink / Hardlink Syncer (mode defaults to hardlink)
        self.syncer = SymlinkVFS(
            vfs_dir=self.config.vfs.vfs_dir,
            link_type=self.config.vfs.mode,
            relative_links=self.config.vfs.relative_links,
            default_language=self.config.vfs.default_language,
            default_type=self.config.vfs.default_type,
            calibre_dir=self.config.calibre.library_path,
            target_calibre_dir=self.config.vfs.calibre_target_dir,
            db=self.db,
        )

        self.last_synced_mtime: float = -1.0

    def reconfigure(self, config: AppConfig):
        """Updates configuration settings on the fly."""
        self.config = config
        self.calibre_reader.calibre_dir = os.path.abspath(config.calibre.library_path)
        self.calibre_reader.db_path = os.path.join(self.calibre_reader.calibre_dir, "metadata.db")
        self.calibre_reader.custom_type_column = config.vfs.custom_type_column
        self.calibre_reader.custom_volume_column = config.vfs.custom_volume_column
        self.calibre_reader.custom_chapter_column = config.vfs.custom_chapter_column

        self.syncer.vfs_dir = os.path.abspath(config.vfs.vfs_dir)
        self.syncer.link_type = config.vfs.mode.lower()
        self.syncer.relative_links = config.vfs.relative_links
        self.syncer.default_language = config.vfs.default_language
        self.syncer.default_type = config.vfs.default_type
        self.syncer.calibre_dir = os.path.abspath(config.calibre.library_path)
        self.syncer.target_calibre_dir = config.vfs.calibre_target_dir.strip() if config.vfs.calibre_target_dir else None

    async def sync_now(self, force: bool = False) -> Dict[str, Any]:
        """Performs a VFS synchronization pass."""
        if not self.calibre_reader.exists():
            msg = f"Calibre metadata.db not found at {self.calibre_reader.db_path}"
            logger.warning(msg)
            return {"status": "error", "message": msg}

        self.state.set_syncing(True)
        try:
            # Run blocking filesystem / database work in thread pool
            loop = asyncio.get_event_loop()
            records = await loop.run_in_executor(None, self.calibre_reader.get_all_book_files)
            desired_map = await loop.run_in_executor(None, self.syncer.build_desired_tree, records)
            created, updated, deleted = await loop.run_in_executor(None, self.syncer.sync, records)

            self.state.update_sync_results(
                records=records,
                desired_map=desired_map,
                collisions=self.syncer.last_collisions,
                mode=self.config.vfs.mode,
                calibre_dir=self.config.calibre.library_path,
                vfs_dir=self.config.vfs.vfs_dir,
            )
            self.last_synced_mtime = self.calibre_reader.get_last_modified()
            return {
                "status": "success",
                "created": created,
                "updated": updated,
                "deleted": deleted,
                "total": len(desired_map),
                "collisions": len(self.syncer.last_collisions),
            }
        except Exception as e:
            logger.exception("Error during VFS synchronization: %s", e)
            self.state.set_syncing(False, error=str(e))
            return {"status": "error", "message": str(e)}

    async def cleanup(self) -> Dict[str, Any]:
        """Performs VFS cleanup, removing unregistered files and converting link modes."""
        if not self.calibre_reader.exists():
            msg = f"Calibre metadata.db not found at {self.calibre_reader.db_path}"
            logger.warning(msg)
            return {"status": "error", "message": msg}

        self.state.set_syncing(True)
        try:
            loop = asyncio.get_event_loop()
            records = await loop.run_in_executor(None, self.calibre_reader.get_all_book_files)
            res = await loop.run_in_executor(None, self.syncer.cleanup_unregistered, records)
            desired_map = await loop.run_in_executor(None, self.syncer.build_desired_tree, records)

            self.state.update_sync_results(
                records=records,
                desired_map=desired_map,
                collisions=self.syncer.last_collisions,
                mode=self.config.vfs.mode,
                calibre_dir=self.config.calibre.library_path,
                vfs_dir=self.config.vfs.vfs_dir,
            )
            self.last_synced_mtime = self.calibre_reader.get_last_modified()
            return {"status": "success", "result": res}
        except Exception as e:
            logger.exception("Error during VFS cleanup: %s", e)
            self.state.set_syncing(False, error=str(e))
            return {"status": "error", "message": str(e)}

    def trigger_manual_sync(self):
        """Wakes up the background sync loop immediately."""
        self._manual_sync_event.set()

    async def start_background_loop(self):
        """Monitors Calibre database for updates and syncs VFS."""
        if not self.config.vfs.enabled:
            logger.info("VFS generation is disabled in configuration. Skipping VFS loop.")
            return

        self._is_running = True
        logger.info(
            "Background VFS sync loop started (mode: %s, target: %s, interval: %ds).",
            self.config.vfs.mode,
            self.config.vfs.vfs_dir,
            self.config.vfs.interval_seconds,
        )

        while self._is_running:
            try:
                force_sync = self._manual_sync_event.is_set()
                if force_sync:
                    self._manual_sync_event.clear()

                if self.calibre_reader.exists():
                    current_mtime = self.calibre_reader.get_last_modified()
                    if force_sync or current_mtime != self.last_synced_mtime:
                        await self.sync_now(force=force_sync)
                else:
                    logger.debug("Waiting for Calibre DB at %s...", self.calibre_reader.db_path)
            except Exception as e:
                logger.error("Unexpected error in VFS background loop: %s", e)

            # Sleep in 1s increments to handle cancellation or manual trigger quickly
            for _ in range(max(1, self.config.vfs.interval_seconds)):
                if not self._is_running or self._manual_sync_event.is_set():
                    break
                await asyncio.sleep(1)

    def stop(self):
        """Stops the background VFS loop."""
        self._is_running = False
        self._manual_sync_event.set()
        logger.info("VFS Manager stopped.")
