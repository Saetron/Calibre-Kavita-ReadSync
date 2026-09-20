"""Configuration loader for kosync-hub supporting YAML and Environment variables."""

import os
from pathlib import Path
from typing import Any, Optional
import yaml
from pydantic import BaseModel, Field


class ServerSettings(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8080
    auth_username: Optional[str] = None
    auth_password: Optional[str] = None


class KavitaSettings(BaseModel):
    enabled: bool = True
    base_url: str = "http://localhost:5000"
    api_key: str = ""
    timeout: float = 10.0


class CalibreSettings(BaseModel):
    enabled: bool = True
    library_path: str = "/calibre/library"
    read_pct_column: str = "#read_pct"
    read_status_column: str = "#read_status"
    last_read_column: str = "#last_read"
    progress_column: str = "#koreader_progress"
    pages_column: str = "#pages"
    auto_create_columns: bool = True
    mark_read_threshold: float = 0.98


class SyncSettings(BaseModel):
    enabled: bool = True
    interval_seconds: int = 300
    conflict_resolution: str = "latest_timestamp"  # "latest_timestamp" or "highest_percentage"
    scan_on_startup: bool = True


class VFSSettings(BaseModel):
    enabled: bool = True
    vfs_dir: str = "/vfs"
    mode: str = "hardlink"  # Default: 'hardlink' (or 'symlink', 'fuse')
    interval_seconds: int = 60
    relative_links: bool = False
    default_language: str = "unknown"
    default_type: str = "Unknown"
    calibre_target_dir: str = ""
    db_path: str = ""
    custom_type_column: str = "type"
    custom_volume_column: str = "volume"
    custom_chapter_column: str = "chapter"


class AppConfig(BaseModel):
    server: ServerSettings = Field(default_factory=ServerSettings)
    kavita: KavitaSettings = Field(default_factory=KavitaSettings)
    calibre: CalibreSettings = Field(default_factory=CalibreSettings)
    sync: SyncSettings = Field(default_factory=SyncSettings)
    vfs: VFSSettings = Field(default_factory=VFSSettings)
    data_dir: str = "/app/data"


_active_config_path: Optional[Path] = None


def get_active_config_path() -> Path:
    global _active_config_path
    if _active_config_path and _active_config_path.is_file():
        return _active_config_path
    if Path("/app/config/config.yaml").is_file():
        return Path("/app/config/config.yaml")
    if Path("config.yaml").is_file():
        return Path("config.yaml")
    if Path("/app/config").is_dir():
        return Path("/app/config/config.yaml")
    return Path("config.yaml")


def set_active_config_path(p: Path):
    global _active_config_path
    _active_config_path = p


def save_config(config: AppConfig, config_path: Optional[str] = None) -> str:
    """Saves AppConfig to YAML file."""
    target_path = Path(config_path) if config_path else get_active_config_path()
    target_path.parent.mkdir(parents=True, exist_ok=True)
    data = config.model_dump()
    with open(target_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False)
    set_active_config_path(target_path)
    return str(target_path)


def load_config(config_path: Optional[str] = None) -> AppConfig:
    """
    Loads configuration from a YAML file if present,
    overriding with environment variables.
    """
    config_dict = {}

    # Check candidates for config file
    candidates = []
    if config_path:
        candidates.append(Path(config_path))
    candidates.extend([
        Path("config.yaml"),
        Path("config.yml"),
        Path("/app/config/config.yaml"),
        Path(os.path.expanduser("~/.config/kosync-hub/config.yaml")),
    ])

    for p in candidates:
        if p.is_file():
            try:
                with open(p, "r", encoding="utf-8") as f:
                    loaded = yaml.safe_load(f)
                    if isinstance(loaded, dict):
                        config_dict = loaded
                        set_active_config_path(p)
                break
            except Exception as e:
                print(f"Warning: Failed to load config from {p}: {e}")

    config = AppConfig(**config_dict)

    # Apply environment variable overrides
    def _should_apply(section: Optional[str], key: str, env_val: Optional[str], default_val: Any = None) -> bool:
        if env_val is None:
            return False
        if isinstance(env_val, str) and env_val.strip() == "":
            return False
        if not config_dict:
            return True
        file_val = config_dict.get(section, {}).get(key) if section else config_dict.get(key)
        if file_val is None or (isinstance(file_val, str) and file_val.strip() == ""):
            return True

        if os.getenv("KOSYNC_FORCE_ENV", "").lower() in ("1", "true", "yes"):
            return True

        norm_env = str(env_val).strip().rstrip("/")
        norm_default = str(default_val).strip().rstrip("/") if default_val is not None else None
        norm_file = str(file_val).strip().rstrip("/")

        # If the environment variable value matches the stock default boilerplate
        # (e.g. VFS_DIR='/vfs' or CALIBRE_LIBRARY_PATH='/calibre/library' from docker-compose / image)
        # and the file contains a configured value, do NOT clobber the user's config file setting.
        if norm_default is not None and norm_env.lower() == norm_default.lower():
            return False

        # If the config file already has a custom setting different from default, preserve it
        if norm_default is not None and norm_file.lower() != norm_default.lower():
            return False

        return True

    # Server
    if (host := os.getenv("KOSYNC_SERVER_HOST")) and _should_apply("server", "host", host, "0.0.0.0"):
        config.server.host = host
    if (port := os.getenv("KOSYNC_SERVER_PORT")) and _should_apply("server", "port", port, 8080):
        try:
            config.server.port = int(port)
        except ValueError:
            pass
    if (user := os.getenv("KOSYNC_AUTH_USER")) and _should_apply("server", "auth_username", user, ""):
        config.server.auth_username = user
    if (pwd := os.getenv("KOSYNC_AUTH_PASSWORD")) and _should_apply("server", "auth_password", pwd, ""):
        config.server.auth_password = pwd

    # Kavita
    if (kavita_url := (os.getenv("KAVITA_URL") or os.getenv("KOSYNC_KAVITA_URL"))) and _should_apply("kavita", "base_url", kavita_url, "http://localhost:5000"):
        config.kavita.base_url = kavita_url.rstrip("/")
    if (kavita_key := (os.getenv("KAVITA_API_KEY") or os.getenv("KOSYNC_KAVITA_API_KEY"))) and _should_apply("kavita", "api_key", kavita_key, ""):
        config.kavita.api_key = kavita_key
    if (kavita_enabled := os.getenv("KAVITA_ENABLED")) and _should_apply("kavita", "enabled", kavita_enabled, True):
        config.kavita.enabled = kavita_enabled.lower() in ("1", "true", "yes")

    # Calibre
    if (cal_path := (os.getenv("CALIBRE_LIBRARY_PATH") or os.getenv("KOSYNC_CALIBRE_PATH") or os.getenv("CALIBRE_DIR"))) and _should_apply("calibre", "library_path", cal_path, "/calibre/library"):
        config.calibre.library_path = cal_path
    if (cal_enabled := os.getenv("CALIBRE_ENABLED")) and _should_apply("calibre", "enabled", cal_enabled, True):
        config.calibre.enabled = cal_enabled.lower() in ("1", "true", "yes")
    if (cal_pct := os.getenv("CALIBRE_READ_PCT_COLUMN")) and _should_apply("calibre", "read_pct_column", cal_pct, "#read_pct"):
        config.calibre.read_pct_column = cal_pct
    if (cal_status := os.getenv("CALIBRE_READ_STATUS_COLUMN")) and _should_apply("calibre", "read_status_column", cal_status, "#read_status"):
        config.calibre.read_status_column = cal_status
    if (cal_pages := os.getenv("CALIBRE_PAGES_COLUMN")) and _should_apply("calibre", "pages_column", cal_pages, "#pages"):
        config.calibre.pages_column = cal_pages

    # Sync
    if (sync_enabled := os.getenv("SYNC_ENABLED")) and _should_apply("sync", "enabled", sync_enabled, True):
        config.sync.enabled = sync_enabled.lower() in ("1", "true", "yes")
    if (interval := os.getenv("SYNC_INTERVAL_SECONDS")) and _should_apply("sync", "interval_seconds", interval, 300):
        try:
            config.sync.interval_seconds = int(interval)
        except ValueError:
            pass
    if (strat := os.getenv("SYNC_CONFLICT_RESOLUTION")) and _should_apply("sync", "conflict_resolution", strat, "latest_timestamp"):
        config.sync.conflict_resolution = strat
    if (scan := os.getenv("SYNC_SCAN_ON_STARTUP")) and _should_apply("sync", "scan_on_startup", scan, True):
        config.sync.scan_on_startup = scan.lower() in ("1", "true", "yes")

    # VFS
    if (vfs_enabled := os.getenv("VFS_ENABLED")) and _should_apply("vfs", "enabled", vfs_enabled, True):
        config.vfs.enabled = vfs_enabled.lower() in ("1", "true", "yes")
    if (vfs_dir := (os.getenv("VFS_DIR") or os.getenv("OUTPUT_DIR"))) and _should_apply("vfs", "vfs_dir", vfs_dir, "/vfs"):
        config.vfs.vfs_dir = vfs_dir
    if (vfs_mode := os.getenv("VFS_MODE")) and _should_apply("vfs", "mode", vfs_mode, "hardlink"):
        config.vfs.mode = vfs_mode.lower()
    if (vfs_interval := (os.getenv("VFS_INTERVAL_SECONDS") or os.getenv("VFS_SYNC_INTERVAL") or os.getenv("SYNC_INTERVAL"))) and _should_apply("vfs", "interval_seconds", vfs_interval, 60):
        try:
            config.vfs.interval_seconds = int(vfs_interval)
        except ValueError:
            pass
    if (rel_links := os.getenv("RELATIVE_LINKS")) and _should_apply("vfs", "relative_links", rel_links, False):
        config.vfs.relative_links = rel_links.lower() in ("1", "true", "yes")
    if (def_lang := (os.getenv("DEFAULT_LANGUAGE") or os.getenv("VFS_DEFAULT_LANGUAGE"))) and _should_apply("vfs", "default_language", def_lang, "en"):
        config.vfs.default_language = def_lang
    if (def_type := (os.getenv("DEFAULT_TYPE") or os.getenv("VFS_DEFAULT_TYPE"))) and _should_apply("vfs", "default_type", def_type, "Book"):
        config.vfs.default_type = def_type
    if (tgt_calibre := (os.getenv("CALIBRE_TARGET_DIR") or os.getenv("SYMLINK_TARGET_PREFIX"))) and _should_apply("vfs", "calibre_target_dir", tgt_calibre, ""):
        config.vfs.calibre_target_dir = tgt_calibre
    if (vfs_db := (os.getenv("CACHE_DB_PATH") or os.getenv("VFS_DB_PATH"))) and _should_apply("vfs", "db_path", vfs_db, ""):
        config.vfs.db_path = vfs_db

    # Data dir
    if (data_dir := (os.getenv("DATA_DIR") or os.getenv("KOSYNC_DATA_DIR"))) and _should_apply(None, "data_dir", data_dir, "/app/data"):
        config.data_dir = data_dir
    elif config.data_dir == "/app/data" and not Path("/app").is_dir():
        config.data_dir = "./data"

    return config
