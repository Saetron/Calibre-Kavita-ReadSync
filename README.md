# 📖 Calibre-Kavita-ReadSync (`kosync-hub`)

A unified all-in-one daemon running in **Docker** that brings together:
1. **📱 KOReader Reading Progress Sync**: Bidirectional reading progress synchronization between **Kavita**, **Calibre's database** (`metadata.db`), and **KOReader / CrossPoint** e-readers.
2. **🗂️ Kavita VFS Generator**: High-performance virtual file system generator creating Kavita-compliant directory hierarchies (`Language/Type/Series/Series Vol. X Ch. Y {calibre_id}.ext`) using **hardlinks by default** (or symlinks/FUSE) from your Calibre library.

Both subsystems are **independent options** and can be toggled on or off individually. All settings can be configured via `config.yaml` or directly inside the **WebUI Settings Editor**, which saves changes back to the config file with instant hot-reloading.

---

## Key Features

- **⚡ Unified Single Container**: No need to run separate containers for VFS generation and reading sync. Exposes a single web dashboard and API on port 8080.
- **🔄 Independent Subsystems**:
  - `sync.enabled: true/false`: Run reading progress synchronization.
  - `vfs.enabled: true/false`: Run Kavita VFS library generation.
- **🔗 Hardlink Mode by Default**: VFS uses hardlinks by default, guaranteeing zero storage duplication, native filesystem performance, and compatibility across container boundaries on the same filesystem. (Symlink and FUSE modes are also supported).
- **⚙️ WebUI Configuration Editor**: Full settings management interface in the browser with password masking and atomic save-to-disk persistence.
- **🎯 100% Deterministic Book Matching**: Filenames in Kavita use the Calibre ID in curly brackets (e.g. `Berserk Vol. 1 {42}.cbz` or `Dune {123}.epub`), allowing instant, deterministic regex matching without fuzzy guesses.
- **📱 KOReader & CrossPoint Compatibility**: Native support for KOReader devices and e-ink readers running CrossPoint (e.g. Xteink X3/X4) with automatic hash-to-Calibre ID alias resolution.
- **⚡ High-Performance Delta Sync**: SQLite caching tracks generated VFS entries and reading sync states, performing fast sub-second delta synchronization passes when changes occur.

---

## Architecture

```
┌─────────────────┐       (Direct sync via /api/koreader/<apiKey>)
│ KOReader Device │ ─────────────────────────────────────────────┐
└─────────────────┘                                              ▼
┌──────────────────┐     (Sync via /syncs/progress)   ┌─────────────────────┐
│ CrossPoint (X3)  │ ─────────────────────────────────│    Kavita Server    │
└──────────────────┘                                  │  - On-Deck / Reads  │
                                                      │  - KOReader store   │
                                                      │  - VFS Library dir  │
                                                      └──────────┬──────────┘
                                                                 │
                                              ┌──────────────────┴──────────────────┐
                                              │                                     │
                                   Bidirectional Progress Sync              Monitors VFS Dir
                                   (Matches via Filename {id})             (Reads Hardlinks)
                                              │                                     │
                                              ▼                                     │
                               ┌──────────────────────────────────────────────┐     │
                               │        kosync-hub (Unified Container)        │     │
                               │  - Reading Progress Sync (port 8080)         │     │
                               │  - Kavita VFS Hardlink/Symlink Generator     │ ────┘
                               │  - Unified 3-Tab WebUI & Settings Editor     │
                               └──────────────────────┬───────────────────────┘
                                                      │
                                           Direct SQLite read/write
                                           (metadata.db & /vfs output)
                                                      │
                                                      ▼
                                       ┌─────────────────────────────┐
                                       │     Calibre in Docker       │
                                       │  - metadata.db (read/write) │
                                       │  - Book files & formats     │
                                       └─────────────────────────────┘
```

---

## Quickstart with Docker Compose

### 1. Create `config.yaml`
```bash
cp config.example.yaml config.yaml
```

Edit `config.yaml`:
```yaml
server:
  host: "0.0.0.0"
  port: 8080

calibre:
  enabled: true
  library_path: "/calibre/library"       # Path to Calibre library containing metadata.db
  read_pct_column: "#read_pct"           # Float percentage (0.0 to 100.0%)
  read_status_column: "#read_status"     # Yes/No boolean
  last_read_column: "#last_read"         # Timestamp of last reading activity
  progress_column: "#koreader_progress"  # KOReader CFI progress
  auto_create_columns: true              # Auto-creates custom columns in metadata.db
  mark_read_threshold: 0.98              # Mark read when progress >= 98%

kavita:
  enabled: true
  base_url: "http://kavita:5000"         # Kavita URL
  api_key: "YOUR_KAVITA_API_KEY"         # Kavita: User Settings -> API Keys

# Reading Progress Sync (Independent option)
sync:
  enabled: true
  interval_seconds: 300                  # Check recent reads every 5 minutes
  conflict_resolution: "latest_timestamp"
  scan_on_startup: true

# Kavita VFS Virtual File System Generator (Independent option)
vfs:
  enabled: true
  mode: "hardlink"                       # 'hardlink' (default), 'symlink', or 'fuse'
  vfs_dir: "/vfs"                        # Target output directory for Kavita library
  interval_seconds: 60                   # Check Calibre metadata.db for updates every 60s
  relative_links: false
  default_language: "unknown"
  default_type: "Unknown"
  custom_type_column: "type"
  custom_volume_column: "volume"
  custom_chapter_column: "chapter"

data_dir: "/app/data"
```

### 2. Configure `docker-compose.yml`

> [!IMPORTANT]
> **Hardlink Filesystem Requirement**:
> Hardlinks cannot cross filesystem boundaries. If using **Hardlink mode (default)**, ensure both your Calibre library and the VFS output directory reside on the **same physical filesystem/mount** (e.g. under `/mnt/media/books`):

