"""Configuration loader for kosync-hub supporting YAML and Environment variables."""

import os
from pathlib import Path
from typing import Optional
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
    # Server
    if host := os.getenv("KOSYNC_SERVER_HOST"):
        config.server.host = host
    if port := os.getenv("KOSYNC_SERVER_PORT"):
        config.server.port = int(port)
    if user := os.getenv("KOSYNC_AUTH_USER"):
        config.server.auth_username = user
    if pwd := os.getenv("KOSYNC_AUTH_PASSWORD"):
        config.server.auth_password = pwd

    # Kavita
    if kavita_url := (os.getenv("KAVITA_URL") or os.getenv("KOSYNC_KAVITA_URL")):
        config.kavita.base_url = kavita_url.rstrip("/")
    if kavita_key := (os.getenv("KAVITA_API_KEY") or os.getenv("KOSYNC_KAVITA_API_KEY")):
        config.kavita.api_key = kavita_key
    if kavita_enabled := os.getenv("KAVITA_ENABLED"):
        config.kavita.enabled = kavita_enabled.lower() in ("1", "true", "yes")

    # Calibre
    if cal_path := (os.getenv("CALIBRE_LIBRARY_PATH") or os.getenv("KOSYNC_CALIBRE_PATH") or os.getenv("CALIBRE_DIR")):
        config.calibre.library_path = cal_path
    if cal_enabled := os.getenv("CALIBRE_ENABLED"):
        config.calibre.enabled = cal_enabled.lower() in ("1", "true", "yes")
    if cal_pct := os.getenv("CALIBRE_READ_PCT_COLUMN"):
        config.calibre.read_pct_column = cal_pct
    if cal_status := os.getenv("CALIBRE_READ_STATUS_COLUMN"):
        config.calibre.read_status_column = cal_status

    # Sync
    if sync_enabled := os.getenv("SYNC_ENABLED"):
        config.sync.enabled = sync_enabled.lower() in ("1", "true", "yes")
    if interval := os.getenv("SYNC_INTERVAL_SECONDS"):
        config.sync.interval_seconds = int(interval)
    if strat := os.getenv("SYNC_CONFLICT_RESOLUTION"):
        config.sync.conflict_resolution = strat
    if scan := os.getenv("SYNC_SCAN_ON_STARTUP"):
        config.sync.scan_on_startup = scan.lower() in ("1", "true", "yes")

    # VFS
    if vfs_enabled := os.getenv("VFS_ENABLED"):
        config.vfs.enabled = vfs_enabled.lower() in ("1", "true", "yes")
    if vfs_dir := (os.getenv("VFS_DIR") or os.getenv("OUTPUT_DIR")):
        config.vfs.vfs_dir = vfs_dir
    if vfs_mode := os.getenv("VFS_MODE"):
        config.vfs.mode = vfs_mode.lower()
    if vfs_interval := (os.getenv("VFS_INTERVAL_SECONDS") or os.getenv("VFS_SYNC_INTERVAL") or os.getenv("SYNC_INTERVAL")):
        try:
            config.vfs.interval_seconds = int(vfs_interval)
        except ValueError:
            pass
    if rel_links := os.getenv("RELATIVE_LINKS"):
        config.vfs.relative_links = rel_links.lower() in ("1", "true", "yes")
    if def_lang := (os.getenv("DEFAULT_LANGUAGE") or os.getenv("VFS_DEFAULT_LANGUAGE")):
        config.vfs.default_language = def_lang
    if def_type := (os.getenv("DEFAULT_TYPE") or os.getenv("VFS_DEFAULT_TYPE")):
        config.vfs.default_type = def_type
    if tgt_calibre := (os.getenv("CALIBRE_TARGET_DIR") or os.getenv("SYMLINK_TARGET_PREFIX")):
        config.vfs.calibre_target_dir = tgt_calibre
    if vfs_db := (os.getenv("CACHE_DB_PATH") or os.getenv("VFS_DB_PATH")):
        config.vfs.db_path = vfs_db

    # Data dir
    if data_dir := (os.getenv("DATA_DIR") or os.getenv("KOSYNC_DATA_DIR")):
        config.data_dir = data_dir
    elif config.data_dir == "/app/data" and not Path("/app").is_dir():
        config.data_dir = "./data"

    return config
