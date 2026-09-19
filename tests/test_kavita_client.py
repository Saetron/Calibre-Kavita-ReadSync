import httpx
import pytest
from kosync_hub.clients.kavita import KavitaClient, extract_calibre_id
from kosync_hub.models import ProgressRecord


def test_extract_calibre_id():
    assert extract_calibre_id("Dune {1}.epub") == 1
    assert extract_calibre_id("The Hobbit - J.R.R. Tolkien {42}.epub") == 42
    assert extract_calibre_id("/calibre/library/Asimov/Foundation {105}/Foundation {105}.epub") == 105
    assert extract_calibre_id("Book Title Without Id.epub") is None
    assert extract_calibre_id(None) is None


@pytest.mark.asyncio
async def test_kavita_client_koreader_and_ondeck(monkeypatch):
    kavita = KavitaClient(base_url="http://kavita.test:5000", api_key="secret_token")

    calls = []
    def mock_handler(request: httpx.Request):
        url_str = str(request.url).lower()
        calls.append((request.method, url_str, request.content))
        if url_str.endswith("/users/auth"):
            return httpx.Response(200, json={"message": "Authorized"})
        elif url_str.endswith("/api/plugin/authenticate") or "/api/plugin/authenticate" in url_str:
            return httpx.Response(200, json={"token": "mock_jwt_token", "username": "testuser"})
        elif "/syncs/progress/dune_hash" in url_str and request.method == "GET":
            return httpx.Response(200, json={
                "document": "dune_hash",
                "progress": "/6/2!/4",
                "percentage": 0.42,
                "timestamp": 1710000000,
                "device": "KOReader",
            })
        elif url_str.endswith("/syncs/progress") and request.method == "PUT":
            return httpx.Response(200, json={"message": "Success"})
        elif "/api/search/search" in url_str:
            return httpx.Response(200, json={
                "files": [{"id": 1, "filePath": "/books/Hobbit {42}.epub", "pages": 300}],
                "chapters": [],
                "series": [],
            })
        elif "/api/search/series-for-mangafile" in url_str:
            return httpx.Response(200, json={"id": 10, "name": "The Hobbit Series", "libraryId": 1})
        elif "/api/reader/mark-chapter-read" in url_str:
            return httpx.Response(200, json={"message": "Marked chapter read"})
        elif "/api/reader/mark-read" in url_str:
            raise AssertionError("mark-read on entire series should not be called!")
        elif "/api/reader/progress" in url_str:
            return httpx.Response(200, json={"message": "Progress saved"})
        elif "/api/series/on-deck" in url_str:
            return httpx.Response(200, json=[
                {"id": 10, "name": "The Hobbit Series"}
            ])
        elif "/api/series/volumes" in url_str and "seriesid=10" in url_str:
            return httpx.Response(200, json=[
                {
                    "id": 100,
                    "chapters": [
                        {
                            "id": 1001,
                            "title": "Chapter 1",
                            "fileName": "The Hobbit {42}.epub",
                            "pages": 300,
                            "pagesRead": 150,
                        }
                    ]
                }
            ])
        return httpx.Response(404)

    transport = httpx.MockTransport(mock_handler)
    original_init = httpx.AsyncClient.__init__

    def mock_init(self, *args, **kwargs):
        kwargs["transport"] = transport
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", mock_init)

    # 1. Test connection
    assert await kavita.test_connection() is True

    # 2. Test get progress
    record = await kavita.get_progress("dune_hash")
    assert record is not None
    assert record.document == "dune_hash"
    assert record.percentage == 0.42

    # 3. Test update progress
    ok = await kavita.update_progress(
        ProgressRecord(
            document="dune_hash",
            progress="/6/4!/4",
            percentage=0.60,
        )
    )
    assert ok is True

    # 4. Test on-deck recent reads with Calibre ID extraction
    recent_reads = await kavita.get_on_deck_reads()
    assert len(recent_reads) == 1
    assert recent_reads[0].calibre_id == 42
    assert recent_reads[0].percentage == 0.5  # 150 / 300
    assert recent_reads[0].filename == "The Hobbit {42}.epub"

    # 5. Test WebUI progress updates (100% -> mark-chapter-read, 50% -> progress)
    webui_ok_100 = await kavita.update_webui_progress(calibre_id=42, percentage=1.0, title="The Hobbit")
    assert webui_ok_100 is True

    import json
    # Verify mark-chapter-read was called and NOT mark-read
    chapter_read_calls = [c for c in calls if "mark-chapter-read" in c[1]]
    assert len(chapter_read_calls) == 1
    body = json.loads(chapter_read_calls[0][2].decode("utf-8"))
    assert body["seriesId"] == 10
    assert body["chapterId"] == 1001

    series_read_calls = [c for c in calls if "mark-read" in c[1] and "mark-chapter-read" not in c[1]]
    assert len(series_read_calls) == 0

    webui_ok_50 = await kavita.update_webui_progress(calibre_id=42, percentage=0.5, title="The Hobbit")
    assert webui_ok_50 is True



