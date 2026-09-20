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
    # WebUI Dashboard (Tabbed: Sync, VFS, Settings)
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

        page_size = 12
        tracked_docs, total_docs = db.get_paginated_documents(page=page, page_size=page_size, search=search)
        recent_events, total_events = db.get_paginated_events(page=event_page, page_size=page_size)

        total_pages = max(1, (total_docs + page_size - 1) // page_size)
        event_total_pages = max(1, (total_events + page_size - 1) // page_size)

        rows_html_list = []
        for d in tracked_docs:
            d_dict = dict(d)
            t_title = d_dict.get("title") or "Unknown"
            t_authors = d_dict.get("authors") or ""
            t_cal_id = d_dict.get("calibre_book_id")
            t_doc = str(d_dict.get("document", ""))
            t_device = d_dict.get("device") or "KOReader"

            if not t_cal_id:
                t_cal_id = db.get_calibre_id_for_document(t_doc) or db.get_calibre_id_by_filename_hash(t_doc)
                if not t_cal_id and synchronizer.calibre and hasattr(synchronizer.calibre, "find_book_by_filename_hash"):
                    t_cal_id = synchronizer.calibre.find_book_by_filename_hash(t_doc)
                if t_cal_id:
                    db.link_document_alias(t_doc, t_cal_id, t_device)
                    with db._get_connection() as c:
                        c.execute("UPDATE tracked_documents SET calibre_book_id = ? WHERE document = ?", (t_cal_id, t_doc))
                        c.commit()

            if (not t_title or t_title == "Unknown") and t_cal_id and synchronizer.calibre and hasattr(synchronizer.calibre, "get_book_by_id"):
                cal_b = synchronizer.calibre.get_book_by_id(t_cal_id)
                if cal_b and cal_b.title:
                    t_title = cal_b.title
                    t_authors = t_authors or cal_b.authors
                    db.update_document_metadata(t_doc, t_title, t_authors)

            pct = float(d_dict.get("percentage", 0.0))
            ts = d_dict.get("timestamp", 0)
            time_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "-"
            t_status = d_dict.get("last_sync_status") or "synced"

            aliases = db.get_aliases_for_calibre_id(t_cal_id) if t_cal_id else []
            other_aliases = [a for a in aliases if a != t_doc]
            alias_badge = f""" <span class="badge badge-info" title="Alternative linked hashes:\n{chr(10).join(other_aliases)}" style="cursor:help; font-size:0.75rem;">+{len(other_aliases)}</span>""" if other_aliases else ""
            cal_badge = f"""<span class="badge badge-primary">#{t_cal_id}</span>""" if t_cal_id else f"""<button class="btn" style="padding:2px 8px; font-size:0.75rem; background:#f59e0b; color:#fff; border:none; border-radius:4px; cursor:pointer;" onclick="linkDocument('{t_doc}')" title="Click to manually link to Calibre ID">#- (Link)</button>"""

            rows_html_list.append(
                f"""<tr>
                    <td><strong>{t_title}</strong><br><small style="color:#94a3b8;">{t_authors}</small></td>
                    <td>{cal_badge}</td>
                    <td><code title="{t_doc}">{t_doc[:10]}...</code>{alias_badge}</td>
                    <td><span class="badge badge-info">{t_device}</span></td>
                    <td>
                        <div style="display:flex; align-items:center; gap:8px;">
                            <div class="progress-bar"><div class="fill" style="width: {min(100, round(pct * 100))}%"></div></div>
                            <span style="font-weight:600; font-size:0.85rem;">{round(pct * 100, 1)}%</span>
                        </div>
                    </td>
                    <td><small style="color:#94a3b8;">{time_str}</small></td>
                    <td><span class="badge badge-success">{t_status}</span></td>
                </tr>"""
            )
        rows_html = "".join(rows_html_list)

        events_html_list = []
        for e in recent_events:
            e_dict = dict(e)
            ts = e_dict.get("timestamp", 0)
            time_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else "-"
            cal_id = e_dict.get("calibre_id")
            doc = str(e_dict.get("document", ""))
            ident = f"#{cal_id}" if cal_id else (doc[:8] + "..." if doc else "-")
            pct = round(float(e_dict.get("percentage", 0.0)) * 100, 1)
            src = e_dict.get("source", "unknown")
            tgt = e_dict.get("target", "unknown")
            success = e_dict.get("success", False)
            msg = e_dict.get("message") or ""
            events_html_list.append(
                f"""<tr>
                    <td><small style="color:#94a3b8;">{time_str}</small></td>
                    <td><span class="badge badge-info">{src}</span> → <span class="badge badge-primary">{tgt}</span></td>
                    <td><strong>{ident}</strong></td>
                    <td>{pct}%</td>
                    <td>{'✅' if success else '❌'} <span style="font-size:0.85rem;">{msg}</span></td>
                </tr>"""
            )
        events_html = "".join(events_html_list)

        search_query_part = f"&search={search}" if search else ""
        prev_page_link = f"/?page={max(1, page - 1)}{search_query_part}&event_page={event_page}#sync"
        next_page_link = f"/?page={min(total_pages, page + 1)}{search_query_part}&event_page={event_page}#sync"
        prev_event_link = f"/?page={page}{search_query_part}&event_page={max(1, event_page - 1)}#sync"
        next_event_link = f"/?page={page}{search_query_part}&event_page={min(event_total_pages, event_page + 1)}#sync"
        search_val = search or ""

        active_tab = tab or "sync"
        active_config_file = str(get_active_config_path())

        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>KOReader Sync Hub & Kavita VFS</title>
    <style>
        :root {{
            --bg: #0f172a;
            --card-bg: #1e293b;
            --card-border: #334155;
            --text: #f8fafc;
            --text-muted: #94a3b8;
            --primary: #38bdf8;
            --primary-hover: #0ea5e9;
            --success: #34d399;
            --warning: #fbbf24;
            --danger: #ef4444;
            --font-mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
        }}
        * {{ box-sizing: border-box; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; background: var(--bg); color: var(--text); margin: 0; padding: 2rem; }}
        .container {{ max-width: 1240px; margin: 0 auto; }}
        header {{ display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 1rem; margin-bottom: 1.5rem; padding-bottom: 1rem; border-bottom: 1px solid var(--card-border); }}
        h1 {{ margin: 0; color: var(--primary); font-size: 1.8rem; display: flex; align-items: center; gap: 0.5rem; }}
        .subtitle {{ color: var(--text-muted); font-size: 0.9rem; margin-top: 4px; }}
        
        /* Tabs */
        .tabs-nav {{ display: flex; gap: 8px; border-bottom: 2px solid var(--card-border); margin-bottom: 1.5rem; }}
        .tab-btn {{ background: transparent; color: var(--text-muted); border: none; border-bottom: 3px solid transparent; padding: 10px 20px; font-size: 1rem; font-weight: 600; cursor: pointer; display: flex; align-items: center; gap: 8px; transition: all 0.2s; }}
        .tab-btn:hover {{ color: var(--text); }}
        .tab-btn.active {{ color: var(--primary); border-bottom-color: var(--primary); }}
        .tab-content {{ display: none; }}
        .tab-content.active {{ display: block; }}

        /* Cards and Grids */
        .card {{ background: var(--card-bg); border-radius: 8px; padding: 1.5rem; margin-bottom: 1.5rem; border: 1px solid var(--card-border); }}
        .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 1rem; margin-bottom: 1.5rem; }}
        .stat-card {{ background: var(--card-bg); border-radius: 8px; padding: 1.2rem; border: 1px solid var(--card-border); }}
        .stat-value {{ font-size: 2rem; font-weight: bold; color: var(--primary); margin-bottom: 0.25rem; }}
        .stat-label {{ font-size: 0.8rem; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.05em; }}
        
        table {{ width: 100%; border-collapse: collapse; margin-top: 0.5rem; }}
        th, td {{ padding: 0.75rem; text-align: left; border-bottom: 1px solid var(--card-border); }}
        th {{ color: var(--text-muted); font-weight: 600; font-size: 0.85rem; text-transform: uppercase; }}
        .badge {{ padding: 0.2rem 0.5rem; border-radius: 4px; font-size: 0.75rem; font-weight: 600; display: inline-block; }}
        .badge-success {{ background: #065f46; color: #34d399; }}
        .badge-primary {{ background: #1e40af; color: #93c5fd; }}
        .badge-info {{ background: #374151; color: #e2e8f0; }}
        .badge-warning {{ background: rgba(245, 158, 11, 0.2); color: #fbbf24; }}
        .badge-danger {{ background: rgba(239, 68, 68, 0.2); color: #f87171; }}
        
        .progress-bar {{ width: 90px; height: 8px; background: #334155; border-radius: 4px; display: inline-block; overflow: hidden; }}
        .fill {{ height: 100%; background: var(--primary); border-radius: 4px; }}
        .btn {{ background: #0284c7; color: white; border: none; padding: 0.5rem 1rem; border-radius: 6px; cursor: pointer; font-weight: 600; text-decoration: none; display: inline-flex; align-items: center; gap: 6px; font-size: 0.9rem; }}
        .btn:hover {{ background: #0369a1; }}
        .btn-secondary {{ background: #334155; color: #f8fafc; padding: 0.4rem 0.8rem; }}
        .btn-secondary:hover {{ background: #475569; }}
        .btn-success {{ background: #059669; color: white; }}
        .btn-success:hover {{ background: #047857; }}
        .btn-disabled {{ opacity: 0.4; pointer-events: none; }}
        .search-input {{ background: #0f172a; border: 1px solid var(--card-border); color: #f8fafc; padding: 0.5rem 0.8rem; border-radius: 6px; width: 260px; font-size: 0.9rem; }}
        .search-input:focus {{ outline: none; border-color: var(--primary); }}
        code {{ background: #0f172a; padding: 0.2rem 0.4rem; border-radius: 4px; color: #e2e8f0; font-size: 0.85rem; font-family: var(--font-mono); }}
        .pagination {{ display: flex; align-items: center; justify-content: space-between; margin-top: 1rem; padding-top: 0.8rem; border-top: 1px solid var(--card-border); font-size: 0.9rem; color: var(--text-muted); }}

        /* Collisions alert box */
        .alert-panel {{ background: rgba(239, 68, 68, 0.15); border: 1px solid var(--danger); border-radius: 8px; padding: 1rem 1.25rem; margin-bottom: 1.5rem; }}
        .alert-panel h3 {{ margin: 0 0 0.5rem 0; color: var(--danger); font-size: 1.05rem; display: flex; align-items: center; gap: 8px; }}

        /* Settings form styling */
        .form-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 1.25rem; }}
        .form-group {{ display: flex; flex-direction: column; gap: 6px; }}
        .form-group label {{ font-size: 0.85rem; font-weight: 600; color: var(--text-muted); }}
        .form-group input, .form-group select {{ background: #0f172a; border: 1px solid var(--card-border); color: #f8fafc; padding: 0.5rem 0.75rem; border-radius: 6px; font-size: 0.9rem; }}
        .form-group input:focus, .form-group select:focus {{ outline: none; border-color: var(--primary); }}
        .form-check {{ display: flex; align-items: center; gap: 8px; margin-top: 4px; }}
        .form-check input {{ width: 16px; height: 16px; accent-color: var(--primary); }}
        .form-section-title {{ color: var(--primary); font-size: 1.1rem; margin: 1.25rem 0 0.75rem 0; padding-bottom: 0.4rem; border-bottom: 1px solid var(--card-border); }}
    </style>
</head>
<body>
    <div class="container">
        <header>
            <div>
                <h1>📖 KOReader Sync Hub & Kavita VFS</h1>
                <div class="subtitle">Unified reading progress synchronization, device sync proxy, and Kavita virtual library generator.</div>
            </div>
            <div style="display: flex; gap: 8px; align-items: center;">
                <span class="badge {'badge-success' if config.sync.enabled else 'badge-info'}">Sync: {'ON' if config.sync.enabled else 'OFF'}</span>
                <span class="badge {'badge-success' if config.vfs.enabled else 'badge-info'}">VFS: {config.vfs.mode.upper() if config.vfs.enabled else 'OFF'}</span>
            </div>
        </header>

        <!-- Navigation Tabs -->
        <div class="tabs-nav">
            <button id="tab-btn-sync" class="tab-btn {'active' if active_tab == 'sync' else ''}" onclick="switchTab('sync')">📱 Reading Sync</button>
            <button id="tab-btn-vfs" class="tab-btn {'active' if active_tab == 'vfs' else ''}" onclick="switchTab('vfs')">🗂️ Kavita VFS</button>
            <button id="tab-btn-settings" class="tab-btn {'active' if active_tab == 'settings' else ''}" onclick="switchTab('settings')">⚙️ Settings</button>
        </div>

        <!-- ============================================================= -->
        <!-- TAB 1: READING SYNC -->
        <!-- ============================================================= -->
        <div id="tab-sync" class="tab-content {'active' if active_tab == 'sync' else ''}">
            <!-- KOReader / CrossPoint Connection Card -->
            <div class="card" style="border-left: 4px solid var(--primary);">
                <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 1rem;">
                    <div>
                        <h3 style="margin: 0 0 0.25rem 0; color: var(--primary); font-size: 1.1rem;">📱 KOReader & CrossPoint Device Sync URL</h3>
                        <p style="margin: 0; color: var(--text-muted); font-size: 0.85rem;">
                            Enter this server URL in your KOReader / CrossPoint Sync settings. Any username/password is accepted.
                        </p>
                    </div>
                    <div style="display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap;">
                        <div style="background: #0f172a; padding: 0.4rem 0.8rem; border-radius: 6px; border: 1px solid var(--card-border); display: flex; align-items: center; gap: 0.5rem;">
                            <span style="color: var(--text-muted); font-size: 0.8rem; text-transform: uppercase; font-weight: 600;">Server URL:</span>
                            <code id="sync-url" style="color: var(--primary); font-weight: bold; font-size: 0.95rem;">{base_url}</code>
                        </div>
                        <button class="btn btn-secondary" onclick="navigator.clipboard.writeText(document.getElementById('sync-url').innerText); this.innerText='Copied!'; setTimeout(() => this.innerText='📋 Copy', 2000);">📋 Copy</button>
                    </div>
                </div>
            </div>

            <!-- Reading History Statistics -->
            <div class="grid">
                <div class="stat-card">
                    <div class="stat-value">{stats['total_tracked']}</div>
                    <div class="stat-label">📚 Tracked Books</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value" style="color: var(--success);">{stats['completed_books']}</div>
                    <div class="stat-label">🏆 Completed (≥98%)</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value" style="color: var(--warning);">{stats['in_progress']}</div>
                    <div class="stat-label">📖 In Progress</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value" style="color: #a78bfa;">{stats['syncs_today']}</div>
                    <div class="stat-label">⚡ Syncs (Today)</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value">{stats['syncs_week']}</div>
                    <div class="stat-label">📅 Syncs (7 Days)</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value" style="color: var(--primary);">{stats['aliases_count']}</div>
                    <div class="stat-label">📱 Connected Devices</div>
                </div>
            </div>

            <!-- Books Table with Search & Pagination -->
            <div class="card">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 1rem; flex-wrap: wrap; gap: 0.5rem;">
                    <div>
                        <h2 style="margin: 0; font-size: 1.25rem;">Books & Reading Progress</h2>
                        <span style="font-size: 0.85rem; color: var(--text-muted);">{total_docs} total books in database</span>
                    </div>
                    <div style="display: flex; gap: 0.5rem; align-items: center; flex-wrap: wrap;">
                        <form method="GET" action="/" style="display: flex; gap: 0.5rem;">
                            <input type="hidden" name="event_page" value="{event_page}">
                            <input type="hidden" name="tab" value="sync">
                            <input type="text" name="search" class="search-input" placeholder="Search title, author, #id..." value="{search_val}">
                            <button class="btn btn-secondary" type="submit">Search</button>
                            {f'<a href="/?event_page={event_page}&tab=sync" class="btn btn-secondary">Clear</a>' if search else ''}
                        </form>
                        <button class="btn btn-secondary" onclick="triggerBackfill(this)">📥 Backfill from Calibre</button>
                        <button class="btn" onclick="triggerSyncNow(this)">🔄 Sync Now</button>
                    </div>
                </div>
                <table>
                    <thead>
                        <tr>
                            <th>Book</th>
                            <th>Calibre ID</th>
                            <th>Document Hash</th>
                            <th>Device</th>
                            <th>Progress</th>
                            <th>Last Activity</th>
                            <th>Status</th>
                        </tr>
                    </thead>
                    <tbody>
                        {rows_html if rows_html else "<tr><td colspan='7' style='color:#94a3b8; text-align:center; padding: 2rem;'>No books found. Read on your Xteink X3 or Kavita to start syncing!</td></tr>"}
                    </tbody>
                </table>
                <div class="pagination">
                    <div>Page {page} of {total_pages}</div>
                    <div style="display: flex; gap: 0.5rem;">
                        <a href="{prev_page_link}" class="btn btn-secondary {'btn-disabled' if page <= 1 else ''}">← Previous</a>
                        <a href="{next_page_link}" class="btn btn-secondary {'btn-disabled' if page >= total_pages else ''}">Next →</a>
                    </div>
                </div>
            </div>

            <!-- Audit Events Table -->
            <div class="card">
                <h3 style="margin: 0 0 1rem 0; font-size: 1.1rem; color: var(--primary);">📋 Recent Sync Audit History</h3>
                <table>
                    <thead>
                        <tr>
                            <th>Timestamp</th>
                            <th>Direction</th>
                            <th>Book / Hash</th>
                            <th>Percentage</th>
                            <th>Result & Details</th>
                        </tr>
                    </thead>
                    <tbody>
                        {events_html if events_html else "<tr><td colspan='5' style='color:#94a3b8; text-align:center; padding: 1.5rem;'>No sync events recorded yet.</td></tr>"}
                    </tbody>
                </table>
                <div class="pagination">
                    <div>Event Page {event_page} of {event_total_pages}</div>
                    <div style="display: flex; gap: 0.5rem;">
                        <a href="{prev_event_link}" class="btn btn-secondary {'btn-disabled' if event_page <= 1 else ''}">← Previous</a>
                        <a href="{next_event_link}" class="btn btn-secondary {'btn-disabled' if event_page >= event_total_pages else ''}">Next →</a>
                    </div>
                </div>
            </div>
        </div>

        <!-- ============================================================= -->
        <!-- TAB 2: KAVITA VFS -->
        <!-- ============================================================= -->
        <div id="tab-vfs" class="tab-content {'active' if active_tab == 'vfs' else ''}">
            <!-- VFS Actions Bar -->
            <div class="card" style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 1rem; border-left: 4px solid var(--primary);">
                <div>
                    <h3 style="margin: 0 0 0.25rem 0; color: var(--primary); font-size: 1.1rem;">🗂️ Kavita Virtual File System</h3>
                    <p style="margin: 0; color: var(--text-muted); font-size: 0.85rem;">
                        Generates <code style="color:#cbd5e1;">language/type/series/series Vol. volume Ch. chapter &#123;id&#125;.ext</code> for Kavita.
                        Mode: <strong style="color:var(--primary);">{config.vfs.mode.upper()}</strong> | Target: <code>{config.vfs.vfs_dir}</code>
                    </p>
                </div>
                <div style="display: flex; gap: 0.5rem; align-items: center; flex-wrap: wrap;">
                    <button class="btn btn-secondary" onclick="triggerVFSCleanup(this)" title="Removes unregistered files and broken links, pruning empty directories">🧹 Cleanup VFS</button>
                    <button class="btn btn-success" onclick="triggerVFSSync(this)" title="Scans Calibre DB and updates VFS links immediately">🔄 Sync VFS Now</button>
                </div>
            </div>

            <!-- VFS Collisions Alert Panel -->
            <div id="vfs-collision-panel" class="alert-panel" style="display: {'block' if vfs_summary.get('collision_count', 0) > 0 else 'none'};">
                <h3>⚠️ Path Collisions Detected ({vfs_summary.get('collision_count', 0)})</h3>
                <p style="font-size:0.85rem; margin-bottom: 0.5rem;">The following books mapped to identical filenames and were automatically disambiguated with unique suffixes:</p>
                <div style="max-height: 200px; overflow-y: auto;">
                    <table>
                        <thead>
                            <tr>
                                <th>Colliding Path</th>
                                <th>Existing Book</th>
                                <th>Colliding Book</th>
                                <th>Disambiguated VFS Path</th>
                            </tr>
                        </thead>
                        <tbody>
                            {"".join([f"<tr><td>{c['relpath']}</td><td>#{c['existing_book_id']}</td><td>#{c['colliding_book_id']}</td><td style='color:var(--success);'>{c['resolved_path']}</td></tr>" for c in vfs_summary.get("collisions", [])])}
                        </tbody>
                    </table>
                </div>
            </div>

            <!-- VFS Statistics Grid -->
            <div class="grid">
                <div class="stat-card">
                    <div id="vfs-stat-books" class="stat-value">{vfs_summary.get('total_books', 0):,}</div>
                    <div class="stat-label">📚 VFS Books</div>
                </div>
                <div class="stat-card">
                    <div id="vfs-stat-series" class="stat-value">{vfs_summary.get('total_series', 0):,}</div>
                    <div class="stat-label">📖 Series Folders</div>
                </div>
                <div class="stat-card">
                    <div id="vfs-stat-vols" class="stat-value">{vfs_summary.get('total_volumes', 0):,}</div>
                    <div class="stat-label">📦 Aggregated Volumes</div>
                </div>
                <div class="stat-card">
                    <div id="vfs-stat-chaps" class="stat-value">{vfs_summary.get('total_chapters', 0):,}</div>
                    <div class="stat-label">📑 Aggregated Chapters</div>
                </div>
                <div class="stat-card">
                    <div id="vfs-stat-collisions" class="stat-value" style="color: {'var(--danger)' if vfs_summary.get('collision_count', 0) > 0 else 'var(--success)'};">{vfs_summary.get('collision_count', 0)}</div>
                    <div class="stat-label">⚠️ Collisions</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value" style="color: var(--primary); font-size: 1.6rem; text-transform: uppercase;">{vfs_summary.get('mode', 'hardlink')}</div>
                    <div class="stat-label">⚙️ Link Mode</div>
                </div>
            </div>

            <!-- VFS Distribution Chips -->
            <div class="card" style="padding: 1rem 1.25rem;">
                <div style="display: flex; flex-wrap: wrap; gap: 1rem; align-items: center;">
                    <div style="display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap;">
                        <span style="font-size:0.85rem; font-weight:600; color:var(--text-muted);">Types:</span>
                        {"".join([f'<span class="badge badge-info">{k}: <strong>{v}</strong></span>' for k, v in vfs_summary.get("type_counts", {}).items()]) or '<span style="color:var(--text-muted); font-size:0.85rem;">None</span>'}
                    </div>
                    <div style="display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap; margin-left: auto;">
                        <span style="font-size:0.85rem; font-weight:600; color:var(--text-muted);">Languages:</span>
                        {"".join([f'<span class="badge badge-info">{k}: <strong>{v}</strong></span>' for k, v in vfs_summary.get("language_counts", {}).items()]) or '<span style="color:var(--text-muted); font-size:0.85rem;">None</span>'}
                    </div>
                </div>
            </div>

            <!-- Searchable Mapped Books Table -->
            <div class="card">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 1rem; flex-wrap: wrap; gap: 0.5rem;">
                    <div>
                        <h2 style="margin: 0; font-size: 1.25rem;">Mapped Books in VFS</h2>
                        <span id="vfs-table-count" style="font-size: 0.85rem; color: var(--text-muted);">Loading VFS catalog...</span>
                    </div>
                    <div>
                        <input type="text" id="vfs-search-input" class="search-input" placeholder="Search title, series, ID, path..." oninput="onVFSSearchInput()">
                    </div>
                </div>
                <table>
                    <thead>
                        <tr>
                            <th style="width: 70px;">ID</th>
                            <th>Series / Title</th>
                            <th style="width: 100px;">Vol</th>
                            <th style="width: 100px;">Ch</th>
                            <th style="width: 90px;">Type</th>
                            <th style="width: 80px;">Lang</th>
                            <th>Target VFS Path</th>
                            <th>Source File</th>
                        </tr>
                    </thead>
                    <tbody id="vfs-books-rows">
                        <tr><td colspan="8" style="text-align: center; color: var(--text-muted); padding: 2rem;">Loading books...</td></tr>
                    </tbody>
                </table>
                <div class="pagination">
                    <div style="display: flex; align-items: center; gap: 8px;">
                        <span>Page Size:</span>
                        <select id="vfs-page-size" onchange="changeVFSPageSize()" style="background:#0f172a; color:#fff; border:1px solid var(--card-border); padding:2px 6px; border-radius:4px;">
                            <option value="50" selected>50</option>
                            <option value="100">100</option>
                            <option value="250">250</option>
                        </select>
                    </div>
                    <div style="display: flex; gap: 0.5rem; align-items: center;">
                        <button id="vfs-btn-prev" class="btn btn-secondary" onclick="goToVFSPage(vfsCurrentPage - 1)">← Prev</button>
                        <span id="vfs-page-indicator" style="font-size: 0.85rem; color: var(--text-muted);">Page 1 of 1</span>
                        <button id="vfs-btn-next" class="btn btn-secondary" onclick="goToVFSPage(vfsCurrentPage + 1)">Next →</button>
                    </div>
                </div>
            </div>
        </div>

        <!-- ============================================================= -->
        <!-- TAB 3: SETTINGS -->
        <!-- ============================================================= -->
        <div id="tab-settings" class="tab-content {'active' if active_tab == 'settings' else ''}">
            <div id="config-alert" class="alert-panel" style="display: none;"></div>

            <div class="card">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 1.5rem; border-bottom: 1px solid var(--card-border); padding-bottom: 1rem; flex-wrap: wrap; gap: 1rem;">
                    <div>
                        <h2 style="margin: 0; font-size: 1.25rem; color: var(--primary);">⚙️ System Settings & Configuration</h2>
                        <span style="font-size: 0.85rem; color: var(--text-muted);">Editing configuration file: <code>{active_config_file}</code></span>
                    </div>
                    <button id="btn-save-config" class="btn btn-success" style="font-size: 1rem; padding: 0.6rem 1.5rem;" onclick="saveConfig()">💾 Save Configuration</button>
                </div>

                <form id="settings-form" onsubmit="event.preventDefault(); saveConfig();">
                    <!-- General / Server -->
                    <div class="form-section-title">🌐 Server Settings</div>
                    <div class="form-grid">
                        <div class="form-group">
                            <label>Host / Bind Address:</label>
                            <input type="text" id="cfg_server_host" value="{config.server.host}">
                        </div>
                        <div class="form-group">
                            <label>Port:</label>
                            <input type="number" id="cfg_server_port" value="{config.server.port}">
                        </div>
                        <div class="form-group">
                            <label>Auth Username (Optional):</label>
                            <input type="text" id="cfg_server_user" value="{config.server.auth_username or ''}" placeholder="Leave empty for open single-user">
                        </div>
                        <div class="form-group">
                            <label>Auth Password (Optional):</label>
                            <input type="password" id="cfg_server_pwd" value="{('******' if config.server.auth_password else '')}" placeholder="Leave empty for open single-user">
                        </div>
                    </div>

                    <!-- Calibre Settings -->
                    <div class="form-section-title">📚 Calibre Library Settings</div>
                    <div class="form-grid">
                        <div class="form-group" style="grid-column: 1 / -1;">
                            <label>Calibre Library Path (contains metadata.db):</label>
                            <input type="text" id="cfg_calibre_path" value="{config.calibre.library_path}">
                        </div>
                        <div class="form-group">
                            <label>Read Percentage Column:</label>
                            <input type="text" id="cfg_calibre_pct" value="{config.calibre.read_pct_column}">
                        </div>
                        <div class="form-group">
                            <label>Read Status (Boolean) Column:</label>
                            <input type="text" id="cfg_calibre_status" value="{config.calibre.read_status_column}">
                        </div>
                        <div class="form-group">
                            <label>Last Read Column:</label>
                            <input type="text" id="cfg_calibre_last_read" value="{config.calibre.last_read_column}">
                        </div>
                        <div class="form-group">
                            <label>KOReader Progress String Column:</label>
                            <input type="text" id="cfg_calibre_progress" value="{config.calibre.progress_column}">
                        </div>
                        <div class="form-group">
                            <label>Mark Read Threshold (0.0 to 1.0):</label>
                            <input type="number" step="0.01" id="cfg_calibre_threshold" value="{config.calibre.mark_read_threshold}">
                        </div>
                        <div class="form-group">
                            <label>Options:</label>
                            <div class="form-check">
                                <input type="checkbox" id="cfg_calibre_enabled" {'checked' if config.calibre.enabled else ''}>
                                <span>Enable Calibre connection</span>
                            </div>
                            <div class="form-check">
                                <input type="checkbox" id="cfg_calibre_auto_create" {'checked' if config.calibre.auto_create_columns else ''}>
                                <span>Auto-create custom columns in metadata.db</span>
                            </div>
                        </div>
                    </div>

                    <!-- Kavita Settings -->
                    <div class="form-section-title">☁️ Kavita Connection Settings</div>
                    <div class="form-grid">
                        <div class="form-group" style="grid-column: 1 / -1;">
                            <label>Kavita Base URL:</label>
                            <input type="text" id="cfg_kavita_url" value="{config.kavita.base_url}" placeholder="http://kavita:5000">
                        </div>
                        <div class="form-group">
                            <label>Kavita User API Key:</label>
                            <input type="text" id="cfg_kavita_key" value="{config.kavita.api_key}" placeholder="API Key from Kavita user settings">
                        </div>
                        <div class="form-group">
                            <label>HTTP Timeout (seconds):</label>
                            <input type="number" step="1" id="cfg_kavita_timeout" value="{config.kavita.timeout}">
                        </div>
                        <div class="form-group">
                            <label>Status:</label>
                            <div class="form-check">
                                <input type="checkbox" id="cfg_kavita_enabled" {'checked' if config.kavita.enabled else ''}>
                                <span>Enable Kavita connection</span>
                            </div>
                        </div>
                    </div>

                    <!-- KOReader Sync Engine -->
                    <div class="form-section-title">📱 KOReader Reading Progress Sync (Independent)</div>
                    <div class="form-grid">
                        <div class="form-group">
                            <label>Enable Reading Sync:</label>
                            <div class="form-check">
                                <input type="checkbox" id="cfg_sync_enabled" {'checked' if config.sync.enabled else ''}>
                                <span>Enable KOReader progress sync loop</span>
                            </div>
                        </div>
                        <div class="form-group">
                            <label>Sync Interval (seconds):</label>
                            <input type="number" id="cfg_sync_interval" value="{config.sync.interval_seconds}">
                        </div>
                        <div class="form-group">
                            <label>Conflict Resolution:</label>
                            <select id="cfg_sync_conflict">
                                <option value="latest_timestamp" {'selected' if config.sync.conflict_resolution == 'latest_timestamp' else ''}>latest_timestamp (Recommended)</option>
                                <option value="highest_percentage" {'selected' if config.sync.conflict_resolution == 'highest_percentage' else ''}>highest_percentage</option>
                            </select>
                        </div>
                        <div class="form-group">
                            <label>Scan on Startup:</label>
                            <div class="form-check">
                                <input type="checkbox" id="cfg_sync_scan" {'checked' if config.sync.scan_on_startup else ''}>
                                <span>Scan & index library hashes on startup</span>
                            </div>
                        </div>
                    </div>

                    <!-- Kavita VFS Engine -->
                    <div class="form-section-title">🗂️ Kavita VFS Virtual File System (Independent)</div>
                    <div class="form-grid">
                        <div class="form-group">
                            <label>Enable VFS Generator:</label>
                            <div class="form-check">
                                <input type="checkbox" id="cfg_vfs_enabled" {'checked' if config.vfs.enabled else ''}>
                                <span>Enable Kavita VFS library generation</span>
                            </div>
                        </div>
                        <div class="form-group">
                            <label>Operating Mode:</label>
                            <select id="cfg_vfs_mode">
                                <option value="hardlink" {'selected' if config.vfs.mode == 'hardlink' else ''}>hardlink (Recommended Default)</option>
                                <option value="symlink" {'selected' if config.vfs.mode == 'symlink' else ''}>symlink</option>
                                <option value="fuse" {'selected' if config.vfs.mode == 'fuse' else ''}>fuse (Read-only FUSE)</option>
                            </select>
                        </div>
                        <div class="form-group" style="grid-column: 1 / -1;">
                            <label>VFS Target Directory (Kavita library folder):</label>
                            <input type="text" id="cfg_vfs_dir" value="{config.vfs.vfs_dir}">
                        </div>
                        <div class="form-group">
                            <label>VFS Check Interval (seconds):</label>
                            <input type="number" id="cfg_vfs_interval" value="{config.vfs.interval_seconds}">
                        </div>
                        <div class="form-group">
                            <label>Default Language:</label>
                            <input type="text" id="cfg_vfs_lang" value="{config.vfs.default_language}">
                        </div>
                        <div class="form-group">
                            <label>Default Type:</label>
                            <input type="text" id="cfg_vfs_type" value="{config.vfs.default_type}">
                        </div>
                        <div class="form-group">
                            <label>Custom Target Calibre Dir Prefix (Symlinks only):</label>
                            <input type="text" id="cfg_vfs_target_dir" value="{config.vfs.calibre_target_dir}" placeholder="/mnt/user/... or Kavita path">
                        </div>
                        <div class="form-group">
                            <label>Relative Symlinks:</label>
                            <div class="form-check">
                                <input type="checkbox" id="cfg_vfs_relative" {'checked' if config.vfs.relative_links else ''}>
                                <span>Use relative paths for symlinks</span>
                            </div>
                        </div>
                        <div class="form-group">
                            <label>Custom Column: Type:</label>
                            <input type="text" id="cfg_vfs_col_type" value="{config.vfs.custom_type_column}">
                        </div>
                        <div class="form-group">
                            <label>Custom Column: Volume:</label>
                            <input type="text" id="cfg_vfs_col_volume" value="{config.vfs.custom_volume_column}">
                        </div>
                        <div class="form-group">
                            <label>Custom Column: Chapter:</label>
                            <input type="text" id="cfg_vfs_col_chapter" value="{config.vfs.custom_chapter_column}">
                        </div>
                    </div>

                    <!-- Storage -->
                    <div class="form-section-title">💾 Storage & Data Directory</div>
                    <div class="form-grid">
                        <div class="form-group" style="grid-column: 1 / -1;">
                            <label>Data Directory (persistent state & internal databases):</label>
                            <input type="text" id="cfg_data_dir" value="{config.data_dir}">
                        </div>
                    </div>

                    <div style="margin-top: 1.5rem; text-align: right;">
                        <button type="submit" class="btn btn-success" style="font-size: 1rem; padding: 0.6rem 1.5rem;">💾 Save Configuration</button>
                    </div>
                </form>
            </div>
        </div>
    </div>

    <!-- Scripts -->
    <script>
        function switchTab(tabName) {{
            document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
            document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
            
            const btn = document.getElementById('tab-btn-' + tabName);
            const content = document.getElementById('tab-' + tabName);
            if (btn && content) {{
                btn.classList.add('active');
                content.classList.add('active');
                window.location.hash = '#' + tabName;
            }}
            if (tabName === 'vfs') {{
                loadVFSBooks();
            }}
        }}

        // Listen to hash on load
        window.addEventListener('DOMContentLoaded', () => {{
            const hash = window.location.hash.replace('#', '');
            if (hash && ['sync', 'vfs', 'settings'].includes(hash)) {{
                switchTab(hash);
            }}
        }});

        function linkDocument(doc) {{
            const id = prompt('Enter Calibre Book ID to link with hash ' + doc + ':');
            if (id) {{
                fetch('/api/link-document', {{
                    method: 'POST',
                    headers: {{'Content-Type': 'application/json'}},
                    body: JSON.stringify({{document: doc, calibre_id: parseInt(id)}})
                }}).then(r => r.json()).then(d => {{
                    if (d.status === 'ok') {{
                        alert('Linked to Calibre Book #' + d.calibre_id + ' (' + (d.title || '') + ')!');
                        location.reload();
                    }} else {{
                        alert('Error linking document: ' + JSON.stringify(d));
                    }}
                }}).catch(e => alert('Error: ' + e));
            }}
        }}

        function triggerSyncNow(btn) {{
            btn.disabled = true;
            btn.innerText = 'Syncing...';
            fetch('/api/sync-now', {{method: 'POST'}})
                .then(() => location.reload())
                .catch(e => {{ alert('Sync error: ' + e); btn.disabled = false; btn.innerText = '🔄 Sync Now'; }});
        }}

        function triggerBackfill(btn) {{
            btn.disabled = true;
            btn.innerText = 'Backfilling...';
            fetch('/api/backfill', {{method: 'POST'}})
                .then(r => r.json())
                .then(d => {{ alert('Backfilled ' + d.backfilled_count + ' books from Calibre!'); location.reload(); }})
                .catch(e => {{ alert('Error: ' + e); btn.disabled = false; btn.innerText = '📥 Backfill from Calibre'; }});
        }}

        function triggerVFSSync(btn) {{
            btn.disabled = true;
            btn.innerText = 'Syncing VFS...';
            fetch('/api/vfs/sync', {{method: 'POST'}})
                .then(r => r.json())
                .then(d => {{
                    if (d.status === 'success') {{
                        alert('VFS Sync Complete! Created: ' + d.created + ', Updated: ' + d.updated + ', Deleted: ' + d.deleted);
                        location.reload();
                    }} else {{
                        alert('VFS Sync error: ' + (d.message || JSON.stringify(d)));
                        btn.disabled = false;
                        btn.innerText = '🔄 Sync VFS Now';
                    }}
                }})
                .catch(e => {{ alert('Error: ' + e); btn.disabled = false; btn.innerText = '🔄 Sync VFS Now'; }});
        }}

        function triggerVFSCleanup(btn) {{
            if (!confirm('Clean up unregistered files and empty directories in VFS?')) return;
            btn.disabled = true;
            btn.innerText = 'Cleaning...';
            fetch('/api/vfs/cleanup', {{method: 'POST'}})
                .then(r => r.json())
                .then(d => {{
                    if (d.status === 'success') {{
                        alert('VFS Cleanup Complete! Removed ' + d.result.removed_count + ' stale files and ' + d.result.empty_dirs_removed + ' empty dirs.');
                        location.reload();
                    }} else {{
                        alert('VFS Cleanup error: ' + (d.message || JSON.stringify(d)));
                        btn.disabled = false;
                        btn.innerText = '🧹 Cleanup VFS';
                    }}
                }})
                .catch(e => {{ alert('Error: ' + e); btn.disabled = false; btn.innerText = '🧹 Cleanup VFS'; }});
        }}

        // VFS Books Table State & Pagination
        let vfsCurrentPage = 1;
        let vfsPageSize = 50;
        let vfsTotalPages = 1;
        let vfsSearchTimer = null;

        function onVFSSearchInput() {{
            clearTimeout(vfsSearchTimer);
            vfsSearchTimer = setTimeout(() => {{
                vfsCurrentPage = 1;
                loadVFSBooks();
            }}, 300);
        }}

        function changeVFSPageSize() {{
            vfsPageSize = parseInt(document.getElementById('vfs-page-size').value) || 50;
            vfsCurrentPage = 1;
            loadVFSBooks();
        }}

        function goToVFSPage(page) {{
            if (page < 1 || page > vfsTotalPages) return;
            vfsCurrentPage = page;
            loadVFSBooks();
        }}

        async function loadVFSBooks() {{
            const q = document.getElementById('vfs-search-input') ? document.getElementById('vfs-search-input').value.trim() : '';
            const offset = (vfsCurrentPage - 1) * vfsPageSize;
            try {{
                const res = await fetch(`/api/vfs/books?q=${{encodeURIComponent(q)}}&limit=${{vfsPageSize}}&offset=${{offset}}`);
                const data = await res.json();
                const total = data.total || 0;
                vfsTotalPages = Math.max(1, Math.ceil(total / vfsPageSize));
                
                document.getElementById('vfs-table-count').innerText = `${{total.toLocaleString()}} books mapped in VFS`;
                document.getElementById('vfs-page-indicator').innerText = `Page ${{vfsCurrentPage}} of ${{vfsTotalPages}}`;
                document.getElementById('vfs-btn-prev').disabled = (vfsCurrentPage <= 1);
                document.getElementById('vfs-btn-next').disabled = (vfsCurrentPage >= vfsTotalPages);

                const rowsHtml = (data.items || []).map(b => `
                    <tr>
                        <td><span class="badge badge-primary">#${{b.book_id}}</span></td>
                        <td><strong>${{b.series || b.title}}</strong><br><small style="color:#94a3b8;">${{b.title != b.series ? b.title : ''}}</small></td>
                        <td>${{b.volume ? 'Vol. ' + b.volume : '-'}}</td>
                        <td>${{b.chapter ? 'Ch. ' + b.chapter : '-'}}</td>
                        <td><span class="badge badge-info">${{b.type || 'Unknown'}}</span></td>
                        <td><span class="badge badge-info">${{b.language || 'unknown'}}</span></td>
                        <td><code style="font-size:0.75rem;" title="${{b.vfs_relpath || b.vfs_path}}">${{(b.vfs_relpath || b.vfs_path).split('/').pop()}}</code></td>
                        <td><code style="font-size:0.75rem;" title="${{b.source_path}}">${{b.source_path.split('/').pop()}}</code></td>
                    </tr>
                `).join('');

                document.getElementById('vfs-books-rows').innerHTML = rowsHtml || '<tr><td colspan="8" style="text-align:center; color:#94a3b8; padding:2rem;">No books found matching search criteria.</td></tr>';
            }} catch (e) {{
                document.getElementById('vfs-books-rows').innerHTML = `<tr><td colspan="8" style="text-align:center; color:var(--danger); padding:2rem;">Error loading VFS books: ${{e}}</td></tr>`;
            }}
        }}

        async function saveConfig() {{
            const saveBtn = document.getElementById('btn-save-config');
            const alertBox = document.getElementById('config-alert');
            saveBtn.disabled = true;
            saveBtn.innerText = 'Saving...';
            alertBox.style.display = 'none';

            const payload = {{
                server: {{
                    host: document.getElementById('cfg_server_host').value.trim(),
                    port: parseInt(document.getElementById('cfg_server_port').value) || 8080,
                    auth_username: document.getElementById('cfg_server_user').value.trim() || null,
                    auth_password: document.getElementById('cfg_server_pwd').value || null,
                }},
                calibre: {{
                    enabled: document.getElementById('cfg_calibre_enabled').checked,
                    library_path: document.getElementById('cfg_calibre_path').value.trim(),
                    read_pct_column: document.getElementById('cfg_calibre_pct').value.trim(),
                    read_status_column: document.getElementById('cfg_calibre_status').value.trim(),
                    last_read_column: document.getElementById('cfg_calibre_last_read').value.trim(),
                    progress_column: document.getElementById('cfg_calibre_progress').value.trim(),
                    auto_create_columns: document.getElementById('cfg_calibre_auto_create').checked,
                    mark_read_threshold: parseFloat(document.getElementById('cfg_calibre_threshold').value) || 0.98,
                }},
                kavita: {{
                    enabled: document.getElementById('cfg_kavita_enabled').checked,
                    base_url: document.getElementById('cfg_kavita_url').value.trim(),
                    api_key: document.getElementById('cfg_kavita_key').value.trim(),
                    timeout: parseFloat(document.getElementById('cfg_kavita_timeout').value) || 10.0,
                }},
                sync: {{
                    enabled: document.getElementById('cfg_sync_enabled').checked,
                    interval_seconds: parseInt(document.getElementById('cfg_sync_interval').value) || 300,
                    conflict_resolution: document.getElementById('cfg_sync_conflict').value,
                    scan_on_startup: document.getElementById('cfg_sync_scan').checked,
                }},
                vfs: {{
                    enabled: document.getElementById('cfg_vfs_enabled').checked,
                    vfs_dir: document.getElementById('cfg_vfs_dir').value.trim(),
                    mode: document.getElementById('cfg_vfs_mode').value,
                    interval_seconds: parseInt(document.getElementById('cfg_vfs_interval').value) || 60,
                    relative_links: document.getElementById('cfg_vfs_relative').checked,
                    default_language: document.getElementById('cfg_vfs_lang').value.trim() || 'unknown',
                    default_type: document.getElementById('cfg_vfs_type').value.trim() || 'Unknown',
                    calibre_target_dir: document.getElementById('cfg_vfs_target_dir').value.trim(),
                    custom_type_column: document.getElementById('cfg_vfs_col_type').value.trim() || 'type',
                    custom_volume_column: document.getElementById('cfg_vfs_col_volume').value.trim() || 'volume',
                    custom_chapter_column: document.getElementById('cfg_vfs_col_chapter').value.trim() || 'chapter',
                }},
                data_dir: document.getElementById('cfg_data_dir').value.trim() || '/app/data',
            }};

            try {{
                const res = await fetch('/api/config', {{
                    method: 'POST',
                    headers: {{'Content-Type': 'application/json'}},
                    body: JSON.stringify(payload),
                }});
                const data = await res.json();
                if (res.ok && data.status === 'success') {{
                    alertBox.className = 'alert-panel';
                    alertBox.style.background = 'rgba(34, 197, 94, 0.15)';
                    alertBox.style.border = '1px solid #22c55e';
                    alertBox.style.color = '#34d399';
                    alertBox.innerHTML = `<strong>✔ Configuration Saved!</strong> Saved to <code>${{data.saved_to}}</code>. Settings have been applied.`;
                    alertBox.style.display = 'block';
                    alertBox.scrollIntoView({{behavior: 'smooth'}});
                }} else {{
                    throw new Error(data.detail || data.message || 'Failed to save configuration');
                }}
            }} catch (err) {{
                alertBox.className = 'alert-panel';
                alertBox.style.background = 'rgba(239, 68, 68, 0.15)';
                alertBox.style.border = '1px solid #ef4444';
                alertBox.style.color = '#f87171';
                alertBox.innerHTML = `<strong>❌ Error Saving Configuration:</strong> ${{err.message}}`;
                alertBox.style.display = 'block';
                alertBox.scrollIntoView({{behavior: 'smooth'}});
            }} finally {{
                saveBtn.disabled = false;
                saveBtn.innerText = '💾 Save Configuration';
            }}
        }}
    </script>
</body>
</html>"""
        return HTMLResponse(content=html)

    return app
