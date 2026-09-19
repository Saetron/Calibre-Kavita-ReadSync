"""Data models for KOReader sync protocol, books, and hub configuration."""

from datetime import datetime
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class DocumentMetadata(BaseModel):
    title: Optional[str] = None
    authors: Optional[str] = None
    filename: Optional[str] = None


class ProgressPayload(BaseModel):
    """Payload received from KOReader when updating progress (PUT /syncs/progress)."""
    document: str
    progress: str
    percentage: float
    device: Optional[str] = "KOReader"
    device_id: Optional[str] = None
    metadata: Optional[DocumentMetadata] = None
    position: Optional[Dict[str, Any]] = None  # CrossPoint rich position extension


class ProgressRecord(BaseModel):
    """Normalized internal representation of reading progress."""
    document: str
    progress: str
    percentage: float
    timestamp: int = Field(default_factory=lambda: int(datetime.utcnow().timestamp()))
    device: Optional[str] = "KOReader"
    device_id: Optional[str] = None
    title: Optional[str] = None
    authors: Optional[str] = None
    filename: Optional[str] = None
    calibre_id: Optional[int] = None


class ProgressResponse(BaseModel):
    """Response returned to KOReader on GET /syncs/progress/:document."""
    document: str
    progress: str
    percentage: float
    timestamp: int
    device: Optional[str] = "KOReader"


class UserAuthRequest(BaseModel):
    username: str
    password: str


class SyncEvent(BaseModel):
    id: Optional[int] = None
    document: str
    calibre_id: Optional[int] = None
    source: str
    target: str
    progress: str
    percentage: float
    timestamp: int
    success: bool
    message: Optional[str] = None


class CalibreBookRecord(BaseModel):
    book_id: int
    title: str
    authors: str
    path: str = ""
    file_path: Optional[str] = None
    format: Optional[str] = None
    percentage: float = 0.0
    last_read: Optional[str] = None
    last_modified: Optional[str] = None
    koreader_progress: Optional[str] = None
    is_read: bool = False
    koreader_hash: Optional[str] = None


class KavitaRecentRead(BaseModel):
    series_id: int
    series_name: str
    chapter_id: Optional[int] = None
    filename: str
    calibre_id: Optional[int] = None
    pages_read: int = 0
    total_pages: int = 0
    percentage: float = 0.0
    last_read_utc: Optional[str] = None
    book_hash: Optional[str] = None


class HubStatus(BaseModel):
    uptime_seconds: float
    tracked_books_count: int
    kavita_connected: bool
    calibre_connected: bool
    last_sync_timestamp: Optional[int] = None
    last_sync_status: Optional[str] = None
    recent_sync_count: int = 0
