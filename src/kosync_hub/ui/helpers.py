"""Helper functions for the KOReader Sync Hub web dashboard."""

from typing import Any, Dict, List


def enrich_tracked_documents(
    db: Any, synchronizer: Any, tracked_docs: List[Any]
) -> List[Dict[str, Any]]:
    """Enriches tracked documents with Calibre book IDs and metadata if missing."""
    enriched = []
    for d in tracked_docs:
        d_dict = dict(d)
        t_cal_id = d_dict.get("calibre_book_id")
        t_doc = str(d_dict.get("document", ""))
        t_device = d_dict.get("device") or "KOReader"
        t_title = d_dict.get("title") or "Unknown"
        t_authors = d_dict.get("authors") or ""

        if not t_cal_id:
            t_cal_id = db.get_calibre_id_for_document(t_doc) or db.get_calibre_id_by_filename_hash(t_doc)
            if not t_cal_id and synchronizer.calibre and hasattr(synchronizer.calibre, "find_book_by_filename_hash"):
                t_cal_id = synchronizer.calibre.find_book_by_filename_hash(t_doc)
            if t_cal_id:
                db.link_document_alias(t_doc, t_cal_id, t_device)
                with db._get_connection() as c:
                    c.execute("UPDATE tracked_documents SET calibre_book_id = ? WHERE document = ?", (t_cal_id, t_doc))
                    c.commit()
                d_dict["calibre_book_id"] = t_cal_id

        if (not t_title or t_title == "Unknown") and t_cal_id and synchronizer.calibre and hasattr(synchronizer.calibre, "get_book_by_id"):
            cal_b = synchronizer.calibre.get_book_by_id(t_cal_id)
            if cal_b and cal_b.title:
                t_title = cal_b.title
                t_authors = t_authors or cal_b.authors
                db.update_document_metadata(t_doc, t_title, t_authors)
                d_dict["title"] = t_title
                d_dict["authors"] = t_authors

        enriched.append(d_dict)
    return enriched
