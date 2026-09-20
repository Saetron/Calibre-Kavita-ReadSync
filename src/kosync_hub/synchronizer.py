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

        if not calibre_id and self.calibre and hasattr(self.calibre, "find_book_by_hash"):
            calibre_id = self.calibre.find_book_by_hash(document)
            if not calibre_id and hasattr(self.calibre, "find_book_by_filename_hash"):
                calibre_id = self.calibre.find_book_by_filename_hash(document)
            if calibre_id:
                self.db.link_document_alias(document, calibre_id, "KOReader")

        canonical_hash = document
        cal_book = None
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
            # Check Kavita with the canonical hash
            k_rec = await self.kavita.get_progress(canonical_hash)
            if k_rec:
                records.append(k_rec)

        if self.calibre and canonical_hash == document and not records:
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

    async def pull_calibre_to_hub(self) -> int:
        """
        Ingress: Pulls reading progress from Calibre DB into the Hub's tracked_documents.
        Only updates Hub if Calibre's progress is ahead or not yet tracked.
        """
        if not self.calibre:
            return 0

        pulled_count = 0
        now_ts = int(datetime.utcnow().timestamp())
        recent_books = self.calibre.get_recently_read_books(limit=100)

        for book in recent_books:
            if book.percentage <= 0 and not book.is_read:
                continue

            cal_pct = 1.0 if book.is_read and book.percentage < 0.98 else book.percentage
            cal_id = book.book_id

            existing = self.db.get_tracked_document_by_calibre_id(cal_id)
            existing_pct = float(existing["percentage"] or 0.0) if existing else 0.0

            if (
                not existing
                or cal_pct > existing_pct
                or (cal_pct == existing_pct and existing_pct > 0 and book.koreader_progress and book.koreader_progress != existing["progress"])
            ):
                doc_hash = (
                    book.koreader_hash
                    or (existing["document"] if existing else None)
                    or self.calibre.get_or_compute_koreader_hash(cal_id)
                    or f"calibre_{cal_id}"
                )
                progress_str = book.koreader_progress or (f"page:{round(cal_pct * 100, 1)}%" if cal_pct > 0 else "0")
                record = ProgressRecord(
                    document=doc_hash,
                    progress=progress_str,
                    percentage=cal_pct,
                    timestamp=now_ts,
                    device="Calibre",
                    title=book.title,
                    authors=book.authors,
                    filename=f"{book.title} {{{cal_id}}}.{book.format.lower() if book.format else 'epub'}",
                    calibre_id=cal_id,
                )
                self.db.upsert_progress(
                    record=record,
                    calibre_book_id=cal_id,
                    calibre_synced_at=now_ts,
                    sync_status="synced",
                )
                self.db.log_sync_event(
                    SyncEvent(
                        document=doc_hash,
                        calibre_id=cal_id,
                        source="calibre",
                        target="hub",
                        progress=progress_str,
                        percentage=cal_pct,
                        timestamp=now_ts,
                        success=True,
                        message=f"Pulled Calibre #{cal_id} ('{book.title}') into Hub ({round(cal_pct * 100, 1)}%)",
                    )
                )
                pulled_count += 1
                logger.info(f"Pulled reading progress from Calibre for book #{cal_id} ('{book.title}'): {round(cal_pct * 100, 1)}%")

        return pulled_count

    async def pull_kavita_to_hub(self) -> int:
        """
        Ingress: Pulls reading progress from Kavita (On Deck / Currently Reading & KOReader endpoint)
        into the Hub's tracked_documents.
        Only updates Hub if Kavita's progress is ahead or not yet tracked.
        """
        if not self.kavita:
            return 0

        pulled_count = 0
        now_ts = int(datetime.utcnow().timestamp())
        recent_reads = await self.kavita.get_on_deck_reads()
        seen_calibre_ids = set()

        for item in recent_reads:
            cal_id = item.calibre_id
            if cal_id is None:
                continue

            seen_calibre_ids.add(cal_id)
            kavita_pct = item.percentage
            kavita_prog = f"page:{item.pages_read}/{item.total_pages}" if item.total_pages > 0 else f"page:{round(kavita_pct * 100, 1)}%"
            kavita_ts = now_ts

            existing_row = self.db.get_tracked_document_by_calibre_id(cal_id)
            existing = dict(existing_row) if existing_row else None
            existing_pct = float(existing["percentage"] or 0.0) if existing else 0.0

            # Check if Kavita KOReader sync endpoint has newer/more granular progress
            ko_hash = (existing["document"] if existing else None)
            if not ko_hash and self.calibre:
                cal_book = self.calibre.get_book_by_id(cal_id)
                if cal_book:
                    ko_hash = cal_book.koreader_hash or self.calibre.get_or_compute_koreader_hash(cal_id)

            if ko_hash:
                ko_record = await self.kavita.get_progress(ko_hash)
                if ko_record:
                    if ko_record.percentage > kavita_pct:
                        kavita_pct = ko_record.percentage
                        kavita_prog = ko_record.progress
                        kavita_ts = ko_record.timestamp or now_ts

            if (
                not existing
                or kavita_pct > existing_pct
                or (kavita_pct == existing_pct and kavita_pct > 0 and kavita_prog != existing["progress"])
            ):
                doc_hash = ko_hash or f"kavita_{cal_id}"
                title = (existing.get("title") if existing else None) or item.series_name or "Unknown"
                authors = existing.get("authors") if existing else None
                if self.calibre:
                    cal_book = self.calibre.get_book_by_id(cal_id)
                    if cal_book:
                        title = cal_book.title or title
                        authors = cal_book.authors or authors

                record = ProgressRecord(
                    document=doc_hash,
                    progress=kavita_prog,
                    percentage=kavita_pct,
                    timestamp=kavita_ts,
                    device="Kavita",
                    title=title,
                    authors=authors,
                    filename=item.filename or f"{title} {{{cal_id}}}.epub",
                    calibre_id=cal_id,
                )
                self.db.upsert_progress(
                    record=record,
                    calibre_book_id=cal_id,
                    kavita_synced_at=kavita_ts,
                    sync_status="synced",
                )
                self.db.log_sync_event(
                    SyncEvent(
                        document=doc_hash,
                        calibre_id=cal_id,
                        source="kavita",
                        target="hub",
                        progress=kavita_prog,
                        percentage=kavita_pct,
                        timestamp=kavita_ts,
                        success=True,
                        message=f"Pulled Kavita #{cal_id} ('{title}') into Hub ({round(kavita_pct * 100, 1)}%)",
                    )
                )
                pulled_count += 1
                logger.info(f"Pulled reading progress from Kavita for book #{cal_id} ('{title}'): {round(kavita_pct * 100, 1)}%")

        # Also check Calibre recent books that have KOReader hashes directly against Kavita KOReader endpoint
        if self.calibre:
            calibre_recent = self.calibre.get_recently_read_books(limit=30)
            for book in calibre_recent:
                if book.book_id in seen_calibre_ids:
                    continue
                hsh = book.koreader_hash or self.calibre.get_or_compute_koreader_hash(book.book_id)
                if not hsh:
                    continue
                ko_rec = await self.kavita.get_progress(hsh)
                if not ko_rec or ko_rec.percentage <= 0:
                    continue

                existing = self.db.get_tracked_document_by_calibre_id(book.book_id)
                existing_pct = float(existing["percentage"] or 0.0) if existing else 0.0

                if (
                    not existing
                    or ko_rec.percentage > existing_pct
                    or (ko_rec.percentage == existing_pct and ko_rec.percentage > 0 and ko_rec.progress != existing["progress"])
                ):
                    ko_rec.title = book.title
                    ko_rec.authors = book.authors
                    ko_rec.calibre_id = book.book_id
                    ko_rec.filename = f"{book.title} {{{book.book_id}}}.{book.format.lower() if book.format else 'epub'}"
                    self.db.upsert_progress(
                        record=ko_rec,
                        calibre_book_id=book.book_id,
                        kavita_synced_at=ko_rec.timestamp,
                        sync_status="synced",
                    )
                    self.db.log_sync_event(
                        SyncEvent(
                            document=hsh,
                            calibre_id=book.book_id,
                            source="kavita_koreader_endpoint",
                            target="hub",
                            progress=ko_rec.progress,
                            percentage=ko_rec.percentage,
                            timestamp=ko_rec.timestamp,
                            success=True,
                            message=f"Pulled KOReader progress from Kavita for #{book.book_id} ('{book.title}') into Hub ({round(ko_rec.percentage * 100, 1)}%)",
                        )
                    )
                    pulled_count += 1
                    logger.info(f"Pulled KOReader progress from Kavita for #{book.book_id} ('{book.title}'): {round(ko_rec.percentage * 100, 1)}%")

        return pulled_count

    async def reconcile_hub(self) -> int:
        """
        Reconciliation: Runs internal Hub reconciliation:
          1. Resolves missing Calibre IDs / titles for documents pushed by e-readers (CrossPoint / KOReader).
          2. Merges duplicate tracked_documents sharing the same Calibre ID into a single winning record.
        """
        reconciled = 0
        if self.calibre and hasattr(self.calibre, "get_book_by_id"):
            repaired = self.db.repair_missing_titles(
                self.calibre.get_book_by_id,
                getattr(self.calibre, "find_book_by_hash", None),
                getattr(self.calibre, "find_book_by_filename_hash", None),
            )
            reconciled += repaired
            if repaired > 0:
                logger.info(f"Reconciled {repaired} documents with Calibre database metadata.")

        merged = self.db.merge_duplicate_calibre_entries()
        reconciled += merged
        if merged > 0:
            logger.info(f"Merged {merged} duplicate Calibre document entries in Hub.")

        return reconciled

    async def push_hub_to_calibre(self) -> int:
        """
        Egress: Pushes reading progress from Hub's tracked_documents out to Calibre DB.
        Only updates Calibre if Hub's progress is ahead or has newer progress.
        """
        if not self.calibre:
            return 0

        pushed_count = 0
        now_ts = int(datetime.utcnow().timestamp())
        all_docs = self.db.get_all_tracked_documents()

        for doc in all_docs:
            cal_id = doc["calibre_book_id"]
            pct = float(doc["percentage"] or 0.0)
            if not cal_id or pct <= 0:
                continue

            cal_book = self.calibre.get_book_by_id(cal_id)
            if not cal_book:
                continue

            if (
                pct > cal_book.percentage
                or (pct == cal_book.percentage and pct > 0 and cal_book.koreader_progress != doc["progress"])
            ):
                logger.info(
                    f"Pushing from Hub to Calibre for book #{cal_id} ('{cal_book.title}'): "
                    f"{round(cal_book.percentage * 100, 1)}% -> {round(pct * 100, 1)}%"
                )
                ok = self.calibre.update_book_progress(
                    book_id=cal_id,
                    percentage=pct,
                    progress_str=doc["progress"],
                    timestamp=doc["timestamp"] or now_ts,
                )
                if ok:
                    pushed_count += 1
                    self.db.update_sync_timestamps(
                        calibre_id=cal_id,
                        calibre_synced_at=now_ts,
                        sync_status="synced_to_calibre",
                    )
                    self.db.log_sync_event(
                        SyncEvent(
                            document=doc["document"],
                            calibre_id=cal_id,
                            source="hub",
                            target="calibre",
                            progress=doc["progress"],
                            percentage=pct,
                            timestamp=now_ts,
                            success=True,
                            message=f"Pushed progress from Hub to Calibre #{cal_id} ({round(pct * 100, 1)}%)",
                        )
                    )

        return pushed_count

    async def push_hub_to_kavita(self) -> int:
        """
        Egress: Pushes reading progress from Hub's tracked_documents out to Kavita (KOReader endpoint & WebUI).
        Only updates Kavita if Hub's progress is ahead or needs syncing.
        """
        if not self.kavita:
            return 0

        pushed_count = 0
        now_ts = int(datetime.utcnow().timestamp())
        all_docs = self.db.get_all_tracked_documents()

        for doc in all_docs:
            cal_id = doc["calibre_book_id"]
            pct = float(doc["percentage"] or 0.0)
            if not cal_id or pct <= 0:
                continue

            hsh = doc["document"]
            if cal_id and self.calibre:
                cal_book = self.calibre.get_book_by_id(cal_id)
                if cal_book and cal_book.koreader_hash:
                    hsh = cal_book.koreader_hash

            kavita_rec = await self.kavita.get_progress(hsh)
            kavita_pct = kavita_rec.percentage if kavita_rec else 0.0

            kavita_synced = doc["kavita_synced_at"]
            doc_ts = doc["timestamp"] or 0

            # Needs push if Hub percentage is ahead of Kavita KOReader endpoint,
            # or if never synced to Kavita, or if Hub timestamp is newer than last Kavita sync
            needs_push = (
                pct > kavita_pct
                or not kavita_synced
                or (doc_ts > kavita_synced and pct >= kavita_pct)
            )

            if needs_push:
                logger.info(
                    f"Pushing from Hub to Kavita for book #{cal_id} ('{doc['title'] or ''}'): "
                    f"{round(kavita_pct * 100, 1)}% -> {round(pct * 100, 1)}%"
                )
                rec = ProgressRecord(
                    document=hsh,
                    progress=doc["progress"] or f"page:{round(pct * 100, 1)}%",
                    percentage=pct,
                    timestamp=doc_ts or now_ts,
                    device=doc["device"] or "Hub",
                    title=doc["title"],
                    authors=doc["authors"],
                    filename=doc["filename"] or f"{doc['title'] or 'Book'} {{{cal_id}}}.epub",
                    calibre_id=cal_id,
                )
                try:
                    ok = await self.kavita.update_progress(rec)
                    if ok:
                        pushed_count += 1
                        self.db.update_sync_timestamps(
                            calibre_id=cal_id,
                            kavita_synced_at=now_ts,
                            sync_status="synced_to_kavita",
                        )
                        self.db.log_sync_event(
                            SyncEvent(
                                document=hsh,
                                calibre_id=cal_id,
                                source="hub",
                                target="kavita",
                                progress=rec.progress,
                                percentage=pct,
                                timestamp=now_ts,
                                success=True,
                                message=f"Pushed progress from Hub to Kavita #{cal_id} ({round(pct * 100, 1)}%)",
                            )
                        )
                except Exception as e:
                    logger.warning(f"Failed to push hub record #{cal_id} to Kavita: {e}")

        return pushed_count

    async def sync_hub_to_remotes(self) -> int:
        """Pushes Hub tracked progress out to both Calibre and Kavita."""
        p_cal = await self.push_hub_to_calibre()
        p_kav = await self.push_hub_to_kavita()
        return p_cal + p_kav

    async def sync_kavita_to_calibre(self) -> int:
        """Pulls recent reads from Kavita into Hub, reconciles, and pushes winning progress to Calibre."""
        await self.pull_kavita_to_hub()
        await self.reconcile_hub()
        return await self.push_hub_to_calibre()

    async def sync_calibre_to_kavita(self) -> int:
        """Pulls recent reads from Calibre into Hub, reconciles, and pushes winning progress to Kavita."""
        await self.pull_calibre_to_hub()
        await self.reconcile_hub()
        return await self.push_hub_to_kavita()

    async def backfill_calibre_books(self) -> int:
        """
        Backfills all books with reading progress from Calibre into the internal database (Hub)
        and syncs them to Kavita (WebUI & KOReader endpoint).
        """
        if not self.calibre:
            return 0

        books = []
        if hasattr(self.calibre, "get_all_books_with_progress"):
            books = self.calibre.get_all_books_with_progress()
        elif hasattr(self.calibre, "get_recently_read_books"):
            books = self.calibre.get_recently_read_books(limit=10000)

        logger.info(f"Starting backfill: found {len(books)} books with progress in Calibre...")
        count = 0
        now_ts = int(datetime.utcnow().timestamp())

        for b in books:
            if b.percentage <= 0 and not b.is_read:
                continue

            pct = 1.0 if b.is_read and b.percentage < 0.98 else b.percentage
            hsh = b.koreader_hash or f"calibre_{b.book_id}"
            prog_str = b.koreader_progress or (f"page:{round(pct * 100, 1)}%" if pct > 0 else "0")

            rec = ProgressRecord(
                document=hsh,
                progress=prog_str,
                percentage=pct,
                timestamp=now_ts,
                device="Calibre",
                title=b.title,
                authors=b.authors,
                filename=f"{b.title} {{{b.book_id}}}.{b.format.lower() if b.format else 'epub'}",
                calibre_id=b.book_id,
            )

            # Insert/update in local database
            self.db.upsert_progress(
                record=rec,
                calibre_book_id=b.book_id,
                calibre_synced_at=now_ts,
                sync_status="synced",
            )
            self.db.update_sync_state(
                calibre_id=b.book_id,
                percentage=pct,
                progress=prog_str,
                source="calibre_backfill",
                synced_at=now_ts,
            )

            # Optionally push to Kavita if configured
            if self.kavita:
                try:
                    await self.kavita.update_progress(rec)
                except Exception as e:
                    logger.debug(f"Kavita update during backfill for #{b.book_id}: {e}")

            count += 1

        self.db.log_sync_event(
            SyncEvent(
                document="backfill",
                source="calibre_db",
                target="hub_db",
                progress="100%",
                percentage=1.0,
                timestamp=now_ts,
                success=True,
                message=f"Backfilled {count} books from Calibre DB to Books & Reading progress",
            )
        )
        logger.info(f"Backfill complete: imported {count} books into Books & Reading progress overview.")
        return count

    async def sync_all(self) -> Dict[str, Any]:
        """
        Runs a complete Star-Topology sync pass:
        Phase 1: Ingress (Spokes -> Hub)
          - Pull Calibre -> Hub
          - Pull Kavita -> Hub
        Phase 2: Reconciliation (Hub internal)
          - Reconcile missing Calibre IDs / titles & merge duplicate hashes
        Phase 3: Egress (Hub -> Spokes)
          - Push Hub -> Calibre
          - Push Hub -> Kavita
        """
        logger.info("Starting Star-Topology sync pass between Spokes and Hub...")
        pulled_calibre = 0
        pulled_kavita = 0
        reconciled = 0
        pushed_calibre = 0
        pushed_kavita = 0
        error_msg = None

        try:
            # Phase 1: Ingress
            pulled_calibre = await self.pull_calibre_to_hub()
            pulled_kavita = await self.pull_kavita_to_hub()

            # Phase 2: Reconciliation
            reconciled = await self.reconcile_hub()

            # Phase 3: Egress
            pushed_calibre = await self.push_hub_to_calibre()
            pushed_kavita = await self.push_hub_to_kavita()

            self.last_sync_status = "success"
            self.sync_count += 1
        except Exception as e:
            logger.error(f"Error during Hub-and-Spoke sync: {e}", exc_info=True)
            self.last_sync_status = f"error: {str(e)}"
            error_msg = str(e)

        self.last_sync_timestamp = int(datetime.utcnow().timestamp())
        logger.info(
            f"Hub-and-Spoke sync completed: "
            f"pulled Calibre: {pulled_calibre}, pulled Kavita: {pulled_kavita}, "
            f"reconciled: {reconciled}, "
            f"pushed Calibre: {pushed_calibre}, pushed Kavita: {pushed_kavita}."
        )
        return {
            "pulled_calibre": pulled_calibre,
            "pulled_kavita": pulled_kavita,
            "reconciled": reconciled,
            "pushed_calibre": pushed_calibre,
            "pushed_kavita": pushed_kavita,
            # Backward compatibility aliases
            "updated_calibre": pushed_calibre,
            "updated_kavita": pushed_kavita,
            "updated_hub": (pushed_calibre + pushed_kavita),
            "timestamp": self.last_sync_timestamp,
            "status": self.last_sync_status,
            "error": error_msg,
        }

    async def index_filename_hashes_task(self, force: bool = False):
        """Indexes filename hashes into internal database incrementally in a worker thread."""
        if not self.calibre or not hasattr(self.calibre, "index_filename_hashes"):
            return
        try:
            await asyncio.to_thread(self.calibre.index_filename_hashes, self.db, not force)
        except Exception as e:
            logger.warning(f"Background filename hash indexing error: {e}")

    async def start_background_loop(self):
        """Runs periodic background synchronization."""
        self._is_running = True
        logger.info(f"Background bidirectional sync loop started (interval: {self.interval_seconds}s).")
        # Initial incremental filename hash indexing in background
        if self.calibre and hasattr(self.calibre, "index_filename_hashes"):
            asyncio.create_task(self.index_filename_hashes_task())

        while self._is_running:
            try:
                await self.sync_all()
                if self.calibre and hasattr(self.calibre, "index_filename_hashes"):
                    await self.index_filename_hashes_task()
            except Exception as e:
                logger.error(f"Unexpected error in sync loop: {e}")
            await asyncio.sleep(self.interval_seconds)

    def stop(self):
        """Stops background synchronization."""
        self._is_running = False
        if self._task and not self._task.done():
            self._task.cancel()
        logger.info("Bidirectional synchronizer stopped.")
