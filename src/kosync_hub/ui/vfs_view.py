"""Rendering functions for the Kavita VFS tab of the web dashboard."""

from typing import Any, Dict


def render_vfs_tab(
    config: Any,
    vfs_summary: Dict[str, Any],
    is_active: bool = False,
) -> str:
    """Renders Tab 2: Kavita Virtual File System."""
    active_cls = "active" if is_active else ""
    collision_count = vfs_summary.get("collision_count", 0)

    collision_rows = "".join(
        [
            f"<tr><td>{c['relpath']}</td><td>#{c['existing_book_id']}</td><td>#{c['colliding_book_id']}</td><td style='color:var(--success);'>{c['resolved_path']}</td></tr>"
            for c in vfs_summary.get("collisions", [])
        ]
    )

    type_badges = "".join(
        [f'<span class="badge badge-info">{k}: <strong>{v}</strong></span>' for k, v in vfs_summary.get("type_counts", {}).items()]
    ) or '<span style="color:var(--text-muted); font-size:0.85rem;">None</span>'

    lang_badges = "".join(
        [f'<span class="badge badge-info">{k}: <strong>{v}</strong></span>' for k, v in vfs_summary.get("language_counts", {}).items()]
    ) or '<span style="color:var(--text-muted); font-size:0.85rem;">None</span>'

    collision_panel_display = "block" if collision_count > 0 else "none"
    collision_color = "var(--danger)" if collision_count > 0 else "var(--success)"

    return f"""<div id="tab-vfs" class="tab-content {active_cls}">
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
        <div id="vfs-collision-panel" class="alert-panel" style="display: {collision_panel_display};">
            <h3>⚠️ Path Collisions Detected ({collision_count})</h3>
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
                        {collision_rows}
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
                <div id="vfs-stat-collisions" class="stat-value" style="color: {collision_color};">{collision_count}</div>
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
                    {type_badges}
                </div>
                <div style="display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap; margin-left: auto;">
                    <span style="font-size:0.85rem; font-weight:600; color:var(--text-muted);">Languages:</span>
                    {lang_badges}
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
    </div>"""
