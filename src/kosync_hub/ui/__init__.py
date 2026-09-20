"""UI library package for KOReader Sync Hub web dashboard."""

from .dashboard import render_dashboard_html
from .helpers import enrich_tracked_documents

__all__ = ["render_dashboard_html", "enrich_tracked_documents"]