@pytest.mark.asyncio
async def test_kavita_opds_fallback(monkeypatch):
    kavita = KavitaClient(base_url="http://kavita.test:5000", api_key="secret_token")

    opds_xml = """<?xml version="1.0" encoding="utf-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <title>On Deck</title>
      <entry>
        <title>Neuromancer {77}</title>
        <link href="/api/reader/77/file" type="application/epub+zip" title="Neuromancer {77}.epub" />
      </entry>
    </feed>
    """

    def mock_handler(request: httpx.Request):
        url_str = str(request.url).lower()
        if "/api/plugin/authenticate" in url_str:
            return httpx.Response(200, json={"token": "jwt123"})
        if "/api/opds/secret_token/on-deck" in url_str:
            return httpx.Response(200, text=opds_xml)
        return httpx.Response(404)

    transport = httpx.MockTransport(mock_handler)
    original_init = httpx.AsyncClient.__init__

    def mock_init(self, *args, **kwargs):
        kwargs["transport"] = transport
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", mock_init)

    reads = await kavita.get_on_deck_reads()
    assert len(reads) == 1
    assert reads[0].calibre_id == 77
    assert "Neuromancer" in reads[0].series_name


@pytest.mark.asyncio
async def test_kavita_db_mapping_cache(tmp_path):
    from kosync_hub.db import InternalDatabase
    db = InternalDatabase(str(tmp_path / "cache_test.db"))

    # Pre-populate DB cache
    db.save_mapping(
        calibre_id=999,
        kavita_series_id=888,
        kavita_series_name="Cached Series",
        pages=450,
    )

    kavita = KavitaClient(base_url="http://kavita.test:5000", api_key="secret_token", db=db)
    meta = await kavita.find_kavita_metadata_by_calibre_id(999)
    assert meta is not None
    assert meta["series_id"] == 888
    assert meta["series_name"] == "Cached Series"
    assert meta["pages"] == 450


@pytest.mark.asyncio
async def test_kavita_id_changed_after_book_update(tmp_path, monkeypatch):
    """
    Tests that if a book was updated in Kavita and its old chapter ID returns 404,
    the client purges the stale cache, re-queries Kavita, gets the new ID, and succeeds.
    """
    from kosync_hub.db import InternalDatabase
    db = InternalDatabase(str(tmp_path / "stale_test.db"))

    # Populate stale mapping (old chapter ID: 101, old series ID: 10)
    db.save_mapping(
        calibre_id=500,
        kavita_series_id=10,
        kavita_volume_id=20,
        kavita_chapter_id=101,
        pages=200,
    )

    kavita = KavitaClient(base_url="http://kavita.test:5000", api_key="secret_token", db=db)

    def mock_handler(request: httpx.Request):
        url_str = str(request.url).lower()
        if "/api/plugin/authenticate" in url_str:
            return httpx.Response(200, json={"token": "jwt123"})
        # Old chapter ID 101 returns 404 (deleted/updated in Kavita)
        if "/api/reader/mark-chapter-read" in url_str:
            import json
            body = json.loads(request.content.decode("utf-8"))
            if body.get("chapterId") == 101:
                return httpx.Response(404, json={"message": "Chapter not found"})
            elif body.get("chapterId") == 202:
                # New chapter ID 202 succeeds!
                return httpx.Response(200, json={"message": "Marked read"})
        # Re-discovery search query
        if "/api/search/search" in url_str:
            return httpx.Response(200, json={
                "files": [{"id": 5, "filePath": "/books/Updated Book {500}.epub", "pages": 250}],
                "chapters": [],
                "series": [],
            })
        if "/api/search/series-for-mangafile" in url_str:
            return httpx.Response(200, json={"id": 10, "name": "Updated Book Series", "libraryId": 1})
        if "/api/series/volumes" in url_str:
            return httpx.Response(200, json=[
                {
                    "id": 20,
                    "chapters": [
                        {
                            "id": 202,  # New chapter ID
                            "title": "Updated Chapter",
                            "files": [{"id": 5, "filePath": "/books/Updated Book {500}.epub"}],
                            "pages": 250,
                        }
                    ]
                }
            ])
        return httpx.Response(404)

    transport = httpx.MockTransport(mock_handler)
    original_init = httpx.AsyncClient.__init__

    def mock_init(self, *args, **kwargs):
        kwargs["transport"] = transport
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", mock_init)

    # Calling update_webui_progress will hit 404 on chapter 101, re-discover chapter 202, and succeed
    ok = await kavita.update_webui_progress(calibre_id=500, percentage=1.0, title="Updated Book")
    assert ok is True

    # Verify that the DB now contains the new chapter ID 202
    updated_mapping = db.get_mapping_by_calibre_id(500)
    assert updated_mapping is not None
    assert updated_mapping["kavita_chapter_id"] == 202

