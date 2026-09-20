"""Rendering functions for the Settings tab of the web dashboard."""

from typing import Any


def render_settings_tab(
    config: Any,
    active_config_file: str,
    is_active: bool = False,
) -> str:
    """Renders Tab 4: System Settings & Configuration."""
    active_cls = "active" if is_active else ""
    masked_pwd = "******" if config.server.auth_password else ""

    return f"""<div id="tab-settings" class="tab-content {active_cls}">
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
                        <input type="password" id="cfg_server_pwd" value="{masked_pwd}" placeholder="Leave empty for open single-user">
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
    </div>"""
