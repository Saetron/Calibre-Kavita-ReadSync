"""Rendering functions for the Statistics and Year in Review tab of the web dashboard."""

from typing import Any, Dict, List


def render_formats_badges(formats: List[Dict[str, Any]]) -> str:
    """Renders badges showing format distribution and sizes."""
    if not formats:
        return "<span style='color:#94a3b8;'>No format data available</span>"
    items = []
    for f in formats:
        items.append(
            f'<span class="badge badge-info" style="font-size: 0.85rem; padding: 0.4rem 0.8rem;">'
            f'<strong>{f["format"]}</strong>: {f["count"]:,} books ({f["size_mb"]} MB)'
            f'</span>'
        )
    return "".join(items)


def render_languages_badges(languages: List[Dict[str, Any]]) -> str:
    """Renders badges showing language distribution."""
    if not languages:
        return "<span style='color:#94a3b8;'>No language data available</span>"
    items = []
    for l in languages:
        items.append(
            f'<span class="badge badge-info" style="font-size: 0.85rem; padding: 0.4rem 0.8rem;">'
            f'<strong>{l["code"].upper()}</strong>: {l["count"]:,} books'
            f'</span>'
        )
    return "".join(items)


def render_top_authors_table(top_authors: List[Dict[str, Any]]) -> str:
    """Renders top 10 authors table rows."""
    if not top_authors:
        return "<tr><td colspan='2' style='text-align:center; color:#94a3b8;'>No author data</td></tr>"
    items = []
    for a in top_authors:
        items.append(
            f'<tr><td><strong>{a["name"]}</strong></td><td style="text-align:right;"><span class="badge badge-primary">{a["count"]}</span></td></tr>'
        )
    return "".join(items)


def render_top_series_table(top_series: List[Dict[str, Any]]) -> str:
    """Renders top 10 series table rows."""
    if not top_series:
        return "<tr><td colspan='2' style='text-align:center; color:#94a3b8;'>No series data</td></tr>"
    items = []
    for s in top_series:
        items.append(
            f'<tr><td><strong>{s["name"]}</strong></td><td style="text-align:right;"><span class="badge badge-primary">{s["count"]}</span></td></tr>'
        )
    return "".join(items)


def render_top_publishers_table(top_publishers: List[Dict[str, Any]]) -> str:
    """Renders top 10 publishers table rows."""
    if not top_publishers:
        return "<tr><td colspan='2' style='text-align:center; color:#94a3b8;'>No publisher data</td></tr>"
    items = []
    for p in top_publishers:
        items.append(
            f'<tr><td><strong>{p["name"]}</strong></td><td style="text-align:right;"><span class="badge badge-primary">{p["count"]}</span></td></tr>'
        )
    return "".join(items)


def render_top_tags_badges(top_tags: List[Dict[str, Any]]) -> str:
    """Renders popular tags & genres chips."""
    if not top_tags:
        return "<span style='color:#94a3b8;'>No tag data</span>"
    items = []
    for t in top_tags:
        items.append(
            f'<span class="badge" style="background:#334155; color:#f8fafc; font-size: 0.85rem; padding: 0.35rem 0.7rem;">'
            f'{t["name"]} <small style="color:var(--primary); font-weight:bold;">({t["count"]})</small></span>'
        )
    return "".join(items)