```yaml
services:
  kosync-hub:
    build: .
    container_name: kosync-hub
    restart: unless-stopped
    ports:
      - "8080:8080"
    volumes:
      # Persistent cache and audit database
      - kosync_data:/app/data

      # Calibre library directory (where metadata.db is located)
      - /mnt/media/books/calibre:/calibre/library:rw

      # VFS output directory (point your Kavita library here)
      - /mnt/media/books/kavita_vfs:/vfs:rw

      # Configuration file (rw allows saving settings from the WebUI)
      - ./config.yaml:/app/config/config.yaml:rw
    environment:
      - CALIBRE_LIBRARY_PATH=/calibre/library
      - VFS_DIR=/vfs
      - VFS_MODE=hardlink
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8080/healthcheck"]
      interval: 30s
      timeout: 10s
      retries: 3

volumes:
  kosync_data:
```

### 3. Start the Daemon
```bash
docker compose up -d --build
```

Access the dashboard at `http://<server-ip>:8080`.

---

## Kavita VFS Subsystem

The Kavita VFS generator inspects Calibre's `metadata.db` and generates a Kavita-standard folder structure:

```
/vfs/
└── {Language}/
    └── {Type}/
        └── {Series}/
            └── {Series} Vol. {Volume} Ch. {Chapter} {{Calibre_ID}}.{ext}
```

### Path Formatting Examples
| Calibre Book Details | Generated Kavita VFS Path |
| :--- | :--- |
| **Manga**: *Berserk*, Vol. 1, ID 42 | `eng/Manga/Berserk/Berserk Vol. 1 {42}.cbz` |
| **Manga**: *Bleach*, Vol. 1-3, Ch. 1-20, ID 105 | `eng/Manga/Bleach/Bleach Vol. 1-3 Ch. 1-20 {105}.cbz` |
| **Light Novel**: *Sword Art Online*, Vol. 21, ID 88 | `eng/Light Novel/Sword Art Online/Sword Art Online Vol. 21 {88}.epub` |
| **Book**: *Dune*, ID 123 | `eng/Book/Dune/Dune {123}.epub` |

### Calibre Custom Columns
The VFS reader automatically discovers custom columns in Calibre for granular organization:
* `#type` (or custom name): The media category (e.g. `Manga`, `Comic`, `Light Novel`, `Book`).
* `#volume` (or custom name): Volume number or range (`1`, `1.5`, `1-3`).
* `#chapter` (or custom name): Chapter number or range (`100`, `105.5`, `1-20`).

---

## WebUI Dashboard

Open `http://<server-ip>:8080` to access the responsive 3-tab dashboard:

1. **📱 Reading Sync Tab**:
   - KOReader sync server URL and connection instructions.
   - Summary cards: Total Tracked, Completed ($\ge 98\%$), In Progress, Syncs Today & 7 Days.
   - Searchable, paginated table of tracked books with real-time reading progress bars, Calibre IDs, and device badges.
   - Real-time audit log with event filter.
   - **Sync Now** and **Backfill Calibre** buttons.

2. **🗂️ Kavita VFS Tab**:
   - VFS statistics: Total books, series, aggregated volume/chapter counts, and collision counts.
   - Distribution chips showing counts per media type (`Manga`, `Light Novel`, `Book`, etc.).
   - Mapped books table with server-side debounced search and pagination.
   - **Sync VFS** and **Cleanup Unregistered** buttons.

3. **⚙️ Settings Tab**:
   - Browser-based configuration editor for Server, Calibre, Kavita, Sync, and VFS settings.
   - Sensitive credentials masked automatically.
   - **Save Configuration** button commits updates directly to `config.yaml` on disk and instantly hot-reconfigures running services.

---

## CrossPoint (Xteink X3 / X4) Configuration

For e-ink devices running **CrossPoint** (e.g. Xteink X3 / X4) reading compressed ebooks:

1. On your Xteink device, navigate to **Settings ➔ System ➔ KOReader Sync**.
2. Set the **Sync Server URL** to:
   ```
   http://<YOUR_KOSYNC_HUB_IP>:8080
   ```
3. Enter any username and password (single-user open authentication accepts any credentials).
4. Tap **Authenticate**.
5. Retain the Calibre ID in curly brackets when naming your ebook files (e.g. `Dune {49522}.epub`).
6. Progress synced from your CrossPoint device will automatically resolve to Calibre and Kavita!

---

## CLI Commands

You can execute commands via `docker compose exec kosync-hub <command>`:

- **Check connectivity & VFS directory readiness**:
  ```bash
  docker compose exec kosync-hub kosync-hub test-connections
  ```
- **Trigger an immediate reading progress sync**:
  ```bash
  docker compose exec kosync-hub kosync-hub sync-now
  ```
- **Trigger an immediate Kavita VFS generation pass**:
  ```bash
  docker compose exec kosync-hub kosync-hub vfs-sync
  ```
- **Clean up unregistered links and orphaned files in VFS**:
  ```bash
  docker compose exec kosync-hub kosync-hub vfs-cleanup
  ```
- **View tracked books and reading progress**:
  ```bash
  docker compose exec kosync-hub kosync-hub status
  ```
- **Scan and index KOReader hashes across Calibre library**:
  ```bash
  docker compose exec kosync-hub kosync-hub scan-calibre
  ```

---

## Running Tests

All unit test suites can be executed with `pytest`:
```bash
source .venv/bin/activate
pytest -v
```
Currently covering 37 unit tests across VFS formatter, database caching, symlink/hardlink synchronization, reading progress fanout, Calibre database client, and configuration API.

