"""FastAPI server implementing KOReader sync protocol and hub proxy."""

import asyncio
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse

from .config import AppConfig
from .db import InternalDatabase
from .hasher import compute_filename_md5
from .models import HubStatus, ProgressPayload, ProgressRecord, ProgressResponse, SyncEvent, UserAuthRequest
from .synchronizer import Synchronizer

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
) -> FastAPI:
    """Factory creating configured FastAPI app for kosync-hub."""
    start_time = time.time()

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
                getattr(synchronizer.calibre, "find_book_by_filename_hash", None),
            )

        # Start background sync worker
        asyncio.create_task(synchronizer.start_background_loop())
        try:
            yield
        finally:
            synchronizer.stop()

    app = FastAPI(
        title="KOReader Sync Hub",
        description="Dual-sync proxy & bridge for Kavita and Calibre",
        version="1.0.0",
        lifespan=lifespan,
    )

    # -------------------------------------------------------------------------
    # KOReader Sync Protocol Endpoints (Single-User open auth)
    # -------------------------------------------------------------------------

    async def _handle_auth(user: Optional[str] = None):
        """Single-user KOReader authorization check (accepts any credentials)."""
        logger.info(f"KOReader auth check received from user='{user or 'anonymous'}'")
        return {"message": "Authorized", "authorized": "OK"}

    @app.get("/users/auth", status_code=status.HTTP_200_OK)
    @app.get("/users/auth/", status_code=status.HTTP_200_OK)
    @app.post("/users/auth", status_code=status.HTTP_200_OK)
    @app.post("/users/auth/", status_code=status.HTTP_200_OK)
    @app.get("/koreader/users/auth", status_code=status.HTTP_200_OK)
    @app.get("/koreader/users/auth/", status_code=status.HTTP_200_OK)
    @app.post("/koreader/users/auth", status_code=status.HTTP_200_OK)
    @app.post("/koreader/users/auth/", status_code=status.HTTP_200_OK)
    @app.get("/sync/users/auth", status_code=status.HTTP_200_OK)
    @app.get("/sync/users/auth/", status_code=status.HTTP_200_OK)
    @app.post("/sync/users/auth", status_code=status.HTTP_200_OK)
    @app.post("/sync/users/auth/", status_code=status.HTTP_200_OK)
    async def users_auth(
        x_auth_user: Optional[str] = Header(None),
        x_auth_key: Optional[str] = Header(None),
    ):
        return await _handle_auth(x_auth_user)

    @app.post("/users/create", status_code=status.HTTP_201_CREATED)
    @app.post("/users/create/", status_code=status.HTTP_201_CREATED)
    @app.post("/koreader/users/create", status_code=status.HTTP_201_CREATED)
    @app.post("/koreader/users/create/", status_code=status.HTTP_201_CREATED)
    @app.post("/sync/users/create", status_code=status.HTTP_201_CREATED)
    @app.post("/sync/users/create/", status_code=status.HTTP_201_CREATED)
    async def users_create(payload: UserAuthRequest):
        """KOReader user registration endpoint."""
        logger.info(f"User registration request received for '{payload.username}'")
        return {"message": "User registered successfully"}

    async def _handle_update_progress(payload: ProgressPayload):
        """
        Receives progress update from KOReader or CrossPoint device.
        Extracts Calibre ID from filename {id} or existing alias, then
        fans out the update to both Kavita and Calibre.
        """
        now_ts = int(datetime.utcnow().timestamp())
        meta = payload.metadata
        filename = meta.filename if meta else None
        title = meta.title if meta else None
        authors = meta.authors if meta else None

        logger.info(
            f"Incoming sync update: document={payload.document}, device={payload.device or 'unknown'}, "
            f"progress={payload.progress}, pct={round(payload.percentage * 100, 1)}%"
        )

        # 1. Check for ID in filename ({id}, (id), [id], etc.)
        calibre_id = None
        if filename:
            match = re.search(r"\{(\d+)\}", filename)
            if match:
                calibre_id = int(match.group(1))
            else:
                cand = extract_calibre_id_from_filename(filename)
                if cand and synchronizer.calibre and hasattr(synchronizer.calibre, "get_book_by_id"):
                    if synchronizer.calibre.get_book_by_id(cand):
                        calibre_id = cand

        # 2. Check if document is an already known alias in internal DB
        if not calibre_id:
            calibre_id = db.get_calibre_id_for_document(payload.document)

        # 3. Check if document is a known KOReader / MD5 hash in Calibre DB
        if not calibre_id and synchronizer.calibre and hasattr(synchronizer.calibre, "find_book_by_hash"):
            calibre_id = synchronizer.calibre.find_book_by_hash(payload.document)

        # 4. Check if document matches MD5 of any Calibre book candidate filename
        if not calibre_id and synchronizer.calibre and hasattr(synchronizer.calibre, "find_book_by_filename_hash"):
            calibre_id = synchronizer.calibre.find_book_by_filename_hash(payload.document)

        # 5. Check if filename itself matches filename hash in Calibre DB
        if not calibre_id and filename and synchronizer.calibre and hasattr(synchronizer.calibre, "find_book_by_filename_hash"):
            calibre_id = synchronizer.calibre.find_book_by_filename_hash(compute_filename_md5(filename))

        # 6. Fallback to title/author/filename matching in Calibre DB
        if not calibre_id and synchronizer.calibre and hasattr(synchronizer.calibre, "match_book") and (filename or title):
            calibre_id = synchronizer.calibre.match_book(
                document_hash=payload.document,
                filename=filename,
                title=title,
                authors=authors,
            )

        # If calibre_id found, link the document hash as an alias and resolve canonical details
        canonical_hash = None
        if calibre_id:
            db.link_document_alias(payload.document, calibre_id, payload.device)
            if synchronizer.calibre and hasattr(synchronizer.calibre, "get_book_by_id"):
                cal_b = synchronizer.calibre.get_book_by_id(calibre_id)
                if cal_b:
                    title = title or cal_b.title
                    authors = authors or cal_b.authors
                    if not filename and cal_b.file_path:
                        filename = Path(cal_b.file_path).name
                    if cal_b.koreader_hash:
                        canonical_hash = cal_b.koreader_hash

            if not canonical_hash and db and hasattr(db, "get_mapping_by_calibre_id"):
                m = db.get_mapping_by_calibre_id(calibre_id)
                if m and m.get("koreader_hash"):
                    canonical_hash = m["koreader_hash"]

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

        # Save to local database
        db.upsert_progress(record, calibre_book_id=calibre_id)
        if calibre_id:
            db.update_sync_state(
                calibre_id=calibre_id,
                percentage=record.percentage,
                progress=record.progress,
                source=record.device or "CrossPoint",
                synced_at=now_ts,
            )

        # 1. Update Calibre immediately (fast local SQLite operation, < 2ms)
        if synchronizer.calibre:
            try:
                await synchronizer.calibre.update_progress(record)
            except Exception as e:
                logger.error(f"Error updating Calibre DB for {payload.document}: {e}")

        # 2. Fanout to Kavita in background so reader gets an immediate response without timing out
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
        """
        Retrieves current reading progress for KOReader / CrossPoint.
        Checks local database, alias mapping, and upstreams.
        """
        logger.info(f"Incoming progress request for document={document}")
        # Trigger single-document sync to reconcile upstreams
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
            raise HTTPException(status_code=404, detail="No progress found for document")

        return ProgressResponse(
            document=record.document,
            progress=record.progress,
            percentage=record.percentage,
            timestamp=record.timestamp,
            device=record.device or "kosync-hub",
        )

    @app.get("/syncs/progress/{document}", response_model=ProgressResponse)
    @app.get("/syncs/progress/{document}/", response_model=ProgressResponse)
    @app.get("/koreader/syncs/progress/{document}", response_model=ProgressResponse)
    @app.get("/koreader/syncs/progress/{document}/", response_model=ProgressResponse)
    @app.get("/sync/syncs/progress/{document}", response_model=ProgressResponse)
    @app.get("/sync/syncs/progress/{document}/", response_model=ProgressResponse)
    @app.get("/sync/progress/{document}", response_model=ProgressResponse)
    @app.get("/sync/progress/{document}/", response_model=ProgressResponse)
    async def get_progress(document: str):
        return await _handle_get_progress(document)

    # -------------------------------------------------------------------------
    # Management & Status Endpoints
    # -------------------------------------------------------------------------

    @app.get("/healthcheck")
    async def healthcheck():
        return {
            "status": "healthy",
            "time": datetime.utcnow().isoformat(),
            "tracked_documents": db.count_tracked(),
        }

    @app.get("/api/status", response_model=HubStatus)
    async def api_status():
        kavita_ok = await synchronizer.kavita.test_connection() if synchronizer.kavita else False
        calibre_ok = await synchronizer.calibre.test_connection() if synchronizer.calibre else False

        return HubStatus(
            uptime_seconds=time.time() - start_time,
            tracked_books_count=db.count_tracked(),
            kavita_connected=kavita_ok,
            calibre_connected=calibre_ok,
            last_sync_timestamp=synchronizer.last_sync_timestamp,
            last_sync_status=synchronizer.last_sync_status,
        )

    @app.post("/api/sync-now")
    async def api_sync_now():
        res = await synchronizer.sync_all()
        if synchronizer.calibre and hasattr(synchronizer.calibre, "get_book_by_id"):
            db.repair_missing_titles(
                synchronizer.calibre.get_book_by_id,
                getattr(synchronizer.calibre, "find_book_by_filename_hash", None),
            )
        return {"status": "completed", "result": res}

    @app.post("/api/backfill")
    async def api_backfill():
        count = await synchronizer.backfill_calibre_books()
        if synchronizer.calibre and hasattr(synchronizer.calibre, "get_book_by_id"):
            db.repair_missing_titles(
                synchronizer.calibre.get_book_by_id,
                getattr(synchronizer.calibre, "find_book_by_filename_hash", None),
            )
        db.merge_duplicate_calibre_entries()
        asyncio.create_task(synchronizer.index_filename_hashes_task(force=False))
        return {"status": "completed", "backfilled_count": count}

    @app.post("/api/link-document")
    async def api_link_document(payload: dict):
        """Manually links a document hash to a Calibre ID and updates title/author from Calibre."""
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
    async def api_books(
        page: int = 1,
        page_size: int = 15,
        search: Optional[str] = None,
    ):
        rows, total = db.get_paginated_documents(page=page, page_size=page_size, search=search)
        return {
            "items": [dict(r) for r in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    @app.get("/api/events")
    async def api_events(
        page: int = 1,
        page_size: int = 15,
    ):
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

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(
        request: Request,
        page: int = 1,
        event_page: int = 1,
        search: Optional[str] = None,
    ):
        base_url = str(request.base_url).rstrip("/")
        stats = db.get_reading_stats()
        page_size = 12
        tracked_docs, total_docs = db.get_paginated_documents(page=page, page_size=page_size, search=search)
        recent_events, total_events = db.get_paginated_events(page=event_page, page_size=page_size)

        total_pages = max(1, (total_docs + page_size - 1) // page_size)
        event_total_pages = max(1, (total_events + page_size - 1) // page_size)

        rows_html_list = []
        for d in tracked_docs:
            d_dict = dict(d)
            title = d_dict.get("title") or "Unknown"
            authors = d_dict.get("authors") or ""
            cal_id = d_dict.get("calibre_book_id")
            doc = str(d_dict.get("document", ""))
            device = d_dict.get("device") or "KOReader"

            # Auto-resolve missing calibre_id on the fly if hash is known
            if not cal_id:
                cal_id = db.get_calibre_id_for_document(doc) or db.get_calibre_id_by_filename_hash(doc)
                if not cal_id and synchronizer.calibre and hasattr(synchronizer.calibre, "find_book_by_filename_hash"):
                    cal_id = synchronizer.calibre.find_book_by_filename_hash(doc)
                if cal_id:
                    db.link_document_alias(doc, cal_id, device)
                    with db._get_connection() as c:
                        c.execute("UPDATE tracked_documents SET calibre_book_id = ? WHERE document = ?", (cal_id, doc))
                        c.commit()

            if (not title or title == "Unknown") and cal_id and synchronizer.calibre and hasattr(synchronizer.calibre, "get_book_by_id"):
                cal_b = synchronizer.calibre.get_book_by_id(cal_id)
                if cal_b and cal_b.title:
                    title = cal_b.title
                    authors = authors or cal_b.authors
                    db.update_document_metadata(doc, title, authors)
            pct = float(d_dict.get("percentage", 0.0))
            ts = d_dict.get("timestamp", 0)
            time_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "-"
            status = d_dict.get("last_sync_status") or "synced"

            aliases = db.get_aliases_for_calibre_id(cal_id) if cal_id else []
            other_aliases = [a for a in aliases if a != doc]
            alias_badge = f""" <span class="badge badge-info" title="Alternative linked hashes:\n{chr(10).join(other_aliases)}" style="cursor:help; font-size:0.75rem;">+{len(other_aliases)}</span>""" if other_aliases else ""

            cal_badge = f"""<span class="badge badge-primary">#{cal_id}</span>""" if cal_id else f"""<button class="btn" style="padding:2px 8px; font-size:0.75rem; background:#f59e0b; color:#fff; border:none; border-radius:4px; cursor:pointer;" onclick="linkDocument('{doc}')" title="Click to manually link to Calibre ID">#- (Link)</button>"""

            rows_html_list.append(
                f"""<tr>
                    <td><strong>{title}</strong><br><small style="color:#94a3b8;">{authors}</small></td>
                    <td>{cal_badge}</td>
                    <td><code title="{doc}">{doc[:10]}...</code>{alias_badge}</td>
                    <td><span class="badge badge-info">{device}</span></td>
                    <td>
                        <div style="display:flex; align-items:center; gap:8px;">
                            <div class="progress-bar"><div class="fill" style="width: {min(100, round(pct * 100))}%"></div></div>
                            <span style="font-weight:600; font-size:0.85rem;">{round(pct * 100, 1)}%</span>
                        </div>
                    </td>
                    <td><small style="color:#94a3b8;">{time_str}</small></td>
                    <td><span class="badge badge-success">{status}</span></td>
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

        # Pagination controls
        search_query_part = f"&search={search}" if search else ""
        prev_page_link = f"/?page={max(1, page - 1)}{search_query_part}&event_page={event_page}"
        next_page_link = f"/?page={min(total_pages, page + 1)}{search_query_part}&event_page={event_page}"

        prev_event_link = f"/?page={page}{search_query_part}&event_page={max(1, event_page - 1)}"
        next_event_link = f"/?page={page}{search_query_part}&event_page={min(event_total_pages, event_page + 1)}"

        search_val = search or ""

        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>KOReader Kavita & Calibre Sync Hub</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; background: #0f172a; color: #f8fafc; margin: 0; padding: 2rem; }}
        .container {{ max-width: 1180px; margin: 0 auto; }}
        h1 {{ margin-top: 0; color: #38bdf8; font-size: 1.8rem; display: flex; align-items: center; gap: 0.5rem; }}
        .card {{ background: #1e293b; border-radius: 8px; padding: 1.5rem; margin-bottom: 1.5rem; border: 1px solid #334155; }}
        .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 1rem; margin-bottom: 1.5rem; }}
        .stat-card {{ background: #1e293b; border-radius: 8px; padding: 1.2rem; border: 1px solid #334155; }}
        .stat-value {{ font-size: 2rem; font-weight: bold; color: #38bdf8; margin-bottom: 0.25rem; }}
        .stat-label {{ font-size: 0.8rem; color: #94a3b8; text-transform: uppercase; letter-spacing: 0.05em; }}
        table {{ width: 100%; border-collapse: collapse; margin-top: 0.5rem; }}
        th, td {{ padding: 0.75rem; text-align: left; border-bottom: 1px solid #334155; }}
        th {{ color: #94a3b8; font-weight: 600; font-size: 0.85rem; text-transform: uppercase; }}
        .badge {{ padding: 0.2rem 0.5rem; border-radius: 4px; font-size: 0.75rem; font-weight: 600; }}
        .badge-success {{ background: #065f46; color: #34d399; }}
        .badge-primary {{ background: #1e40af; color: #93c5fd; }}
        .badge-info {{ background: #374151; color: #e2e8f0; }}
        .progress-bar {{ width: 90px; height: 8px; background: #334155; border-radius: 4px; display: inline-block; overflow: hidden; }}
        .fill {{ height: 100%; background: #38bdf8; border-radius: 4px; }}
        .btn {{ background: #0284c7; color: white; border: none; padding: 0.5rem 1rem; border-radius: 6px; cursor: pointer; font-weight: 600; text-decoration: none; display: inline-block; font-size: 0.9rem; }}
        .btn:hover {{ background: #0369a1; }}
        .btn-secondary {{ background: #334155; color: #f8fafc; padding: 0.4rem 0.8rem; }}
        .btn-secondary:hover {{ background: #475569; }}
        .btn-disabled {{ opacity: 0.4; pointer-events: none; }}
        .search-input {{ background: #0f172a; border: 1px solid #334155; color: #f8fafc; padding: 0.5rem 0.8rem; border-radius: 6px; width: 260px; font-size: 0.9rem; }}
        .search-input:focus {{ outline: none; border-color: #38bdf8; }}
        code {{ background: #0f172a; padding: 0.2rem 0.4rem; border-radius: 4px; color: #e2e8f0; font-size: 0.85rem; }}
        .pagination {{ display: flex; align-items: center; justify-content: space-between; margin-top: 1rem; padding-top: 0.8rem; border-top: 1px solid #334155; font-size: 0.9rem; color: #94a3b8; }}
    </style>
</head>
<body>
    <div class="container">
        <h1>📖 Kavita & Calibre Sync Hub</h1>
        <p style="color: #94a3b8; margin-bottom: 1.5rem;">CrossPoint / KOReader single-user sync with Calibre DB & Kavita WebUI integration.</p>
        
        <!-- KOReader / CrossPoint Connection Card -->
        <div class="card" style="border-left: 4px solid #38bdf8; background: #1e293b;">
            <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 1rem;">
                <div>
                    <h3 style="margin: 0 0 0.25rem 0; color: #38bdf8; font-size: 1.1rem;">📱 KOReader & CrossPoint Device Sync URL</h3>
                    <p style="margin: 0; color: #94a3b8; font-size: 0.85rem;">
                        Enter this server URL in your KOReader / CrossPoint Sync settings. Any username/password is accepted.
                    </p>
                </div>
                <div style="display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap;">
                    <div style="background: #0f172a; padding: 0.4rem 0.8rem; border-radius: 6px; border: 1px solid #334155; display: flex; align-items: center; gap: 0.5rem;">
                        <span style="color: #94a3b8; font-size: 0.8rem; text-transform: uppercase; font-weight: 600;">Server URL:</span>
                        <code id="sync-url" style="color: #38bdf8; font-weight: bold; font-size: 0.95rem;">{base_url}</code>
                    </div>
                    <button class="btn btn-secondary" onclick="navigator.clipboard.writeText(document.getElementById('sync-url').innerText); this.innerText='Copied!'; setTimeout(() => this.innerText='📋 Copy', 2000);">📋 Copy</button>
                </div>
            </div>
            <div style="margin-top: 0.75rem; font-size: 0.8rem; color: #64748b; border-top: 1px solid #334155; padding-top: 0.5rem;">
                Tip for Xteink X3 / CrossPoint: Export books from Calibre formatted with <code style="color:#cbd5e1;">&#123;id&#125;</code> (e.g. <code style="color:#cbd5e1;">Title - Author &#123;123&#125;.epub</code>) to sync automatically even when compressed or renamed.
            </div>
        </div>

        <!-- Reading History Statistics -->
        <div class="grid">
            <div class="stat-card">
                <div class="stat-value">{stats['total_tracked']}</div>
                <div class="stat-label">📚 Tracked Books</div>
            </div>
            <div class="stat-card">
                <div class="stat-value" style="color: #34d399;">{stats['completed_books']}</div>
                <div class="stat-label">🏆 Completed (≥98%)</div>
            </div>
            <div class="stat-card">
                <div class="stat-value" style="color: #fbbf24;">{stats['in_progress']}</div>
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
                <div class="stat-value" style="color: #38bdf8;">{stats['aliases_count']}</div>
                <div class="stat-label">📱 Connected Devices</div>
            </div>
        </div>

        <!-- Books Table with Search & Pagination -->
        <div class="card">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 1rem; flex-wrap: wrap; gap: 0.5rem;">
                <div>
                    <h2 style="margin: 0; font-size: 1.25rem;">Books & Reading Progress</h2>
                    <span style="font-size: 0.85rem; color: #94a3b8;">{total_docs} total books in database</span>
                </div>
                <div style="display: flex; gap: 0.5rem; align-items: center; flex-wrap: wrap;">
                    <form method="GET" action="/" style="display: flex; gap: 0.5rem;">
                        <input type="hidden" name="event_page" value="{event_page}">
                        <input type="text" name="search" class="search-input" placeholder="Search title, author, #id..." value="{search_val}">
                        <button class="btn btn-secondary" type="submit">Search</button>
                        {f'<a href="/?event_page={event_page}" class="btn btn-secondary">Clear</a>' if search else ''}
                    </form>
                    <form action="/api/backfill" method="POST" onsubmit="event.preventDefault(); const b=this.querySelector('button'); b.disabled=true; b.innerText='Backfilling...'; fetch('/api/backfill', {{method: 'POST'}}).then(r=>r.json()).then(d=>{{ alert('Backfilled ' + d.backfilled_count + ' books from Calibre!'); location.reload(); }}).catch(e=>{{ alert('Error: ' + e); b.disabled=false; b.innerText='📥 Backfill from Calibre'; }});">
                        <button class="btn btn-secondary" type="submit" title="Scan Calibre DB for all books with read percentage or status and import them into KOReader Hub">📥 Backfill from Calibre</button>
                    </form>
                    <form action="/api/sync-now" method="POST" onsubmit="event.preventDefault(); fetch('/api/sync-now', {{method: 'POST'}}).then(() => location.reload());">
                        <button class="btn" type="submit">🔄 Sync Now</button>
                    </form>
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

        <!-- Recent Sync Events with Pagination -->
        <div class="card">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.5rem;">
                <h2 style="margin: 0; font-size: 1.25rem;">Sync History & Audit Log</h2>
                <span style="font-size: 0.85rem; color: #94a3b8;">{total_events} events logged</span>
            </div>
            <table>
                <thead>
                    <tr>
                        <th>Time</th>
                        <th>Route</th>
                        <th>Book</th>
                        <th>Progress</th>
                        <th>Result</th>
                    </tr>
                </thead>
                <tbody>
                    {events_html if events_html else "<tr><td colspan='5' style='color:#94a3b8; text-align:center; padding: 2rem;'>No sync events logged yet.</td></tr>"}
                </tbody>
            </table>
            <div class="pagination">
                <div>Page {event_page} of {event_total_pages}</div>
                <div style="display: flex; gap: 0.5rem;">
                    <a href="{prev_event_link}" class="btn btn-secondary {'btn-disabled' if event_page <= 1 else ''}">← Previous</a>
                    <a href="{next_event_link}" class="btn btn-secondary {'btn-disabled' if event_page >= event_total_pages else ''}">Next →</a>
                </div>
            </div>
        </div>
    </div>
    <script>
    async function linkDocument(doc) {{
        const idStr = prompt("Enter Calibre Book ID for document " + doc + ":");
        if (!idStr) return;
        const calId = parseInt(idStr.trim(), 10);
        if (isNaN(calId) || calId <= 0) {{
            alert("Invalid Calibre ID");
            return;
        }}
        try {{
            const res = await fetch("/api/link-document", {{
                method: "POST",
                headers: {{"Content-Type": "application/json"}},
                body: JSON.stringify({{document: doc, calibre_id: calId}})
            }});
            if (res.ok) {{
                window.location.reload();
            }} else {{
                alert("Failed to link document: " + await res.text());
            }}
        }} catch (e) {{
            alert("Error linking document: " + e);
        }}
    }}
    </script>
</body>
</html>"""
        return HTMLResponse(content=html)

    return app
