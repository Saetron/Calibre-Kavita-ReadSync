"""CSS styles for the KOReader Sync Hub web dashboard."""

CSS_STYLES = """
:root {
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
}
* { box-sizing: border-box; }
body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    background: var(--bg);
    color: var(--text);
    margin: 0;
    padding: 2rem;
}
.container { max-width: 1240px; margin: 0 auto; }
header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    flex-wrap: wrap;
    gap: 1rem;
    margin-bottom: 1.5rem;
    padding-bottom: 1rem;
    border-bottom: 1px solid var(--card-border);
}
h1 {
    margin: 0;
    color: var(--primary);
    font-size: 1.8rem;
    display: flex;
    align-items: center;
    gap: 0.5rem;
}
.subtitle { color: var(--text-muted); font-size: 0.9rem; margin-top: 4px; }

/* Tabs Navigation */
.tabs-nav {
    display: flex;
    gap: 8px;
    border-bottom: 2px solid var(--card-border);
    margin-bottom: 1.5rem;
}
.tab-btn {
    background: transparent;
    color: var(--text-muted);
    border: none;
    border-bottom: 3px solid transparent;
    padding: 10px 20px;
    font-size: 1rem;
    font-weight: 600;
    cursor: pointer;
    display: flex;
    align-items: center;
    gap: 8px;
    transition: all 0.2s;
}
.tab-btn:hover { color: var(--text); }
.tab-btn.active { color: var(--primary); border-bottom-color: var(--primary); }
.tab-content { display: none; }
.tab-content.active { display: block; }

/* Cards and Grid */
.card {
    background: var(--card-bg);
    border-radius: 8px;
    padding: 1.5rem;
    margin-bottom: 1.5rem;
    border: 1px solid var(--card-border);
}
.grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    gap: 1rem;
    margin-bottom: 1.5rem;
}
.stat-card {
    background: var(--card-bg);
    border-radius: 8px;
    padding: 1.2rem;
    border: 1px solid var(--card-border);
}
.stat-value {
    font-size: 2rem;
    font-weight: bold;
    color: var(--primary);
    margin-bottom: 0.25rem;
}
.stat-label {
    font-size: 0.8rem;
    color: var(--text-muted);
    text-transform: uppercase;
    letter-spacing: 0.05em;
}

/* Tables */
table { width: 100%; border-collapse: collapse; margin-top: 0.5rem; }
th, td {
    padding: 0.75rem;
    text-align: left;
    border-bottom: 1px solid var(--card-border);
}
th {
    color: var(--text-muted);
    font-weight: 600;
    font-size: 0.85rem;
    text-transform: uppercase;
}
.badge {
    padding: 0.2rem 0.5rem;
    border-radius: 4px;
    font-size: 0.75rem;
    font-weight: 600;
    display: inline-block;
}
.badge-success { background: #065f46; color: #34d399; }
.badge-primary { background: #1e40af; color: #93c5fd; }
.badge-info { background: #374151; color: #e2e8f0; }
.badge-warning { background: rgba(245, 158, 11, 0.2); color: #fbbf24; }
.badge-danger { background: rgba(239, 68, 68, 0.2); color: #f87171; }

.progress-bar {
    width: 90px;
    height: 8px;
    background: #334155;
    border-radius: 4px;
    display: inline-block;
    overflow: hidden;
}
.fill { height: 100%; background: var(--primary); border-radius: 4px; }
.btn {
    background: #0284c7;
    color: white;
    border: none;
    padding: 0.5rem 1rem;
    border-radius: 6px;
    cursor: pointer;
    font-weight: 600;
    text-decoration: none;
    display: inline-flex;
    align-items: center;
    gap: 6px;
    font-size: 0.9rem;
}
.btn:hover { background: #0369a1; }
.btn-secondary { background: #334155; color: #f8fafc; padding: 0.4rem 0.8rem; }
.btn-secondary:hover { background: #475569; }
.btn-success { background: #059669; color: white; }
.btn-success:hover { background: #047857; }
.btn-warning { background: #d97706; color: white; }
.btn-warning:hover { background: #b45309; }
.btn-disabled { opacity: 0.4; pointer-events: none; }
.search-input {
    background: #0f172a;
    border: 1px solid var(--card-border);
    color: #f8fafc;
    padding: 0.5rem 0.8rem;
    border-radius: 6px;
    width: 260px;
    font-size: 0.9rem;
}
.search-input:focus { outline: none; border-color: var(--primary); }
code {
    background: #0f172a;
    padding: 0.2rem 0.4rem;
    border-radius: 4px;
    color: #e2e8f0;
    font-size: 0.85rem;
    font-family: var(--font-mono);
}
.pagination {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-top: 1rem;
    padding-top: 0.8rem;
    border-top: 1px solid var(--card-border);
    font-size: 0.9rem;
    color: var(--text-muted);
}

/* Alert Boxes */
.alert-panel {
    background: rgba(239, 68, 68, 0.15);
    border: 1px solid var(--danger);
    border-radius: 8px;
    padding: 1rem 1.25rem;
    margin-bottom: 1.5rem;
}
.alert-panel h3 {
    margin: 0 0 0.5rem 0;
    color: var(--danger);
    font-size: 1.05rem;
    display: flex;
    align-items: center;
    gap: 8px;
}

/* Settings Form Grid */
.form-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
    gap: 1.25rem;
}
.form-group { display: flex; flex-direction: column; gap: 6px; }
.form-group label {
    font-size: 0.85rem;
    font-weight: 600;
    color: var(--text-muted);
}
.form-group input, .form-group select {
    background: #0f172a;
    border: 1px solid var(--card-border);
    color: #f8fafc;
    padding: 0.5rem 0.75rem;
    border-radius: 6px;
    font-size: 0.9rem;
}
.form-group input:focus, .form-group select:focus {
    outline: none;
    border-color: var(--primary);
}
.form-check { display: flex; align-items: center; gap: 8px; margin-top: 4px; }
.form-check input { width: 16px; height: 16px; accent-color: var(--primary); }
.form-section-title {
    color: var(--primary);
    font-size: 1.1rem;
    margin: 1.25rem 0 0.75rem 0;
    padding-bottom: 0.4rem;
    border-bottom: 1px solid var(--card-border);
}
"""