def render_stats_tab(
    lib_stats: Dict[str, Any],
    db_counts: Dict[str, Any],
    is_active: bool = True,
) -> str:
    """Renders Statistics & Year in Review, Calibre Analytics, and Database Maintenance."""
    active_cls = "active" if is_active else ""
    db_size_mb = round(db_counts.get("file_size_bytes", 0) / (1024 * 1024), 2)

    formats_html = render_formats_badges(lib_stats.get("formats", []))
    languages_html = render_languages_badges(lib_stats.get("languages", []))

    # All books
    top_authors_html = render_top_authors_table(lib_stats.get("top_authors", []))
    top_series_html = render_top_series_table(lib_stats.get("top_series", []))
    top_publishers_html = render_top_publishers_table(lib_stats.get("top_publishers", []))

    # Read only (completed) books
    top_authors_read_html = render_top_authors_table(lib_stats.get("top_authors_read", []))
    top_series_read_html = render_top_series_table(lib_stats.get("top_series_read", []))
    top_publishers_read_html = render_top_publishers_table(lib_stats.get("top_publishers_read", []))

    top_tags_html = render_top_tags_badges(lib_stats.get("top_tags", []))

    return f"""<div id="tab-stats" class="tab-content {active_cls}">
        <!-- Section 1: Year in Review & Reading History -->
        <div class="card" style="border-left: 4px solid #a855f7;">
            <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 1rem; margin-bottom: 1.25rem;">
                <div>
                    <h2 style="margin: 0; color: #c084fc; font-size: 1.3rem;">🏆 Year in Review & Reading History</h2>
                    <span style="font-size: 0.85rem; color: var(--text-muted);">Reading progress, completed books, and yearly reading pace</span>
                </div>
                <!-- Year Selection Buttons -->
                <div id="year-selector" style="display: flex; gap: 0.4rem; flex-wrap: wrap;"></div>
            </div>

            <!-- Year Metrics KPI Grid -->
            <div class="grid" style="margin-bottom: 1.5rem;">
                <div class="stat-card">
                    <div class="stat-value" id="stats-books-completed" style="color: var(--success);">-</div>
                    <div class="stat-label">🎉 Books Completed</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value" id="stats-pages-read" style="color: var(--primary);">-</div>
                    <div class="stat-label">📄 Est. Pages Read</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value" id="stats-top-author" style="color: #c084fc; font-size: 1.3rem; text-overflow: ellipsis; overflow: hidden; white-space: nowrap;">-</div>
                    <div class="stat-label">👤 Top Author Read</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value" id="stats-peak-month" style="color: var(--warning); font-size: 1.3rem;">-</div>
                    <div class="stat-label">⚡ Peak Reading Month</div>
                </div>
            </div>

            <!-- Monthly Completion Bar Chart -->
            <div style="background: rgba(15, 23, 42, 0.6); padding: 1.25rem; border-radius: 8px; border: 1px solid var(--card-border); margin-bottom: 1.5rem;">
                <div style="font-size: 0.9rem; font-weight: 600; color: var(--text-muted); margin-bottom: 1rem; display: flex; justify-content: space-between;">
                    <span>Monthly Completed Books</span>
                    <span id="chart-year-label" style="color: var(--primary); font-weight: bold;">-</span>
                </div>
                <div id="monthly-chart-container" style="display: flex; align-items: flex-end; justify-content: space-between; height: 130px; gap: 8px; padding-top: 24px;">
                    <!-- Rendered by JS -->
                </div>
            </div>

            <!-- Completed Books Table for the Year -->
            <div>
                <h3 style="margin: 0 0 0.75rem 0; font-size: 1.05rem; color: var(--text);">📖 Finished Books in <span id="table-year-label">-</span></h3>
                <div style="max-height: 320px; overflow-y: auto; border: 1px solid var(--card-border); border-radius: 6px;">
                    <table>
                        <thead>
                            <tr>
                                <th>Title</th>
                                <th>Author</th>
                                <th>Date Finished</th>
                                <th>Status</th>
                            </tr>
                        </thead>
                        <tbody id="completed-books-rows">
                            <tr><td colspan="4" style="text-align:center; color:#94a3b8; padding:1.5rem;">Loading reading history...</td></tr>
                        </tbody>
                    </table>
                </div>
            </div>
        </div>

        <!-- Section 2: Calibre Library Analytics -->
        <div class="card" style="border-left: 4px solid var(--primary);">
            <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 1rem; margin-bottom: 1.25rem;">
                <div>
                    <h2 style="margin: 0; color: var(--primary); font-size: 1.3rem;">📚 Calibre Library Analytics</h2>
                    <span style="font-size: 0.85rem; color: var(--text-muted);">Overview of books, series, tags, publishers, and formats from Calibre metadata.db</span>
                </div>
                <!-- Filter Toggle: All Books vs Read Only -->
                <div style="display: flex; gap: 6px; align-items: center; background: #0f172a; padding: 4px 8px; border-radius: 6px; border: 1px solid var(--card-border);">
                    <span style="font-size: 0.8rem; color: var(--text-muted); font-weight: 600;">Show Top Lists:</span>
                    <button id="btn-top-all" class="btn btn-primary" style="padding: 3px 10px; font-size: 0.8rem;" onclick="toggleTopStatsFilter('all')">📚 All Books</button>
                    <button id="btn-top-read" class="btn btn-secondary" style="padding: 3px 10px; font-size: 0.8rem;" onclick="toggleTopStatsFilter('read')">🏆 Read Only (Finished)</button>
                </div>
            </div>

            <!-- Library KPI Cards -->
            <div class="grid" style="margin-bottom: 1.5rem;">
                <div class="stat-card">
                    <div class="stat-value">{lib_stats.get('total_books', 0):,}</div>
                    <div class="stat-label">📚 Total Books</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value">{lib_stats.get('total_authors', 0):,}</div>
                    <div class="stat-label">👥 Authors</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value">{lib_stats.get('total_series', 0):,}</div>
                    <div class="stat-label">🗂️ Series</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value">{lib_stats.get('total_tags', 0):,}</div>
                    <div class="stat-label">🏷️ Tags / Genres</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value">{lib_stats.get('total_publishers', 0):,}</div>
                    <div class="stat-label">🏢 Publishers</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value">{lib_stats.get('total_languages', 0):,}</div>
                    <div class="stat-label">🌐 Languages</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value" style="color: var(--success);">{lib_stats.get('total_size_gb', 0)} GB</div>
                    <div class="stat-label">💾 Library Storage</div>
                </div>
            </div>

            <!-- Formats & Languages breakdown -->
            <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 1.5rem; margin-bottom: 1.5rem;">
                <div>
                    <div style="font-size: 0.9rem; font-weight: 600; color: var(--text-muted); margin-bottom: 0.5rem;">📁 File Formats Breakdown</div>
                    <div style="display: flex; gap: 8px; flex-wrap: wrap;">
                        {formats_html}
                    </div>
                </div>
                <div>
                    <div style="font-size: 0.9rem; font-weight: 600; color: var(--text-muted); margin-bottom: 0.5rem;">🌐 Languages Breakdown</div>
                    <div style="display: flex; gap: 8px; flex-wrap: wrap;">
                        {languages_html}
                    </div>
                </div>
            </div>

            <!-- 3-Column Grid for Top Authors, Top Series & Top Publishers with Read Filter -->
            <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 1.5rem; margin-bottom: 1.5rem;">
                <!-- Top Authors -->
                <div style="background: rgba(15, 23, 42, 0.4); padding: 1rem; border-radius: 8px; border: 1px solid var(--card-border);">
                    <h4 style="margin: 0 0 0.75rem 0; color: var(--primary);">👤 Top 10 Authors <span id="lbl-authors-filter" style="font-size: 0.75rem; color: var(--text-muted); font-weight: normal;">(All Books)</span></h4>
                    <table>
                        <thead><tr><th>Author</th><th style="text-align:right;">Books</th></tr></thead>
                        <tbody id="top-authors-all">
                            {top_authors_html}
                        </tbody>
                        <tbody id="top-authors-read" style="display:none;">
                            {top_authors_read_html}
                        </tbody>
                    </table>
                </div>
                <!-- Top Series -->
                <div style="background: rgba(15, 23, 42, 0.4); padding: 1rem; border-radius: 8px; border: 1px solid var(--card-border);">
                    <h4 style="margin: 0 0 0.75rem 0; color: var(--primary);">🗂️ Top 10 Series <span id="lbl-series-filter" style="font-size: 0.75rem; color: var(--text-muted); font-weight: normal;">(All Books)</span></h4>
                    <table>
                        <thead><tr><th>Series</th><th style="text-align:right;">Books</th></tr></thead>
                        <tbody id="top-series-all">
                            {top_series_html}
                        </tbody>
                        <tbody id="top-series-read" style="display:none;">
                            {top_series_read_html}
                        </tbody>
                    </table>
                </div>
                <!-- Top Publishers -->
                <div style="background: rgba(15, 23, 42, 0.4); padding: 1rem; border-radius: 8px; border: 1px solid var(--card-border);">
                    <h4 style="margin: 0 0 0.75rem 0; color: var(--primary);">🏢 Top 10 Publishers <span id="lbl-pub-filter" style="font-size: 0.75rem; color: var(--text-muted); font-weight: normal;">(All Books)</span></h4>
                    <table>
                        <thead><tr><th>Publisher</th><th style="text-align:right;">Books</th></tr></thead>
                        <tbody id="top-publishers-all">
                            {top_publishers_html}
                        </tbody>
                        <tbody id="top-publishers-read" style="display:none;">
                            {top_publishers_read_html}
                        </tbody>
                    </table>
                </div>
            </div>

            <!-- Top Tags / Genres -->
            <div>
                <div style="font-size: 0.9rem; font-weight: 600; color: var(--text-muted); margin-bottom: 0.5rem;">Popular Tags & Genres</div>
                <div style="display: flex; gap: 6px; flex-wrap: wrap;">
                    {top_tags_html}
                </div>
            </div>
        </div>

        <!-- Section 3: Database Maintenance & Optimization -->
        <div class="card" style="border-left: 4px solid var(--warning);">
            <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 1rem; margin-bottom: 1rem;">
                <div>
                    <h2 style="margin: 0; color: var(--warning); font-size: 1.3rem;">🧹 Sync Database Maintenance & Optimization</h2>
                    <span style="font-size: 0.85rem; color: var(--text-muted);">Prune audit history, purge candidate filename caches, and reclaim disk space</span>
                </div>
                <div style="display: flex; align-items: center; gap: 0.5rem;">
                    <span style="color: var(--text-muted); font-size: 0.85rem;">Current Database File:</span>
                    <code id="db-size-badge" style="background: #0f172a; padding: 0.35rem 0.75rem; border-radius: 6px; border: 1px solid var(--card-border); color: var(--warning); font-weight: bold; font-size: 0.95rem;">
                        kosync_hub.sqlite3 ({db_size_mb} MB)
                    </code>
                </div>
            </div>

            <!-- Table Breakdown Chips -->
            <div style="display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 1.25rem;">
                <span class="badge badge-info" style="font-size: 0.85rem; padding: 0.35rem 0.7rem;">🗂️ Filename Hashes Cache: <strong id="cnt-hashes">{db_counts.get('filename_hashes', 0):,}</strong></span>
                <span class="badge badge-info" style="font-size: 0.85rem; padding: 0.35rem 0.7rem;">⚡ Sync Audit Events: <strong id="cnt-events">{db_counts.get('sync_events', 0):,}</strong></span>
                <span class="badge badge-info" style="font-size: 0.85rem; padding: 0.35rem 0.7rem;">📚 Tracked Books: <strong id="cnt-tracked">{db_counts.get('tracked_documents', 0):,}</strong></span>
                <span class="badge badge-info" style="font-size: 0.85rem; padding: 0.35rem 0.7rem;">📱 Device Aliases: <strong id="cnt-aliases">{db_counts.get('document_aliases', 0):,}</strong></span>
            </div>

            <!-- Cleanup Controls Form -->
            <div style="background: rgba(15, 23, 42, 0.4); padding: 1.25rem; border-radius: 8px; border: 1px solid var(--card-border);">
                <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 1rem; margin-bottom: 1.25rem;">
                    <div class="form-check">
                        <input type="checkbox" id="clean_prune_events" checked>
                        <div>
                            <strong>Prune Old Sync Events</strong><br>
                            <span style="font-size: 0.8rem; color: var(--text-muted);">Keep events from the last 14 days (caps audit table)</span>
                        </div>
                    </div>
                    <div class="form-check">
                        <input type="checkbox" id="clean_purge_hashes" checked>
                        <div>
                            <strong>Purge Candidate Filename Hashes</strong><br>
                            <span style="font-size: 0.8rem; color: var(--text-muted);">Reclaims ~300+ MB immediately. Active books remain permanently linked!</span>
                        </div>
                    </div>
                    <div class="form-check">
                        <input type="checkbox" id="clean_vacuum" checked>
                        <div>
                            <strong>Execute SQLite VACUUM</strong><br>
                            <span style="font-size: 0.8rem; color: var(--text-muted);">Rebuilds database file to physically return freed disk space to OS</span>
                        </div>
                    </div>
                </div>
                <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 0.75rem;">
                    <span style="font-size: 0.85rem; color: var(--text-muted);">Safe to run at any time. Active reading progress and linked devices are never deleted.</span>
                    <button id="btn-run-cleanup" class="btn btn-warning" onclick="runDbCleanup(this)">🧹 Run Database Cleanup & Vacuum</button>
                </div>
                <div id="cleanup-alert" class="alert-panel" style="display: none; margin-top: 1rem;"></div>
            </div>
        </div>
    </div>"""
