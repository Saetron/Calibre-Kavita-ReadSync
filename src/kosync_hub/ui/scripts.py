"""Client-side JavaScript functions for the KOReader Sync Hub web dashboard."""

CLIENT_SCRIPTS = """
function switchTab(tabName) {
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
    
    const btn = document.getElementById('tab-btn-' + tabName);
    const content = document.getElementById('tab-' + tabName);
    if (btn && content) {
        btn.classList.add('active');
        content.classList.add('active');
        window.location.hash = '#' + tabName;
    }
    if (tabName === 'vfs') {
        loadVFSBooks();
    }
    if (tabName === 'stats') {
        loadReadingYearStats();
    }
}

// Listen to hash on load
window.addEventListener('DOMContentLoaded', () => {
    const hash = window.location.hash.replace('#', '');
    if (hash && ['sync', 'vfs', 'stats', 'settings'].includes(hash)) {
        switchTab(hash);
    } else {
        switchTab('stats');
    }
});

function linkDocument(doc) {
    const id = prompt('Enter Calibre Book ID to link with hash ' + doc + ':');
    if (id) {
        fetch('/api/link-document', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({document: doc, calibre_id: parseInt(id)})
        }).then(r => r.json()).then(d => {
            if (d.status === 'ok') {
                alert('Linked to Calibre Book #' + d.calibre_id + ' (' + (d.title || '') + ')!');
                location.reload();
            } else {
                alert('Error linking document: ' + JSON.stringify(d));
            }
        }).catch(e => alert('Error: ' + e));
    }
}

function triggerSyncNow(btn) {
    btn.disabled = true;
    btn.innerText = 'Syncing...';
    fetch('/api/sync-now', {method: 'POST'})
        .then(() => location.reload())
        .catch(e => { alert('Sync error: ' + e); btn.disabled = false; btn.innerText = '🔄 Sync Now'; });
}

function triggerBackfill(btn) {
    btn.disabled = true;
    btn.innerText = 'Backfilling...';
    fetch('/api/backfill', {method: 'POST'})
        .then(r => r.json())
        .then(d => { alert('Backfilled ' + d.backfilled_count + ' books from Calibre!'); location.reload(); })
        .catch(e => { alert('Error: ' + e); btn.disabled = false; btn.innerText = '📥 Backfill from Calibre'; });
}

function triggerVFSSync(btn) {
    btn.disabled = true;
    btn.innerText = 'Syncing VFS...';
    fetch('/api/vfs/sync', {method: 'POST'})
        .then(r => r.json())
        .then(d => {
            if (d.status === 'success') {
                alert('VFS Sync Complete! Created: ' + d.created + ', Updated: ' + d.updated + ', Deleted: ' + d.deleted);
                location.reload();
            } else {
                alert('VFS Sync error: ' + (d.message || JSON.stringify(d)));
                btn.disabled = false;
                btn.innerText = '🔄 Sync VFS Now';
            }
        })
        .catch(e => { alert('Error: ' + e); btn.disabled = false; btn.innerText = '🔄 Sync VFS Now'; });
}

function triggerVFSCleanup(btn) {
    if (!confirm('Clean up unregistered files and empty directories in VFS?')) return;
    btn.disabled = true;
    btn.innerText = 'Cleaning...';
    fetch('/api/vfs/cleanup', {method: 'POST'})
        .then(r => r.json())
        .then(d => {
            if (d.status === 'success') {
                alert('VFS Cleanup Complete! Removed ' + d.result.removed_count + ' stale files and ' + d.result.empty_dirs_removed + ' empty dirs.');
                location.reload();
            } else {
                alert('VFS Cleanup error: ' + (d.message || JSON.stringify(d)));
                btn.disabled = false;
                btn.innerText = '🧹 Cleanup VFS';
            }
        })
        .catch(e => { alert('Error: ' + e); btn.disabled = false; btn.innerText = '🧹 Cleanup VFS'; });
}

// VFS Books Table State & Pagination
let vfsCurrentPage = 1;
let vfsPageSize = 50;
let vfsTotalPages = 1;
let vfsSearchTimer = null;

function onVFSSearchInput() {
    clearTimeout(vfsSearchTimer);
    vfsSearchTimer = setTimeout(() => {
        vfsCurrentPage = 1;
        loadVFSBooks();
    }, 300);
}

function changeVFSPageSize() {
    vfsPageSize = parseInt(document.getElementById('vfs-page-size').value) || 50;
    vfsCurrentPage = 1;
    loadVFSBooks();
}

function goToVFSPage(page) {
    if (page < 1 || page > vfsTotalPages) return;
    vfsCurrentPage = page;
    loadVFSBooks();
}

async function loadVFSBooks() {
    const q = document.getElementById('vfs-search-input') ? document.getElementById('vfs-search-input').value.trim() : '';
    const offset = (vfsCurrentPage - 1) * vfsPageSize;
    try {
        const res = await fetch(`/api/vfs/books?q=${encodeURIComponent(q)}&limit=${vfsPageSize}&offset=${offset}`);
        const data = await res.json();
        const total = data.total || 0;
        vfsTotalPages = Math.max(1, Math.ceil(total / vfsPageSize));
        
        document.getElementById('vfs-table-count').innerText = `${total.toLocaleString()} books mapped in VFS`;
        document.getElementById('vfs-page-indicator').innerText = `Page ${vfsCurrentPage} of ${vfsTotalPages}`;
        document.getElementById('vfs-btn-prev').disabled = (vfsCurrentPage <= 1);
        document.getElementById('vfs-btn-next').disabled = (vfsCurrentPage >= vfsTotalPages);

        const rowsHtml = (data.items || []).map(b => `
            <tr>
                <td><span class="badge badge-primary">#${b.book_id}</span></td>
                <td><strong>${b.series || b.title}</strong><br><small style="color:#94a3b8;">${b.title != b.series ? b.title : ''}</small></td>
                <td>${b.volume ? 'Vol. ' + b.volume : '-'}</td>
                <td>${b.chapter ? 'Ch. ' + b.chapter : '-'}</td>
                <td><span class="badge badge-info">${b.type || 'Unknown'}</span></td>
                <td><span class="badge badge-info">${b.language || 'unknown'}</span></td>
                <td><code style="font-size:0.75rem;" title="${b.vfs_relpath || b.vfs_path}">${(b.vfs_relpath || b.vfs_path).split('/').pop()}</code></td>
                <td><code style="font-size:0.75rem;" title="${b.source_path}">${b.source_path.split('/').pop()}</code></td>
            </tr>
        `).join('');

        document.getElementById('vfs-books-rows').innerHTML = rowsHtml || '<tr><td colspan="8" style="text-align:center; color:#94a3b8; padding:2rem;">No books found matching search criteria.</td></tr>';
    } catch (e) {
        document.getElementById('vfs-books-rows').innerHTML = `<tr><td colspan="8" style="text-align:center; color:var(--danger); padding:2rem;">Error loading VFS books: ${e}</td></tr>`;
    }
}

async function saveConfig() {
    const saveBtn = document.getElementById('btn-save-config');
    const alertBox = document.getElementById('config-alert');
    saveBtn.disabled = true;
    saveBtn.innerText = 'Saving...';
    alertBox.style.display = 'none';

    const payload = {
        server: {
            host: document.getElementById('cfg_server_host').value.trim(),
            port: parseInt(document.getElementById('cfg_server_port').value) || 8080,
            auth_username: document.getElementById('cfg_server_user').value.trim() || null,
            auth_password: document.getElementById('cfg_server_pwd').value || null,
        },
        calibre: {
            enabled: document.getElementById('cfg_calibre_enabled').checked,
            library_path: document.getElementById('cfg_calibre_path').value.trim(),
            read_pct_column: document.getElementById('cfg_calibre_pct').value.trim(),
            read_status_column: document.getElementById('cfg_calibre_status').value.trim(),
            last_read_column: document.getElementById('cfg_calibre_last_read').value.trim(),
            progress_column: document.getElementById('cfg_calibre_progress').value.trim(),
            auto_create_columns: document.getElementById('cfg_calibre_auto_create').checked,
            mark_read_threshold: parseFloat(document.getElementById('cfg_calibre_threshold').value) || 0.98,
        },
        kavita: {
            enabled: document.getElementById('cfg_kavita_enabled').checked,
            base_url: document.getElementById('cfg_kavita_url').value.trim(),
            api_key: document.getElementById('cfg_kavita_key').value.trim(),
            timeout: parseFloat(document.getElementById('cfg_kavita_timeout').value) || 10.0,
        },
        sync: {
            enabled: document.getElementById('cfg_sync_enabled').checked,
            interval_seconds: parseInt(document.getElementById('cfg_sync_interval').value) || 300,
            conflict_resolution: document.getElementById('cfg_sync_conflict').value,
            scan_on_startup: document.getElementById('cfg_sync_scan').checked,
        },
        vfs: {
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
        },
        data_dir: document.getElementById('cfg_data_dir').value.trim() || '/app/data',
    };

    try {
        const res = await fetch('/api/config', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(payload),
        });
        const data = await res.json();
        if (res.ok && data.status === 'success') {
            alertBox.className = 'alert-panel';
            alertBox.style.background = 'rgba(34, 197, 94, 0.15)';
            alertBox.style.border = '1px solid #22c55e';
            alertBox.style.color = '#34d399';
            alertBox.innerHTML = `<strong>✔ Configuration Saved!</strong> Saved to <code>${data.saved_to}</code>. Settings have been applied.`;
            alertBox.style.display = 'block';
            alertBox.scrollIntoView({behavior: 'smooth'});
        } else {
            throw new Error(data.detail || data.message || 'Failed to save configuration');
        }
    } catch (err) {
        alertBox.className = 'alert-panel';
        alertBox.style.background = 'rgba(239, 68, 68, 0.15)';
        alertBox.style.border = '1px solid #ef4444';
        alertBox.style.color = '#f87171';
        alertBox.innerHTML = `<strong>❌ Error Saving Configuration:</strong> ${err.message}`;
        alertBox.style.display = 'block';
        alertBox.scrollIntoView({behavior: 'smooth'});
    } finally {
        saveBtn.disabled = false;
        saveBtn.innerText = '💾 Save Configuration';
    }
}

// Year in Review & Reading Statistics
let currentReadingYear = '2026';

async function loadReadingYearStats(year) {
    const yr = year || currentReadingYear || 'all';
    currentReadingYear = yr;
    try {
        const res = await fetch(`/api/stats/reading?year=${yr}`);
        const data = await res.json();
        if (data.error) return;

        document.getElementById('stats-books-completed').innerText = data.books_completed || 0;
        document.getElementById('stats-pages-read').innerText = (data.estimated_pages || 0).toLocaleString();
        const topA = (data.top_read_authors && data.top_read_authors.length > 0) ? data.top_read_authors[0].name : 'None';
        document.getElementById('stats-top-author').innerText = topA;
        document.getElementById('stats-top-author').title = topA;
        document.getElementById('stats-peak-month').innerText = data.peak_month || '-';

        document.getElementById('chart-year-label').innerText = data.display_year || yr;
        document.getElementById('table-year-label').innerText = data.display_year || yr;

        const yrs = data.available_years || [];
        let yrBtnsHtml = `<button class="btn ${yr === 'all' ? 'btn-primary' : 'btn-secondary'}" style="padding: 0.3rem 0.75rem; font-size: 0.85rem;" onclick="loadReadingYearStats('all')">All Time</button>`;
        yrs.forEach(y => {
            yrBtnsHtml += `<button class="btn ${String(yr) === String(y) ? 'btn-primary' : 'btn-secondary'}" style="padding: 0.3rem 0.75rem; font-size: 0.85rem;" onclick="loadReadingYearStats('${y}')">${y}</button>`;
        });
        document.getElementById('year-selector').innerHTML = yrBtnsHtml;

        const counts = data.monthly_counts || {};
        const maxCount = Math.max(1, ...Object.values(counts));
        const months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
        
        const chartHtml = months.map(m => {
            const cnt = counts[m] || 0;
            const hPct = cnt > 0 ? Math.max(12, Math.round((cnt / maxCount) * 100)) : 4;
            const barBg = cnt > 0 ? 'linear-gradient(180deg, var(--primary) 0%, #0284c7 100%)' : '#334155';
            return `
                <div style="flex: 1; display: flex; flex-direction: column; align-items: center; height: 100%; justify-content: flex-end;">
                    <span style="font-size: 0.75rem; font-weight: 600; color: ${cnt > 0 ? 'var(--text)' : 'transparent'}; margin-bottom: 4px;">${cnt}</span>
                    <div style="width: 100%; max-width: 28px; height: ${hPct}%; background: ${barBg}; border-radius: 4px 4px 0 0; transition: height 0.3s;" title="${m}: ${cnt} books"></div>
                    <span style="font-size: 0.75rem; color: var(--text-muted); margin-top: 6px;">${m}</span>
                </div>
            `;
        }).join('');
        document.getElementById('monthly-chart-container').innerHTML = chartHtml;

        const books = data.books || [];
        if (books.length === 0) {
            document.getElementById('completed-books-rows').innerHTML = '<tr><td colspan="4" style="text-align:center; color:#94a3b8; padding:1.5rem;">No books recorded as finished for this period.</td></tr>';
        } else {
            document.getElementById('completed-books-rows').innerHTML = books.map(b => `
                <tr>
                    <td><strong>${b.title}</strong></td>
                    <td style="color: #cbd5e1;">${b.authors || '-'}</td>
                    <td><code style="font-size: 0.8rem; color: var(--primary);">${b.date || '-'}</code></td>
                    <td><span class="badge badge-success">Finished</span></td>
                </tr>
            `).join('');
        }
    } catch (err) {
        console.error('Error loading reading stats:', err);
    }
}

async function runDbCleanup(btn) {
    const alertBox = document.getElementById('cleanup-alert');
    btn.disabled = true;
    btn.innerText = 'Cleaning up...';
    alertBox.style.display = 'none';

    const payload = {
        retention_days: 14,
        max_events: 5000,
        clear_hashes: document.getElementById('clean_purge_hashes').checked,
        vacuum: document.getElementById('clean_vacuum').checked,
    };

    try {
        const res = await fetch('/api/database/cleanup', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(payload),
        });
        const data = await res.json();
        if (res.ok && data.status === 'success') {
            alertBox.className = 'alert-panel';
            alertBox.style.background = 'rgba(34, 197, 94, 0.15)';
            alertBox.style.border = '1px solid #22c55e';
            alertBox.style.color = '#34d399';
            alertBox.innerHTML = `<strong>✔ Database Cleanup Succeeded!</strong> ` +
                `Pruned ${data.events_deleted} old audit events and ${data.hashes_deleted} candidate hashes. ` +
                `Disk size: ${data.size_initial_mb} MB ➔ <strong>${data.size_final_mb} MB</strong> ` +
                `(Reclaimed <strong>${data.reclaimed_mb} MB</strong>).`;
            alertBox.style.display = 'block';

            document.getElementById('db-size-badge').innerText = `kosync_hub.sqlite3 (${data.size_final_mb} MB)`;
            if (data.hashes_deleted > 0) {
                document.getElementById('cnt-hashes').innerText = '0';
            }
        } else {
            throw new Error(data.message || 'Cleanup failed');
        }
    } catch (err) {
        alertBox.className = 'alert-panel';
        alertBox.style.background = 'rgba(239, 68, 68, 0.15)';
        alertBox.style.border = '1px solid #ef4444';
        alertBox.style.color = '#f87171';
        alertBox.innerHTML = `<strong>❌ Error during cleanup:</strong> ${err.message}`;
        alertBox.style.display = 'block';
    } finally {
        btn.disabled = false;
        btn.innerText = '🧹 Run Database Cleanup & Vacuum';
    }
}

function toggleTopStatsFilter(mode) {
    const isRead = (mode === 'read');
    const aAll = document.getElementById('top-authors-all');
    const aRead = document.getElementById('top-authors-read');
    const sAll = document.getElementById('top-series-all');
    const sRead = document.getElementById('top-series-read');
    const pAll = document.getElementById('top-publishers-all');
    const pRead = document.getElementById('top-publishers-read');

    if (aAll && aRead) {
        aAll.style.display = isRead ? 'none' : '';
        aRead.style.display = isRead ? '' : 'none';
    }
    if (sAll && sRead) {
        sAll.style.display = isRead ? 'none' : '';
        sRead.style.display = isRead ? '' : 'none';
    }
    if (pAll && pRead) {
        pAll.style.display = isRead ? 'none' : '';
        pRead.style.display = isRead ? '' : 'none';
    }

    const lblFilter = isRead ? '(Read Only)' : '(All Books)';
    if (document.getElementById('lbl-authors-filter')) document.getElementById('lbl-authors-filter').innerText = lblFilter;
    if (document.getElementById('lbl-series-filter')) document.getElementById('lbl-series-filter').innerText = lblFilter;
    if (document.getElementById('lbl-pub-filter')) document.getElementById('lbl-pub-filter').innerText = lblFilter;

    const btnAll = document.getElementById('btn-top-all');
    const btnRead = document.getElementById('btn-top-read');
    if (btnAll && btnRead) {
        if (isRead) {
            btnRead.className = 'btn btn-primary';
            btnAll.className = 'btn btn-secondary';
        } else {
            btnAll.className = 'btn btn-primary';
            btnRead.className = 'btn btn-secondary';
        }
    }
}
"""
