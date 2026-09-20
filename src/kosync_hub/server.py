"""FastAPI server implementing KOReader sync protocol, Kavita VFS generator, and hub proxy."""

import asyncio
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from .config import AppConfig, get_active_config_path, save_config
from .db import InternalDatabase
from .hasher import compute_filename_md5
from .models import HubStatus, ProgressPayload, ProgressRecord, ProgressResponse, SyncEvent, UserAuthRequest
from .synchronizer import Synchronizer
from .ui import enrich_tracked_documents, render_dashboard_html
from .vfs import VFSManager, state as default_vfs_state

logger = logging.getLogger("kosync_hub.server")


def extract_calibre_id_from_filename(filename: Optional[str]) -> Optional[int]:
    """Extracts a potential Calibre ID from filename using various common patterns."""
    if not filename:
        return None
    patterns = [
        r"\{(\d+)\}",
        r"\((\d+)\)",
        r"\[(\d+)\]",
        r"[-_](\d+)\b",
    ]
    for pat in patterns:
        m = re.search(pat, filename)
        if m:
            try:
                val = int(m.group(1))
                if 0 < val < 10000000:
                    return val
            except ValueError:
                continue
    return None


from contextlib import asynccontextmanager

def create_app(
    config: AppConfig,
    db: InternalDatabase,
    synchronizer: Synchronizer,
    vfs_manager: Optional[VFSManager] = None,
) -> FastAPI:
    """Factory creating configured FastAPI app for kosync-hub."""
    start_time = time.time()
    vfs = vfs_manager or VFSManager(config)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logger.info("Initializing kosync-hub server...")
        # Verify provider connections
        if synchronizer.kavita:
            asyncio.create_task(synchronizer.kavita.test_connection())
        if synchronizer.calibre:
            synchronizer.calibre.internal_db = db
            asyncio.create_task(synchronizer.calibre.test_connection())

        # Scan Calibre library if enabled
        if config.sync.scan_on_startup and hasattr(synchronizer.calibre, "scan_and_index_library"):
            loop = asyncio.get_event_loop()
            loop.run_in_executor(None, synchronizer.calibre.scan_and_index_library)

        # Repair any missing/unknown titles from Calibre DB
        if synchronizer.calibre and hasattr(synchronizer.calibre, "get_book_by_id"):
            loop = asyncio.get_event_loop()
            loop.run_in_executor(
                None,
                db.repair_missing_titles,
                synchronizer.calibre.get_book_by_id,
                getattr(synchronizer.calibre, "find_book_by_hash", None),
                getattr(synchronizer.calibre, "find_book_by_filename_hash", None),
            )

        # Start KOReader background sync worker if enabled
        if config.sync.enabled:
            asyncio.create_task(synchronizer.start_background_loop())
        else:
            logger.info("KOReader reading progress sync is disabled in configuration.")

        # Start Kavita VFS background worker if enabled
        if config.vfs.enabled:
            asyncio.create_task(vfs.start_background_loop())
        else:
            logger.info("Kavita VFS is disabled in configuration.")

        try:
            yield
        finally:
            synchronizer.stop()
            vfs.stop()

    app = FastAPI(
        title="KOReader Sync & Kavita VFS Hub",
        description="Dual-sync proxy, KOReader cloud sync, and Kavita VFS bridge for Calibre",
        version="1.1.0",
        lifespan=lifespan,
    )

    # -------------------------------------------------------------------------
    # KOReader Sync Protocol Endpoints (Single-User open auth)
    # -------------------------------------------------------------------------

    async def _handle_auth(user: Optional[str] = None):
        """Single-user KOReader authorization check (accepts any credentials)."""
        logger.info(f"KOReader auth check received from user='{user or 'anonymous'}'")
        return {"authorized": "OK"}

    @app.post("/users/create", status_code=status.HTTP_201_CREATED)
    @app.post("/users/create/", status_code=status.HTTP_201_CREATED)
    @app.post("/koreader/users/create", status_code=status.HTTP_201_CREATED)
    @app.post("/koreader/users/create/", status_code=status.HTTP_201_CREATED)
    @app.post("/sync/users/create", status_code=status.HTTP_201_CREATED)
    @app.post("/sync/users/create/", status_code=status.HTTP_201_CREATED)
    async def create_user(req: Optional[UserAuthRequest] = None):
        user = req.username if req else None
        return await _handle_auth(user)

    @app.get("/users/auth")
    @app.get("/users/auth/")
    @app.get("/koreader/users/auth")
    @app.get("/koreader/users/auth/")
    @app.get("/sync/users/auth")
    @app.get("/sync/users/auth/")
    async def auth_user(x_auth_user: Optional[str] = Header(None)):
        return await _handle_auth(x_auth_user)

    async def _handle_update_progress(payload: ProgressPayload):
        """Processes incoming progress updates from KOReader / CrossPoint."""
        logger.info(
            f"Incoming progress update for document={payload.document}: "
            f"progress='{payload.progress}', percentage={payload.percentage}, device='{payload.device}'"
        )
        now_ts = payload.timestamp or int(datetime.utcnow().timestamp())

        title = payload.metadata.title if payload.metadata else None
        authors = payload.metadata.authors if payload.metadata else None
        filename = payload.metadata.filename if payload.metadata else None

        calibre_id = getattr(payload, "calibre_id", None)
        if not calibre_id and filename:
            calibre_id = extract_calibre_id_from_filename(filename)

        if not calibre_id:
            calibre_id = db.get_calibre_id_for_document(payload.document)

        if not calibre_id and synchronizer.calibre and hasattr(synchronizer.calibre, "find_book_by_hash"):
            calibre_id = synchronizer.calibre.find_book_by_hash(payload.document)
            if not calibre_id and hasattr(synchronizer.calibre, "find_book_by_filename_hash"):
                calibre_id = synchronizer.calibre.find_book_by_filename_hash(payload.document)

        if calibre_id:
            db.link_document_alias(payload.document, calibre_id, payload.device or "KOReader")

        canonical_hash = payload.document
        if calibre_id and synchronizer.calibre and hasattr(synchronizer.calibre, "get_book_by_id"):
            cal_b = synchronizer.calibre.get_book_by_id(calibre_id)
            if cal_b:
                title = title or cal_b.title
                authors = authors or cal_b.authors
                canonical_hash = cal_b.koreader_hash or payload.document

        record = ProgressRecord(
            document=payload.document,
            progress=payload.progress,
            percentage=payload.percentage,
            timestamp=now_ts,
            device=payload.device or "KOReader",
            device_id=payload.device_id,
            title=title,
            authors=authors,
            filename=filename,
            calibre_id=calibre_id,
        )

        db.upsert_progress(record, calibre_book_id=calibre_id)
        if calibre_id:
            db.update_sync_state(
                calibre_id=calibre_id,
                percentage=record.percentage,
                progress=record.progress,
                source=record.device or "CrossPoint",
                synced_at=now_ts,
            )

        if synchronizer.calibre:
            try:
                await synchronizer.calibre.update_progress(record)
            except Exception as e:
                logger.error(f"Error updating Calibre DB for {payload.document}: {e}")

        if synchronizer.kavita:
            kavita_rec = record.model_copy(update={"document": canonical_hash}) if canonical_hash else record
            asyncio.create_task(synchronizer.kavita.update_progress(kavita_rec))

        db.log_sync_event(
            SyncEvent(
                document=payload.document,
                calibre_id=calibre_id,
                source=payload.device or "KOReader",
                target="calibre+kavita",
                progress=payload.progress,
                percentage=payload.percentage,
                timestamp=now_ts,
                success=True,
                message=f"Received progress from {payload.device or 'device'} for #{calibre_id or '-'}",
            )
        )

        logger.info(f"Progress recorded successfully for {payload.document} (#{calibre_id or '-'})")
        return {
            "document": payload.document,
            "timestamp": now_ts,
            "status": "ok",
        }

    @app.put("/syncs/progress", status_code=status.HTTP_200_OK)
    @app.put("/syncs/progress/", status_code=status.HTTP_200_OK)
    @app.put("/koreader/syncs/progress", status_code=status.HTTP_200_OK)
    @app.put("/koreader/syncs/progress/", status_code=status.HTTP_200_OK)
    @app.put("/sync/syncs/progress", status_code=status.HTTP_200_OK)
    @app.put("/sync/syncs/progress/", status_code=status.HTTP_200_OK)
    @app.put("/sync/progress", status_code=status.HTTP_200_OK)
    @app.put("/sync/progress/", status_code=status.HTTP_200_OK)
    async def update_progress(payload: ProgressPayload):
        return await _handle_update_progress(payload)

    async def _handle_get_progress(document: str):
        """Retrieves current reading progress for KOReader / CrossPoint."""
        logger.info(f"Incoming progress request for document={document}")
        await synchronizer.sync_document(document)

        record = db.get_document(document)
        if not record:
            calibre_id = db.get_calibre_id_for_document(document)
            if not calibre_id and synchronizer.calibre and hasattr(synchronizer.calibre, "find_book_by_hash"):
                calibre_id = synchronizer.calibre.find_book_by_hash(document)
                if not calibre_id and hasattr(synchronizer.calibre, "find_book_by_filename_hash"):
                    calibre_id = synchronizer.calibre.find_book_by_filename_hash(document)
                if calibre_id:
                    db.link_document_alias(document, calibre_id, "KOReader")

            if calibre_id and synchronizer.calibre and hasattr(synchronizer.calibre, "get_book_by_id"):
                cal_book = synchronizer.calibre.get_book_by_id(calibre_id)
                if cal_book:
                    return ProgressResponse(
                        document=document,
                        progress=cal_book.koreader_progress or f"page:{cal_book.percentage}",
                        percentage=cal_book.percentage,
                        timestamp=int(datetime.utcnow().timestamp()),
                        device="Calibre",
                    )
            return Response(status_code=status.HTTP_204_NO_CONTENT)

        return ProgressResponse(
            document=record.document,
            progress=record.progress,
            percentage=record.percentage,
            timestamp=record.timestamp,
            device=record.device,
            device_id=record.device_id,
        )

    @app.get("/syncs/progress/{document:path}")
    @app.get("/koreader/syncs/progress/{document:path}")
    @app.get("/sync/syncs/progress/{document:path}")
    @app.get("/sync/progress/{document:path}")
    async def get_progress(document: str):
        return await _handle_get_progress(document)

    # -------------------------------------------------------------------------
    # General Hub & Diagnostics Endpoints
    # -------------------------------------------------------------------------

    @app.get("/healthcheck")
    async def healthcheck():
        return {
            "status": "healthy",
            "timestamp": int(datetime.utcnow().timestamp()),
            "uptime_seconds": int(time.time() - start_time),
            "tracked_documents": db.count_tracked(),
        }

    @app.get("/status", response_model=HubStatus)
    async def get_status():
        calibre_ok = await synchronizer.calibre.test_connection() if synchronizer.calibre else False
        kavita_ok = await synchronizer.kavita.test_connection() if synchronizer.kavita else False
        return HubStatus(
            status="running",
            version="1.1.0",
            uptime_seconds=int(time.time() - start_time),
            tracked_documents=db.count_tracked(),
            kavita_connected=kavita_ok,
            calibre_connected=calibre_ok,
            last_sync_timestamp=synchronizer.last_sync_timestamp,
            last_sync_status=synchronizer.last_sync_status,
        )

    # -------------------------------------------------------------------------
    # KOReader Progress Sync Management Endpoints
    # -------------------------------------------------------------------------

    @app.post("/api/sync-now")
    async def api_sync_now():
        res = await synchronizer.sync_all()
        if synchronizer.calibre and hasattr(synchronizer.calibre, "get_book_by_id"):
            db.repair_missing_titles(
                synchronizer.calibre.get_book_by_id,
                getattr(synchronizer.calibre, "find_book_by_hash", None),
                getattr(synchronizer.calibre, "find_book_by_filename_hash", None),
            )
        return {"status": "completed", "result": res}

    @app.post("/api/backfill")
    async def api_backfill():
        count = await synchronizer.backfill_calibre_books()
        if synchronizer.calibre and hasattr(synchronizer.calibre, "get_book_by_id"):
            db.repair_missing_titles(
                synchronizer.calibre.get_book_by_id,
                getattr(synchronizer.calibre, "find_book_by_hash", None),
                getattr(synchronizer.calibre, "find_book_by_filename_hash", None),
            )
        db.merge_duplicate_calibre_entries()
        asyncio.create_task(synchronizer.index_filename_hashes_task(force=False))
        return {"status": "completed", "backfilled_count": count}

    @app.post("/api/link-document")
    async def api_link_document(payload: dict):
        doc = payload.get("document")
        calibre_id = payload.get("calibre_id")
        if not doc or calibre_id is None:
            raise HTTPException(status_code=400, detail="Missing document or calibre_id")
        try:
            calibre_id = int(calibre_id)
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="Invalid calibre_id")

        db.link_document_alias(doc, calibre_id)
        title = None
        authors = None
        if synchronizer.calibre and hasattr(synchronizer.calibre, "get_book_by_id"):
            cal_b = synchronizer.calibre.get_book_by_id(calibre_id)
            if cal_b:
                title = cal_b.title
                authors = cal_b.authors

        with db._get_connection() as conn:
            conn.execute(
                """
                UPDATE tracked_documents
                SET calibre_book_id = ?,
                    title = COALESCE(?, title),
                    authors = COALESCE(?, authors)
                WHERE document = ?
                """,
                (calibre_id, title, authors, doc),
            )
            conn.commit()

        db.merge_duplicate_calibre_entries()
        return {"status": "ok", "calibre_id": calibre_id, "title": title}

    @app.get("/api/books")
    async def api_books(page: int = 1, page_size: int = 15, search: Optional[str] = None):
        rows, total = db.get_paginated_documents(page=page, page_size=page_size, search=search)
        return {
            "items": [dict(r) for r in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    @app.get("/api/events")
    async def api_events(page: int = 1, page_size: int = 15):
        rows, total = db.get_paginated_events(page=page, page_size=page_size)
        return {
            "items": [dict(r) for r in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    @app.get("/api/stats")
    async def api_stats():
        return db.get_reading_stats()

    # -------------------------------------------------------------------------
    # Kavita VFS Endpoints
    # -------------------------------------------------------------------------

    @app.get("/api/vfs/status")
    async def api_vfs_status():
        return vfs.state.get_summary()

    @app.get("/api/vfs/books")
    async def api_vfs_books(q: str = "", limit: int = 50, offset: int = 0):
        return vfs.state.get_items(query=q, limit=limit, offset=offset)

    @app.post("/api/vfs/sync")
    async def api_vfs_sync():
        res = await vfs.sync_now(force=True)
        return res

    @app.post("/api/vfs/cleanup")
    async def api_vfs_cleanup():
        res = await vfs.cleanup()
        return res

    # -------------------------------------------------------------------------
    # WebUI Configuration Management Endpoints
    # -------------------------------------------------------------------------

    @app.get("/api/config")
    async def api_get_config():
        data = config.model_dump()
        # Mask passwords for display
        if data.get("server", {}).get("auth_password"):
            data["server"]["auth_password"] = "******"
        return {
            "config": data,
            "active_file": str(get_active_config_path()),
        }

    @app.post("/api/config")
    async def api_post_config(payload: dict):
        nonlocal config
        try:
            if "config" in payload and isinstance(payload["config"], dict):
                payload = payload["config"]

            # Preserve existing password if masked
            current_pwd = config.server.auth_password
            if payload.get("server", {}).get("auth_password") == "******":
                payload["server"]["auth_password"] = current_pwd

            new_config = AppConfig(**payload)
            saved_path = save_config(new_config)

            # Hot-reconfigure running instances
            for field_name in AppConfig.model_fields.keys():
                setattr(config, field_name, getattr(new_config, field_name))
            app.state.config = config
            vfs.reconfigure(config)

            return {
                "status": "success",
                "saved_to": saved_path,
                "message": "Configuration successfully saved and applied.",
            }
        except Exception as e:
            logger.exception("Error saving configuration: %s", e)
            raise HTTPException(status_code=400, detail=str(e))

    # -------------------------------------------------------------------------
    # Database Maintenance Endpoints
    # -------------------------------------------------------------------------

    @app.get("/api/database/status")
    async def api_database_status():
        counts = db.get_table_counts()
        return {
            "status": "success",
            "db_path": str(db.db_path),
            "size_bytes": counts.get("file_size_bytes", 0),
            "size_mb": round(counts.get("file_size_bytes", 0) / (1024 * 1024), 2),
            "counts": counts,
        }

    @app.post("/api/database/cleanup")
    async def api_database_cleanup(payload: Optional[dict] = None):
        opts = payload or {}
        days = int(opts.get("retention_days", 14))
        max_ev = int(opts.get("max_events", 5000))
        clear_hashes = bool(opts.get("clear_hashes", False))
        vacuum = bool(opts.get("vacuum", True))

        res = db.cleanup_all(
            retention_days=days,
            max_events=max_ev,
            clear_hashes=clear_hashes,
            vacuum_db=vacuum,
        )
        return res

    # -------------------------------------------------------------------------
    # Library Statistics & Reading History Endpoints
    # -------------------------------------------------------------------------

    @app.get("/api/stats/library")
    async def api_stats_library():
        if not synchronizer.calibre:
            return {"error": "Calibre integration is not enabled"}
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, synchronizer.calibre.get_library_statistics)

    @app.get("/api/stats/reading")
    async def api_stats_reading(year: Optional[str] = None):
        if not synchronizer.calibre:
            return {"error": "Calibre integration is not enabled"}
        y_val = int(year) if year and year.isdigit() else (None if not year or year == "all" else None)
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, synchronizer.calibre.get_reading_history_analytics, y_val)

    # -------------------------------------------------------------------------
    # WebUI Dashboard (Tabbed: Sync, VFS, Stats, Settings)
    # -------------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(
        request: Request,
        page: int = 1,
        event_page: int = 1,
        search: Optional[str] = None,
        tab: Optional[str] = None,
    ):
        base_url = str(request.base_url).rstrip("/")
        stats = db.get_reading_stats()
        vfs_summary = vfs.state.get_summary()

        # Database size and row counts
        db_counts = db.get_table_counts()
        db_size_mb = round(db_counts.get("file_size_bytes", 0) / (1024 * 1024), 2)

        # Library analytics and reading history
        lib_stats = {}
        reading_analytics = {}
        if synchronizer.calibre:
            try:
                lib_stats = synchronizer.calibre.get_library_statistics()
            except Exception as e:
                logger.debug(f"Error fetching library stats: {e}")
            try:
                reading_analytics = synchronizer.calibre.get_reading_history_analytics()
            except Exception as e:
                logger.debug(f"Error fetching reading analytics: {e}")

        page_size = 12
        tracked_docs, total_docs = db.get_paginated_documents(page=page, page_size=page_size, search=search)
        recent_events, total_events = db.get_paginated_events(page=event_page, page_size=page_size)

        total_pages = max(1, (total_docs + page_size - 1) // page_size)
        event_total_pages = max(1, (total_events + page_size - 1) // page_size)

        tracked_docs = enrich_tracked_documents(db, synchronizer, tracked_docs)
        active_config_file = str(get_active_config_path())

        html = render_dashboard_html(
            config=config,
            base_url=base_url,
            stats=stats,
            vfs_summary=vfs_summary,
            db_counts=db_counts,
            lib_stats=lib_stats,
            tracked_docs=tracked_docs,
            recent_events=recent_events,
            page=page,
            total_pages=total_pages,
            total_docs=total_docs,
            event_page=event_page,
            event_total_pages=event_total_pages,
            search=search,
            tab=tab,
            active_config_file=active_config_file,
            db=db,
        )
        return HTMLResponse(content=html)

    return app
