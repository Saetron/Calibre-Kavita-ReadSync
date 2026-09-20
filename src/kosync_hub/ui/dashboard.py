"""Dashboard HTML page assembler for KOReader Sync Hub & Kavita VFS."""

from typing import Any, Dict, List, Optional

from .scripts import CLIENT_SCRIPTS
from .settings_view import render_settings_tab
from .stats_view import render_stats_tab
from .styles import CSS_STYLES
from .sync_view import render_sync_tab
from .vfs_view import render_vfs_tab


def render_dashboard_html(
    config: Any,
    base_url: str,
    stats: Dict[str, Any],
    vfs_summary: Dict[str, Any],
    db_counts: Dict[str, Any],
    lib_stats: Dict[str, Any],
    tracked_docs: List[Any],
    recent_events: List[Any],
    page: int,
    total_pages: int,
    total_docs: int,
    event_page: int,
    event_total_pages: int,
    search: Optional[str] = None,
    tab: Optional[str] = None,
    active_config_file: str = "",
    db: Any = None,
) -> str:
    """Assembles the complete HTML document for the multi-tab web dashboard."""
    active_tab = tab or "stats"

    sync_badge_cls = "badge-success" if config.sync.enabled else "badge-info"
    sync_badge_text = "ON" if config.sync.enabled else "OFF"
    vfs_badge_cls = "badge-success" if config.vfs.enabled else "badge-info"
    vfs_badge_text = config.vfs.mode.upper() if config.vfs.enabled else "OFF"

    stats_tab_html = render_stats_tab(
        lib_stats=lib_stats,
        db_counts=db_counts,
        is_active=(active_tab == "stats"),
    )

    sync_tab_html = render_sync_tab(
        base_url=base_url,
        stats=stats,
        tracked_docs=tracked_docs,
        recent_events=recent_events,
        page=page,
        total_pages=total_pages,
        total_docs=total_docs,
        event_page=event_page,
        event_total_pages=event_total_pages,
        search=search,
        is_active=(active_tab == "sync"),
        db=db,
    )

    vfs_tab_html = render_vfs_tab(
        config=config,
        vfs_summary=vfs_summary,
        is_active=(active_tab == "vfs"),
    )

    settings_tab_html = render_settings_tab(
        config=config,
        active_config_file=active_config_file,
        is_active=(active_tab == "settings"),
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>KOReader Sync Hub & Kavita VFS</title>
    <style>
{CSS_STYLES}
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
                <span class="badge {sync_badge_cls}">Sync: {sync_badge_text}</span>
                <span class="badge {vfs_badge_cls}">VFS: {vfs_badge_text}</span>
            </div>
        </header>

        <!-- Navigation Tabs (Statistics is the home page) -->
        <div class="tabs-nav">
            <button id="tab-btn-stats" class="tab-btn {'active' if active_tab == 'stats' else ''}" onclick="switchTab('stats')">📊 Statistics</button>
            <button id="tab-btn-sync" class="tab-btn {'active' if active_tab == 'sync' else ''}" onclick="switchTab('sync')">📱 Reading Sync</button>
            <button id="tab-btn-vfs" class="tab-btn {'active' if active_tab == 'vfs' else ''}" onclick="switchTab('vfs')">🗂️ Kavita VFS</button>
            <button id="tab-btn-settings" class="tab-btn {'active' if active_tab == 'settings' else ''}" onclick="switchTab('settings')">⚙️ Settings</button>
        </div>

        {stats_tab_html}
        {sync_tab_html}
        {vfs_tab_html}
        {settings_tab_html}
    </div>

    <!-- Scripts -->
    <script>
{CLIENT_SCRIPTS}
    </script>
</body>
</html>"""
