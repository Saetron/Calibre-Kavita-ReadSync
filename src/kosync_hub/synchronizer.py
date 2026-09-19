"""Bidirectional synchronization engine between Kavita and Calibre using filename Calibre IDs."""

import asyncio
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from .clients.calibre_db import CalibreDbClient
from .clients.kavita import KavitaClient
from .db import InternalDatabase
from .models import ProgressRecord, SyncEvent

logger = logging.getLogger("kosync_hub.sync")


class Synchronizer:
    """Orchestrates bidirectional reading progress synchronization between Kavita and Calibre."""

    def __init__(
        self,
        db: InternalDatabase,
        kavita: Optional[KavitaClient] = None,
        calibre: Optional[CalibreDbClient] = None,
        conflict_strategy: str = "latest_timestamp",
        interval_seconds: int = 300,
    ):
        self.db = db
        self.kavita = kavita
        self.calibre = calibre
        self.conflict_strategy = conflict_strategy
        self.interval_seconds = interval_seconds
        self._is_running = False
        self.last_sync_timestamp: Optional[int] = None
        self.last_sync_status: Optional[str] = None
        self.sync_count: int = 0

    async def sync_document(self, document: str) -> Dict[str, Any]:
        """Synchronizes a single document hash between Kavita, Calibre, and local DB."""
        local_rec = self.db.get_document(document)
        records = [r for r in [local_rec] if r]

        calibre_id = None
        if local_rec and local_rec.calibre_id:
            calibre_id = local_rec.calibre_id
        else:
            calibre_id = self.db.get_calibre_id_for_document(document)

        canonical_hash = document
        if calibre_id:
            # Check if there is a canonical document hash or Calibre record
            if self.calibre and hasattr(self.calibre, "get_book_by_id"):
                cal_book = self.calibre.get_book_by_id(calibre_id)
                if cal_book:
                    if getattr(cal_book, "koreader_hash", None):
                        canonical_hash = cal_book.koreader_hash
                    # Get progress from Calibre
                    c_rec = await self.calibre.get_progress(canonical_hash)
                    if c_rec:
                        records.append(c_rec)

        if self.kavita:
            # Check Kavita with both the requested hash and canonical hash
            k_rec = await self.kavita.get_progress(canonical_hash)
            if not k_rec and canonical_hash != document:
                k_rec = await self.kavita.get_progress(document)
            if k_rec:
                records.append(k_rec)

        if self.calibre and canonical_hash == document:
            c_rec = await self.calibre.get_progress(document)
            if c_rec:
                records.append(c_rec)

        if not records:
            return {"document": document, "synced": False}

        winner = max(records, key=lambda r: (r.timestamp, r.percentage))
        if calibre_id and not winner.calibre_id:
            winner.calibre_id = calibre_id

        # Save under requested document hash so device lookup succeeds
        device_rec = ProgressRecord(
            document=document,
            progress=winner.progress,
            percentage=winner.percentage,
            timestamp=winner.timestamp,
            device=winner.device,
            device_id=winner.device_id,
            title=winner.title,
            authors=winner.authors,
            filename=winner.filename,
            calibre_id=calibre_id or winner.calibre_id,
        )
        self.db.upsert_progress(device_rec, calibre_book_id=calibre_id or winner.calibre_id)

        if calibre_id:
            self.db.update_sync_state(
                calibre_id=calibre_id,
                percentage=winner.percentage,
                progress=winner.progress,
                source="sync_document",
                synced_at=winner.timestamp,
            )

        return {"document": document, "synced": True, "winner": device_rec}

    async def sync_kavita_to_calibre(self) -> int:
        """
        Fetches recent reads from Kavita (On Deck / continue reading),
        extracts Calibre IDs from {id} in filenames, and syncs progress into Calibre DB.
        """
        if not self.kavita or not self.calibre:
            return 0

        updated_count = 0
        recent_reads = await self.kavita.get_on_deck_reads()

        for item in recent_reads:
            calibre_id = item.calibre_id
            if calibre_id is None:
                continue

            calibre_book = self.calibre.get_book_by_id(calibre_id)
            if not calibre_book:
                logger.debug(f"Book with Calibre ID {calibre_id} ('{item.filename}') not found in Calibre DB.")
                continue

            kavita_pct = item.percentage
            kavita_progress_str = f"page:{item.pages_read}/{item.total_pages}"
            kavita_ts = int(datetime.utcnow().timestamp())

            # Check if Kavita KOReader sync endpoint has newer/more granular progress
            if calibre_book.koreader_hash:
                ko_record = await self.kavita.get_progress(calibre_book.koreader_hash)
                if ko_record:
                    if ko_record.percentage > kavita_pct:
                        kavita_pct = ko_record.percentage
                        kavita_progress_str = ko_record.progress
                        kavita_ts = ko_record.timestamp

            # Compare with Calibre's current percentage
            if kavita_pct > calibre_book.percentage or (kavita_pct == calibre_book.percentage and kavita_pct > 0 and calibre_book.koreader_progress != kavita_progress_str):
                logger.info(
                    f"Syncing from Kavita to Calibre for book #{calibre_id} ('{calibre_book.title}'): "
                    f"{round(calibre_book.percentage * 100, 1)}% -> {round(kavita_pct * 100, 1)}%"
                )
                ok = self.calibre.update_book_progress(
                    book_id=calibre_id,
                    percentage=kavita_pct,
                    progress_str=kavita_progress_str,
                    timestamp=kavita_ts,
                )
                if ok:
                    updated_count += 1
                    doc_hash = calibre_book.koreader_hash or f"calibre_{calibre_id}"
                    record = ProgressRecord(
                        document=doc_hash,
                        progress=kavita_progress_str,
                        percentage=kavita_pct,
                        timestamp=kavita_ts,
                        device="Kavita",
                        title=calibre_book.title,
                        authors=calibre_book.authors,
                        calibre_id=calibre_id,
                    )
                    self.db.upsert_progress(
                        record=record,
                        calibre_book_id=calibre_id,
                        kavita_synced_at=kavita_ts,
                        calibre_synced_at=kavita_ts,
                        sync_status="synced_to_calibre",
                    )
                    self.db.log_sync_event(
                        SyncEvent(
                            document=doc_hash,
                            calibre_id=calibre_id,
                            source="kavita",
                            target="calibre",
                            progress=kavita_progress_str,
                            percentage=kavita_pct,
                            timestamp=kavita_ts,
                            success=True,
                            message=f"Synced from Kavita to Calibre #{calibre_id} ({round(kavita_pct * 100, 1)}%)",
                        )
                    )

        # Also check Calibre's recent books that have KOReader hashes directly against Kavita KOReader endpoint
        calibre_recent = self.calibre.get_recently_read_books(limit=30)
        checked_calibre_ids = {r.calibre_id for r in recent_reads if r.calibre_id is not None}
        for book in calibre_recent:
            if book.book_id in checked_calibre_ids:
                continue
            hsh = book.koreader_hash or self.calibre.get_or_compute_koreader_hash(book.book_id)
            if not hsh:
                continue
            ko_rec = await self.kavita.get_progress(hsh)
            if ko_rec and (
                ko_rec.percentage > book.percentage
                or (ko_rec.percentage == book.percentage and ko_rec.percentage > 0 and book.koreader_progress != ko_rec.progress)
            ):
                logger.info(
                    f"Syncing from Kavita KOReader endpoint to Calibre for book #{book.book_id} ('{book.title}'): "
                    f"{round(book.percentage * 100, 1)}% -> {round(ko_rec.percentage * 100, 1)}%"
                )
                ok = self.calibre.update_book_progress(
                    book_id=book.book_id,
                    percentage=ko_rec.percentage,
                    progress_str=ko_rec.progress,
                    timestamp=ko_rec.timestamp,
                )
                if ok:
                    updated_count += 1
                    self.db.upsert_progress(
                        record=ko_rec,
                        calibre_book_id=book.book_id,
                        kavita_synced_at=ko_rec.timestamp,
                        calibre_synced_at=ko_rec.timestamp,
                        sync_status="synced_to_calibre",
                    )
                    self.db.log_sync_event(
                        SyncEvent(
                            document=hsh,
                            calibre_id=book.book_id,
                            source="kavita_koreader_endpoint",
                            target="calibre",
                            progress=ko_rec.progress,
                            percentage=ko_rec.percentage,
                            timestamp=ko_rec.timestamp,
                            success=True,
                            message=f"Synced KOReader progress from Kavita to Calibre #{book.book_id} ({round(ko_rec.percentage * 100, 1)}%)",
                        )
                    )
                    # Also make sure Kavita WebUI has this reading progress marked
                    try:
                        await self.kavita.update_webui_progress(
                            calibre_id=book.book_id,
                            percentage=ko_rec.percentage,
                            title=book.title,
                        )
                    except Exception as e:
                        logger.warning(f"Failed to update Kavita WebUI for #{book.book_id}: {e}")

        return updated_count

    async def sync_calibre_to_kavita(self) -> int:
        """
        Fetches recently read/updated books from Calibre DB.
        If Calibre progress is ahead of Kavita, pushes update to Kavita's KOReader endpoint.
        """
        if not self.kavita or not self.calibre:
            return 0

        updated_count = 0
        recent_books = self.calibre.get_recently_read_books(limit=30)

        for book in recent_books:
            if book.percentage <= 0:
                continue

            hsh = book.koreader_hash or self.calibre.get_or_compute_koreader_hash(book.book_id)
            if not hsh:
                continue

            # Check Kavita's current KOReader progress
            kavita_rec = await self.kavita.get_progress(hsh)
            kavita_pct = kavita_rec.percentage if kavita_rec else 0.0

            # If Calibre is ahead of Kavita
            if book.percentage > kavita_pct:
                logger.info(
                    f"Syncing from Calibre to Kavita for book #{book.book_id} ('{book.title}'): "
                    f"{round(kavita_pct * 100, 1)}% -> {round(book.percentage * 100, 1)}%"
                )
                now_ts = int(datetime.utcnow().timestamp())
                record = ProgressRecord(
                    document=hsh,
                    progress=book.koreader_progress or f"page:{round(book.percentage * 100, 1)}%",
                    percentage=book.percentage,
                    timestamp=now_ts,
                    device="Calibre",
                    title=book.title,
                    authors=book.authors,
                    filename=f"{book.title} {{{book.book_id}}}.{book.format.lower() if book.format else 'epub'}",
                    calibre_id=book.book_id,
                )
                ok = await self.kavita.update_progress(record)
                if ok:
                    updated_count += 1
                    self.db.upsert_progress(
                        record=record,
                        calibre_book_id=book.book_id,
                        kavita_synced_at=now_ts,
                        calibre_synced_at=now_ts,
                        sync_status="synced_to_kavita",
                    )
                    self.db.log_sync_event(
                        SyncEvent(
                            document=hsh,
                            calibre_id=book.book_id,
                            source="calibre",
                            target="kavita",
                            progress=record.progress,
                            percentage=record.percentage,
                            timestamp=now_ts,
                            success=True,
                            message=f"Pushed Calibre #{book.book_id} ({round(record.percentage * 100, 1)}%) to Kavita KOReader endpoint",
                        )
                    )

        return updated_count

    async def sync_all(self) -> Dict[str, Any]:
        """Runs a complete bidirectional sync pass between Kavita and Calibre."""
        logger.info("Starting bidirectional sync pass between Kavita and Calibre DB...")
        updated_calibre = 0
        updated_kavita = 0
        error_msg = None

        try:
            # 1. Kavita -> Calibre
            updated_calibre = await self.sync_kavita_to_calibre()

            # 2. Calibre -> Kavita
            updated_kavita = await self.sync_calibre_to_kavita()

            self.last_sync_status = "success"
            self.sync_count += 1
        except Exception as e:
            logger.error(f"Error during bidirectional sync: {e}")
            self.last_sync_status = f"error: {str(e)}"
            error_msg = str(e)

        self.last_sync_timestamp = int(datetime.utcnow().timestamp())
        logger.info(
            f"Bidirectional sync completed: updated Calibre: {updated_calibre}, updated Kavita: {updated_kavita}."
        )
        return {
            "updated_calibre": updated_calibre,
            "updated_kavita": updated_kavita,
            "timestamp": self.last_sync_timestamp,
            "status": self.last_sync_status,
            "error": error_msg,
        }

    async def start_background_loop(self):
        """Runs periodic background synchronization."""
        self._is_running = True
        logger.info(f"Background bidirectional sync loop started (interval: {self.interval_seconds}s).")
        while self._is_running:
            try:
                await self.sync_all()
            except Exception as e:
                logger.error(f"Unexpected error in sync loop: {e}")
            await asyncio.sleep(self.interval_seconds)

    def stop(self):
        """Stops background synchronization."""
        self._is_running = False
        if self._task and not self._task.done():
            self._task.cancel()
        logger.info("Bidirectional synchronizer stopped.")
