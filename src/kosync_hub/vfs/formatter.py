"""Path and filename formatting utilities for Kavita VFS.

Rules:
- Target structure: language/type/series/series Vol. volume Ch. chapter {calibre_id}.ext
- Only write 'Vol. volume' or 'Ch. chapter' if they are set in Calibre.
- Numbers are formatted cleanly (e.g. 1.0 -> 1, 1.5 -> 1.5).
"""

from __future__ import annotations

import re
from typing import Any, Optional


_INVALID_CHARS_RE = re.compile(r'[\\/:*?"<>|]')


def sanitize_filename_component(name: str, replacement: str = "_") -> str:
    """Sanitize a directory or filename component to remove filesystem-illegal characters."""
    if not name or not name.strip():
        return ""
    # Strip illegal characters
    cleaned = _INVALID_CHARS_RE.sub(replacement, name).strip()
    # Avoid trailing dots or spaces on Windows/network shares
    cleaned = cleaned.rstrip(". ")
    return cleaned


def format_number_or_str(val: Any) -> Optional[str]:
    """Format volume or chapter value.

    If float represents an integer (e.g. 1.0), format as "1".
    If float has decimal (e.g. 1.5), format as "1.5".
    If string or int, convert cleanly.
    Returns None if value is None or empty.
    """
    if val is None:
        return None

    if isinstance(val, (int,)):
        return str(val)

    if isinstance(val, float):
        if val.is_integer():
            return str(int(val))
        return f"{val:g}"

    s = str(val).strip()
    if not s:
        return None

    # Try parsing as float to clean up values like "1.0" or "01.0" if applicable
    try:
        f = float(s)
        if f.is_integer():
            if "." in s:
                return str(int(f))
            return s
        return f"{f:g}"
    except ValueError:
        return s


_VOL_PREFIX_RE = re.compile(r"^(?:vol(?:ume)?\.?\s*|v\.\s*)", re.IGNORECASE)
_CH_PREFIX_RE = re.compile(r"^(?:ch(?:apter)?\.?\s*|c\.\s*)", re.IGNORECASE)


def clean_vol_string(val: Any) -> Optional[str]:
    """Clean volume string, handling ranges (e.g. '1-3') and stripping redundant 'Vol.' prefix."""
    s = format_number_or_str(val)
    if s is None:
        return None
    cleaned = _VOL_PREFIX_RE.sub("", s).strip()
    return cleaned if cleaned else None


def clean_ch_string(val: Any) -> Optional[str]:
    """Clean chapter string, handling ranges (e.g. '1-5') and stripping redundant 'Ch.' prefix."""
    s = format_number_or_str(val)
    if s is None:
        return None
    cleaned = _CH_PREFIX_RE.sub("", s).strip()
    return cleaned if cleaned else None


def build_kavita_filename(
    series: str,
    volume: Any = None,
    chapter: Any = None,
    calibre_id: Optional[Any] = None,
    extension: str = "",
    title: Optional[str] = None,
) -> str:
    """Build filename according to:

    {series}[ Vol. {volume}][ Ch. {chapter}][ {calibre_id}].{ext}

    Only include 'Vol. {volume}' if volume is set.
    Only include 'Ch. {chapter}' if chapter is set.
    Includes '{calibre_id}' in curly brackets before extension if provided.
    Supports string ranges (e.g. volume='1-3' -> 'Vol. 1-3').
    """
    parts = []
    series_clean = sanitize_filename_component(series)
    parts.append(series_clean)

    vol_str = clean_vol_string(volume)
    if vol_str is not None:
        parts.append(f"Vol. {sanitize_filename_component(vol_str)}")

    ch_str = clean_ch_string(chapter)
    if ch_str is not None:
        parts.append(f"Ch. {sanitize_filename_component(ch_str)}")

    if calibre_id is not None and str(calibre_id).strip():
        parts.append(f"{{{str(calibre_id).strip()}}}")

    base_name = " ".join(parts)

    ext = extension.lstrip(".")
    if ext:
        return f"{base_name}.{ext}"
    return base_name


def build_vfs_relpath(
    language: Optional[str],
    type_: Optional[str],
    series: Optional[str],
    volume: Any = None,
    chapter: Any = None,
    calibre_id: Optional[Any] = None,
    extension: str = "",
    title: Optional[str] = None,
    default_language: str = "unknown",
    default_type: str = "Unknown",
) -> str:
    """Build relative path: language/type/series/series Vol. volume Ch. chapter {id}.ext

    Missing values will fall back to specified defaults.
    If series is missing, title is used as series folder/name.
    """
    lang_clean = sanitize_filename_component(
        (language or default_language).strip() or default_language
    )
    type_clean = sanitize_filename_component(
        (type_ or default_type).strip() or default_type
    )

    series_val = (series or "").strip()
    if not series_val:
        series_val = (title or "Unknown").strip() or "Unknown"

    series_folder = sanitize_filename_component(series_val)
    filename = build_kavita_filename(
        series=series_val,
        volume=volume,
        chapter=chapter,
        calibre_id=calibre_id,
        extension=extension,
        title=title,
    )

    return f"{lang_clean}/{type_clean}/{series_folder}/{filename}"
