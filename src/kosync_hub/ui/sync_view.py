"""Rendering functions for the Reading Sync tab of the web dashboard."""

from datetime import datetime
from typing import Any, Dict, List, Optional


def render_book_row(d: Dict[str, Any], db: Any = None) -> str:
    """Renders a single row in the tracked documents / books table."""
    t_title = d.get("title") or "Unknown"
    t_authors = d.get("authors") or ""
    t_cal_id = d.get("calibre_book_id")
    t_doc = str(d.get("document", ""))
    t_device = d.get("device") or "KOReader"

    pct = float(d.get("percentage", 0.0))
    ts = d.get("timestamp", 0)
    time_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "-"
    t_status = d.get("last_sync_status") or "synced"

    aliases = db.get_aliases_for_calibre_id(t_cal_id) if (db and t_cal_id) else []
    other_aliases = [a for a in aliases if a != t_doc]
    alias_badge = (
        f""" <span class="badge badge-info" title="Alternative linked hashes:\n{chr(10).join(other_aliases)}" style="cursor:help; font-size:0.75rem;">+{len(other_aliases)}</span>"""
        if other_aliases
        else ""
    )
    cal_badge = (
        f"""<span class="badge badge-primary">#{t_cal_id}</span>"""
        if t_cal_id
        else f"""<button class="btn" style="padding:2px 8px; font-size:0.75rem; background:#f59e0b; color:#fff; border:none; border-radius:4px; cursor:pointer;" onclick="linkDocument('{t_doc}')" title="Click to manually link to Calibre ID">#- (Link)</button>"""
    )

    return f"""<tr>
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


def render_event_row(e: Dict[str, Any]) -> str:
    """Renders a single row in the sync audit events table."""
    ts = e.get("timestamp", 0)
    time_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else "-"
    cal_id = e.get("calibre_id")
    doc = str(e.get("document", ""))
    ident = f"#{cal_id}" if cal_id else (doc[:8] + "..." if doc else "-")
    pct = round(float(e.get("percentage", 0.0)) * 100, 1)
    src = e.get("source", "unknown")
    tgt = e.get("target", "unknown")
    success = e.get("success", False)
    msg = e.get("message") or ""

    return f"""<tr>
        <td><small style="color:#94a3b8;">{time_str}</small></td>
        <td><span class="badge badge-info">{src}</span> → <span class="badge badge-primary">{tgt}</span></td>
        <td><strong>{ident}</strong></td>
        <td>{pct}%</td>
        <td>{'✅' if success else '❌'} <span style="font-size:0.85rem;">{msg}</span></td>
    </tr>"""


def render_sync_tab(
    base_url: str,
    stats: Dict[str, Any],
    tracked_docs: List[Any],
    recent_events: List[Any],
    page: int,
    total_pages: int,
    total_docs: int,
    event_page: int,
    event_total_pages: int,
    search: Optional[str] = None,
    is_active: bool = True,
    db: Any = None,
) -> str:
    """Renders Tab 1: Reading Sync."""
    active_cls = "active" if is_active else ""

    rows_html = "".join([render_book_row(dict(d), db=db) for d in tracked_docs])
    events_html = "".join([render_event_row(dict(e)) for e in recent_events])

    search_query_part = f"&search={search}" if search else ""
    prev_page_link = f"/?page={max(1, page - 1)}{search_query_part}&event_page={event_page}#sync"
    next_page_link = f"/?page={min(total_pages, page + 1)}{search_query_part}&event_page={event_page}#sync"
    prev_event_link = f"/?page={page}{search_query_part}&event_page={max(1, event_page - 1)}#sync"
    next_event_link = f"/?page={page}{search_query_part}&event_page={min(event_total_pages, event_page + 1)}#sync"
    search_val = search or ""

    device_names = stats.get("device_names", [])
    devices_title = ", ".join(device_names) if device_names else "No devices registered"
    devices_count = stats.get("devices_count", 1 if stats.get("total_tracked", 0) > 0 else 0)

    return f"""<div id="tab-sync" class="tab-content {active_cls}">
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
                <div class="stat-value">{stats.get('total_tracked', 0)}</div>
                <div class="stat-label">📚 Tracked Books</div>
            </div>
            <div class="stat-card">
                <div class="stat-value" style="color: var(--success);">{stats.get('completed_books', 0)}</div>
                <div class="stat-label">🏆 Completed (≥98%)</div>
            </div>
            <div class="stat-card">
                <div class="stat-value" style="color: var(--warning);">{stats.get('in_progress', 0)}</div>
                <div class="stat-label">📖 In Progress</div>
            </div>
            <div class="stat-card">
                <div class="stat-value" style="color: #a78bfa;">{stats.get('syncs_today', 0)}</div>
                <div class="stat-label">⚡ Syncs (Today)</div>
            </div>
            <div class="stat-card">
                <div class="stat-value">{stats.get('syncs_week', 0)}</div>
                <div class="stat-label">📅 Syncs (7 Days)</div>
            </div>
            <div class="stat-card" title="{devices_title}">
                <div class="stat-value" style="color: var(--primary);">{devices_count}</div>
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
    </div>"""
