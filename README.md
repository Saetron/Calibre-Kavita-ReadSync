# 📖 Kavita & Calibre KOReader Progress Sync (`kosync-hub`)

A dedicated bidirectional synchronization daemon running in **Docker** that syncs reading progress between **Kavita** and **Calibre's database** (`metadata.db`).

---

## How It Works

* **KOReader connects directly to Kavita**: You do **not** need a proxy! Your KOReader devices continue using Kavita's built-in KOReader sync server (`http://<kavita-url>/api/koreader/<apiKey>`).
* **Deterministic Matching via `{id}`**: The filenames of books in Kavita contain the Calibre ID in curly brackets (e.g. `Dune {123}.epub` or `Foundation - Isaac Asimov {42}.epub`). `kosync-hub` uses regex `\{(\d+)\}` to extract the Calibre book ID and map it directly to Calibre's `books.id` with 100% accuracy.
* **Checks Recent Reads**:
  - **From Kavita**: The daemon checks Kavita's active reading progress (e.g. On-Deck items, continue-reading chapters, and Kavita's KOReader sync store).
  - **From Calibre**: The daemon checks recently read / modified books in Calibre.
* **Bidirectional Sync**:
  - If Kavita has newer or farther reading progress ➔ Writes progress to Calibre's custom columns in `metadata.db` (`#read_pct`, `#read_status`, `#last_read`, `#koreader_progress`).
  - If Calibre has newer progress ➔ Pushes the progress into Kavita's KOReader endpoint (`PUT /api/koreader/<apiKey>/syncs/progress`).

---

## Architecture

```
┌─────────────────┐
│ KOReader Device │ ─── (Direct sync via /api/koreader/<apiKey>) ───┐
└─────────────────┘                                                 │
                                                                    ▼
                                                         ┌─────────────────────┐
                                                         │    Kavita Server    │
                                                         │  - On-Deck / Reads  │
                                                         │  - KOReader store   │
                                                         └──────────┬──────────┘
                                                                    │
                                                        Bidirectional Sync Loop
                                                        (Matches via Filename {id})
                                                                    │
                                                                    ▼
                                                         ┌─────────────────────┐
                                                         │     kosync-hub      │
                                                         │   (Docker Daemon)   │
                                                         └──────────┬──────────┘
                                                                    │
                                                         Direct SQLite read/write
                                                                    │
                                                                    ▼
                                                         ┌─────────────────────┐
                                                         │  Calibre in Docker  │
                                                         │    (metadata.db)    │
                                                         └─────────────────────┘
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

kavita:
  enabled: true
  base_url: "http://192.168.1.100:5000"   # Your Kavita URL
  api_key: "YOUR_KAVITA_API_KEY"         # Kavita: User Settings -> API Keys

calibre:
  enabled: true
  library_path: "/calibre/library"       # Path to Calibre library containing metadata.db
  read_pct_column: "#read_pct"           # Stores float percentage (0.0 to 100.0%)
  read_status_column: "#read_status"     # Stores boolean (Yes/No) if read
  last_read_column: "#last_read"         # Stores timestamp of last reading activity
  progress_column: "#koreader_progress"  # Stores exact KOReader CFI position
  auto_create_columns: true              # Auto-creates custom columns if missing
  mark_read_threshold: 0.98              # Mark read when progress >= 98%

sync:
  interval_seconds: 300                  # Check recent reads every 5 minutes
  conflict_resolution: "latest_timestamp"
  scan_on_startup: true
```

### 2. Configure `docker-compose.yml`
Ensure the volume mounts your Calibre library directory where `metadata.db` is located:
```yaml
services:
  kosync-hub:
    build: .
    container_name: kosync-hub
    restart: unless-stopped
    ports:
      - "8080:8080"
    volumes:
      - kosync_data:/app/data
      # Mount your Calibre library volume (where metadata.db is located):
      - /path/to/calibre/library:/calibre/library:rw
      - ./config.yaml:/app/config/config.yaml:ro
    environment:
      - CALIBRE_LIBRARY_PATH=/calibre/library

volumes:
  kosync_data:
```

### 3. Start the Daemon
```bash
docker compose up -d --build
```

You can view the dashboard and sync history at `http://<server-ip>:8080`.

---

## Calibre Custom Columns

When `auto_create_columns: true` is enabled, `kosync-hub` automatically creates the following custom columns in your Calibre `metadata.db` if they do not exist:

| Lookup Name | Column Name | Type | Description |
| :--- | :--- | :--- | :--- |
| `#read_pct` | Read Progress (%) | Float | Progress percentage (`0.0` to `100.0%`) |
| `#read_status` | Read Status | Yes/No (Bool) | Read status (`True` when \(\ge 98\%\)) |
| `#last_read` | Last Read Date | Date | Timestamp of last read activity |
| `#koreader_progress` | KOReader Progress CFI | Long Text | Exact KOReader position / CFI string |

---

## KOReader Configuration

In KOReader:
1. Open top menu ➔ **Settings** (or **Tools**) ➔ **Progress Sync** (or **Plugins** ➔ **Progress Sync**).
2. Set **Custom sync server** to your Kavita KOReader endpoint:
   ```
   http://<YOUR_KAVITA_IP>:5000/api/koreader/<YOUR_API_KEY>
   ```
3. Set **Document matching method** to **Binary**.
4. Enable **Auto sync**.

You continue reading as normal. Whenever KOReader syncs with Kavita, `kosync-hub` checks recent reads and updates Calibre's database!

---

## CLI Commands

You can run commands directly or through `docker compose exec`:

* **Test connectivity to Kavita and Calibre DB**:
  ```bash
  docker compose exec kosync-hub kosync-hub test-connections -c /app/config/config.yaml
  ```
* **Trigger an immediate bidirectional sync**:
  ```bash
  docker compose exec kosync-hub kosync-hub sync-now -c /app/config/config.yaml
  ```
* **View tracked books and Calibre IDs**:
  ```bash
  docker compose exec kosync-hub kosync-hub status -c /app/config/config.yaml
  ```

---

## Running Tests

```bash
source .venv/bin/activate
pytest -v
```
