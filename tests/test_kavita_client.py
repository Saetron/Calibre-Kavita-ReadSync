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

    def mock_handler(request: httpx.Request):
        url_str = str(request.url)
        if url_str.endswith("/users/auth"):
            return httpx.Response(200, json={"message": "Authorized"})
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
        elif "/api/series/on-deck" in url_str:
            return httpx.Response(200, json=[
                {"id": 10, "name": "The Hobbit Series"}
            ])
        elif "/api/series/volumes" in url_str and "seriesId=10" in url_str:
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
