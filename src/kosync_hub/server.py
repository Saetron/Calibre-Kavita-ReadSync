"""FastAPI server implementing KOReader sync protocol and hub proxy."""

import asyncio
import logging
import time
from datetime import datetime
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse

from .config import AppConfig
from .db import InternalDatabase
from .models import HubStatus, ProgressPayload, ProgressRecord, ProgressResponse, UserAuthRequest
from .synchronizer import Synchronizer

logger = logging.getLogger("kosync_hub.server")


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
            asyncio.create_task(synchronizer.calibre.test_connection())

        # Scan Calibre library if enabled
        if config.sync.scan_on_startup and hasattr(synchronizer.calibre, "scan_and_index_library"):
            loop = asyncio.get_event_loop()
            loop.run_in_executor(None, synchronizer.calibre.scan_and_index_library)

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
    # KOReader Sync Protocol Endpoints
    # -------------------------------------------------------------------------

    @app.get("/users/auth", status_code=status.HTTP_200_OK)
    async def users_auth(
        x_auth_user: Optional[str] = Header(None),
        x_auth_key: Optional[str] = Header(None),
    ):
        """KOReader login / authorization check."""
        if config.server.auth_username and x_auth_user != config.server.auth_username:
            raise HTTPException(status_code=401, detail="Invalid username or key")
        return {"message": "Authorized", "authorized": "OK"}

    @app.post("/users/create", status_code=status.HTTP_201_CREATED)
    async def users_create(payload: UserAuthRequest):
        """KOReader user registration endpoint."""
        logger.info(f"User registration request received for '{payload.username}'")
        return {"message": "User registered successfully"}

    @app.put("/syncs/progress", status_code=status.HTTP_200_OK)
    async def update_progress(payload: ProgressPayload):
        """
        Receives progress update from KOReader device.
        Immediately fans out the update to both Kavita and Calibre.
        """
        now_ts = int(datetime.utcnow().timestamp())
        meta = payload.metadata
        record = ProgressRecord(
            document=payload.document,
            progress=payload.progress,
            percentage=payload.percentage,
            timestamp=now_ts,
            device=payload.device,
            device_id=payload.device_id,
            title=meta.title if meta else None,
            authors=meta.authors if meta else None,
            filename=meta.filename if meta else None,
        )

        # Save to local database
        db.upsert_progress(record)

        # Concurrent fanout to both Kavita and Calibre
        tasks = []
        if synchronizer.kavita:
            tasks.append(synchronizer.kavita.update_progress(record))
        if synchronizer.calibre:
            tasks.append(synchronizer.calibre.update_progress(record))

        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            logger.debug(f"Progress fanout results for {payload.document}: {results}")

        return {
            "document": payload.document,
            "timestamp": now_ts,
            "status": "ok",
        }

    @app.get("/syncs/progress/{document}", response_model=ProgressResponse)
    async def get_progress(document: str):
        """
        Retrieves current reading progress for KOReader.
        Checks local database and upstreams, returning the winning progress.
        """
        # Trigger single-document sync to reconcile upstreams
        sync_res = await synchronizer.sync_document(document)

        record = db.get_document(document)
        if not record:
            raise HTTPException(status_code=404, detail="No progress found for document")

        return ProgressResponse(
            document=record.document,
            progress=record.progress,
            percentage=record.percentage,
            timestamp=record.timestamp,
            device=record.device or "kosync-hub",
        )

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
        return {"status": "completed", "result": res}

    @app.get("/api/books")
    async def api_books():
        rows = db.get_all_tracked_documents()
        return [dict(r) for r in rows]

    @app.get("/", response_class=HTMLResponse)
    async def dashboard():
        tracked_count = db.count_tracked()
        recent_events = db.get_recent_events(limit=15)
        tracked_docs = db.get_all_tracked_documents()[:15]

        rows_html = "".join([
            f"""<tr>
                <td><strong>{d['title'] or 'Unknown'}</strong><br><small>{d['authors'] or ''}</small></td>
                <td><span class="badge badge-primary">#{d['calibre_book_id'] or '-'}</span></td>
                <td><code>{d['document'][:12]}...</code></td>
                <td><div class="progress-bar"><div class="fill" style="width: {min(100, round(d['percentage'] * 100))}%"></div></div> {round(d['percentage'] * 100, 1)}%</td>
                <td>{datetime.fromtimestamp(d['timestamp']).strftime('%Y-%m-%d %H:%M')}</td>
                <td><span class="badge badge-success">{d['last_sync_status'] or 'synced'}</span></td>
            </tr>"""
            for d in tracked_docs
        ])

        events_html = "".join([
            f"""<tr>
                <td>{datetime.fromtimestamp(e['timestamp']).strftime('%H:%M:%S')}</td>
                <td><span class="badge badge-info">{e['source']}</span> → <span class="badge badge-primary">{e['target']}</span></td>
                <td>{f"#{e['calibre_id']}" if e['calibre_id'] else e['document'][:8] + '...'}</td>
                <td>{round(e['percentage'] * 100, 1)}%</td>
                <td>{'✅' if e['success'] else '❌'} {e['message'] or ''}</td>
            </tr>"""
            for e in recent_events
        ])

        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>KOReader Kavita & Calibre Sync</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; background: #0f172a; color: #f8fafc; margin: 0; padding: 2rem; }}
        .container {{ max-width: 1100px; margin: 0 auto; }}
        h1 {{ margin-top: 0; color: #38bdf8; font-size: 1.8rem; display: flex; align-items: center; gap: 0.5rem; }}
        .card {{ background: #1e293b; border-radius: 8px; padding: 1.5rem; margin-bottom: 1.5rem; border: 1px solid #334155; }}
        .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 1rem; margin-bottom: 1.5rem; }}
        .stat-card {{ background: #1e293b; border-radius: 8px; padding: 1rem; border: 1px solid #334155; }}
        .stat-value {{ font-size: 1.8rem; font-weight: bold; color: #38bdf8; }}
        .stat-label {{ font-size: 0.85rem; color: #94a3b8; text-transform: uppercase; }}
        table {{ width: 100%; border-collapse: collapse; margin-top: 0.5rem; }}
        th, td {{ padding: 0.75rem; text-align: left; border-bottom: 1px solid #334155; }}
        th {{ color: #94a3b8; font-weight: 600; font-size: 0.85rem; text-transform: uppercase; }}
        .badge {{ padding: 0.2rem 0.5rem; border-radius: 4px; font-size: 0.75rem; font-weight: 600; }}
        .badge-success {{ background: #065f46; color: #34d399; }}
        .badge-primary {{ background: #1e40af; color: #93c5fd; }}
        .badge-info {{ background: #374151; color: #e2e8f0; }}
        .progress-bar {{ width: 100px; height: 8px; background: #334155; border-radius: 4px; display: inline-block; overflow: hidden; vertical-align: middle; margin-right: 6px; }}
        .fill {{ height: 100%; background: #38bdf8; border-radius: 4px; }}
        .btn {{ background: #0284c7; color: white; border: none; padding: 0.5rem 1rem; border-radius: 6px; cursor: pointer; font-weight: 600; }}
        .btn:hover {{ background: #0369a1; }}
        code {{ background: #0f172a; padding: 0.2rem 0.4rem; border-radius: 4px; color: #e2e8f0; font-size: 0.85rem; }}
    </style>
</head>
<body>
    <div class="container">
        <h1>📖 Kavita & Calibre Sync</h1>
        <p style="color: #94a3b8;">Bidirectional progress synchronization matching via Calibre IDs in filenames (<code>{{id}}</code>).</p>
        
        <div class="grid">
            <div class="stat-card">
                <div class="stat-value">{tracked_count}</div>
                <div class="stat-label">Tracked Books</div>
            </div>
            <div class="stat-card">
                <div class="stat-value">{'Active' if synchronizer.kavita else 'Disabled'}</div>
                <div class="stat-label">Kavita Server</div>
            </div>
            <div class="stat-card">
                <div class="stat-value">{'Active' if synchronizer.calibre else 'Disabled'}</div>
                <div class="stat-label">Calibre DB</div>
            </div>
            <div class="stat-card">
                <div class="stat-value">{config.sync.interval_seconds}s</div>
                <div class="stat-label">Sync Frequency</div>
            </div>
        </div>

        <div class="card">
            <div style="display: flex; justify-content: space-between; align-items: center;">
                <h2 style="margin: 0; font-size: 1.2rem;">Recent Books & Progress</h2>
                <form action="/api/sync-now" method="POST" onsubmit="event.preventDefault(); fetch('/api/sync-now', {{method: 'POST'}}).then(() => location.reload());">
                    <button class="btn" type="submit">🔄 Sync Now</button>
                </form>
            </div>
            <table>
                <thead>
                    <tr>
                        <th>Book</th>
                        <th>Calibre ID</th>
                        <th>Document Hash</th>
                        <th>Progress</th>
                        <th>Last Activity</th>
                        <th>Status</th>
                    </tr>
                </thead>
                <tbody>
                    {rows_html if rows_html else "<tr><td colspan='6' style='color:#94a3b8; text-align:center;'>No books synced yet. Start reading in KOReader or Kavita!</td></tr>"}
                </tbody>
            </table>
        </div>

        <div class="card">
            <h2 style="margin: 0 0 1rem 0; font-size: 1.2rem;">Recent Sync Activity</h2>
            <table>
                <thead>
                    <tr>
                        <th>Time</th>
                        <th>Route</th>
                        <th>Doc</th>
                        <th>Percentage</th>
                        <th>Message</th>
                    </tr>
                </thead>
                <tbody>
                    {events_html if events_html else "<tr><td colspan='5' style='color:#94a3b8; text-align:center;'>No sync events logged yet.</td></tr>"}
                </tbody>
            </table>
        </div>
    </div>
</body>
</html>"""
        return HTMLResponse(content=html)

    return app
