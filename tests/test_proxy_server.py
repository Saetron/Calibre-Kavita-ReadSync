import tempfile
from pathlib import Path
import pytest
from httpx import ASGITransport, AsyncClient

from kosync_hub.config import AppConfig
from kosync_hub.db import InternalDatabase
from kosync_hub.models import ProgressRecord
from kosync_hub.server import create_app
from kosync_hub.synchronizer import Synchronizer
from kosync_hub.clients.base import BaseSyncClient


class DummyClient(BaseSyncClient):
    def __init__(self, name: str):
        self._name = name
        self.pushed_records = []

    @property
    def name(self) -> str:
        return self._name

    async def test_connection(self) -> bool:
        return True

    async def get_progress(self, document: str):
        return None

    async def update_progress(self, record: ProgressRecord) -> bool:
        self.pushed_records.append(record)
        return True


@pytest.mark.asyncio
async def test_proxy_server_fanout():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        db = InternalDatabase(str(db_path))

        dummy_kavita = DummyClient("MockKavita")
        dummy_calibre = DummyClient("MockCalibre")

        config = AppConfig()
        sync = Synchronizer(db=db, kavita=dummy_kavita, calibre=dummy_calibre)
        app = create_app(config=config, db=db, synchronizer=sync)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # 1. Auth check
            res = await client.get("/users/auth", headers={"x-auth-user": "test", "x-auth-key": "md5"})
            assert res.status_code == 200

            # 2. Push progress from KOReader
            payload = {
                "document": "doc_hash_xyz",
                "progress": "/6/2[chap1]!/4",
                "percentage": 0.45,
                "device": "Kindle",
                "metadata": {
                    "title": "Project Hail Mary",
                    "authors": "Andy Weir",
                    "filename": "Project Hail Mary.epub",
                },
            }
            res = await client.put("/syncs/progress", json=payload)
            assert res.status_code == 200
            data = res.json()
            assert data["document"] == "doc_hash_xyz"

            # Verify fanout to both providers
            assert len(dummy_kavita.pushed_records) == 1
            assert dummy_kavita.pushed_records[0].document == "doc_hash_xyz"
            assert dummy_kavita.pushed_records[0].percentage == 0.45

            assert len(dummy_calibre.pushed_records) == 1
            assert dummy_calibre.pushed_records[0].document == "doc_hash_xyz"

            # Verify saved to internal database
            stored = db.get_document("doc_hash_xyz")
            assert stored is not None
            assert stored.title == "Project Hail Mary"

            # 3. Pull progress
            res = await client.get("/syncs/progress/doc_hash_xyz")
            assert res.status_code == 200
            assert res.json()["percentage"] == 0.45

            # 4. Healthcheck & status
            res = await client.get("/healthcheck")
            assert res.status_code == 200
            assert res.json()["tracked_documents"] == 1

            # 5. Dashboard test (verify no IndexError/KeyError on events)
            from kosync_hub.models import SyncEvent
            db.log_sync_event(
                SyncEvent(
                    document="doc_hash_xyz",
                    calibre_id=123,
                    source="kavita",
                    target="calibre",
                    progress="10/20",
                    percentage=0.5,
                    timestamp=1710000000,
                    success=True,
                    message="Synced successfully",
                )
            )
            res = await client.get("/")
            assert res.status_code == 200
            assert "Project Hail Mary" in res.text
            assert "#123" in res.text
            assert "Tracked Books" in res.text
            assert "KOReader &amp; CrossPoint Device Sync URL" in res.text or "KOReader & CrossPoint Device Sync URL" in res.text
            assert "Server URL:" in res.text
            assert "Backfill from Calibre" in res.text

            # 6. Test CrossPoint / Xteink X3 compressed ebook scenario
            # Device sends a compressed hash different from original, but includes {calibre_id} in filename
            # and sends CrossPoint position object.
            crosspoint_payload = {
                "document": "xteink_compressed_hash_49522",
                "progress": "epubcfi(/6/4[chapter-2]!/4/2/1:0)",
                "percentage": 0.82,
                "device": "CrossPoint",
                "device_id": "crosspoint-x3",
                "metadata": {
                    "title": "Dune",
                    "authors": "Frank Herbert",
                    "filename": "Dune {49522}.epub",
                },
                "position": {
                    "pctQ": 8200,
                    "spine": 2,
                    "page": 150,
                    "pages": 600,
                    "para": 12,
                },
            }
            res = await client.put("/syncs/progress", json=crosspoint_payload)
            assert res.status_code == 200

            # Verify Calibre ID was extracted and linked as an alias
            assert db.get_calibre_id_for_document("xteink_compressed_hash_49522") == 49522

            # Pull progress using the compressed document hash
            res = await client.get("/syncs/progress/xteink_compressed_hash_49522")
            assert res.status_code == 200
            assert res.json()["percentage"] == 0.82

            # Verify stats API
            stats_res = await client.get("/api/stats")
            assert stats_res.status_code == 200
            stats_data = stats_res.json()
            assert stats_data["total_tracked"] >= 2
            assert stats_data["in_progress"] >= 1
            assert stats_data["aliases_count"] >= 1

            # Test search pagination
            books_res = await client.get("/api/books?search=Dune")
            assert books_res.status_code == 200
            assert books_res.json()["total"] == 1
            assert books_res.json()["items"][0]["calibre_book_id"] == 49522

            # 7. Test backfill endpoint
            from kosync_hub.clients.calibre_db import CalibreBookRecord
            dummy_calibre.get_all_books_with_progress = lambda: [
                CalibreBookRecord(
                    book_id=9999,
                    title="Foundation",
                    authors="Isaac Asimov",
                    percentage=0.67,
                    is_read=False,
                    koreader_progress="page:67%",
                )
            ]
            backfill_res = await client.post("/api/backfill")
            assert backfill_res.status_code == 200
            assert backfill_res.json()["status"] == "completed"
            assert backfill_res.json()["backfilled_count"] == 1

            # Check that Foundation is now in tracked books
            found_res = await client.get("/api/books?search=Foundation")
            assert found_res.status_code == 200
            assert found_res.json()["total"] == 1
            assert found_res.json()["items"][0]["calibre_book_id"] == 9999

            # 8. Test repair of missing titles (e.g. from user screenshot #56905)
            # Insert a record with title=None
            db.upsert_progress(
                ProgressRecord(
                    document="hash_56905",
                    progress="100%",
                    percentage=1.0,
                    calibre_id=56905,
                    title=None,
                    authors=None,
                    device="Kavita",
                ),
                calibre_book_id=56905,
            )
            # Before repair, verify get_paginated_documents can fallback or repair can populate
            dummy_calibre.get_book_by_id = lambda bid: CalibreBookRecord(
                book_id=56905,
                title="Sleeping Little Sister",
                authors="Author Name",
                percentage=1.0,
            ) if bid == 56905 else None
            
            repaired_count = db.repair_missing_titles(dummy_calibre.get_book_by_id)
            assert repaired_count == 1

            # Verify in /api/books and dashboard
            res_repair = await client.get("/api/books?search=Sleeping")
            assert res_repair.status_code == 200
            assert res_repair.json()["total"] == 1
            assert res_repair.json()["items"][0]["title"] == "Sleeping Little Sister"
            assert res_repair.json()["items"][0]["authors"] == "Author Name"

            res_dash = await client.get("/")
            assert res_dash.status_code == 200
            assert "Sleeping Little Sister" in res_dash.text
            assert "#56905" in res_dash.text


@pytest.mark.asyncio
async def test_crosspoint_filename_sync():
    """Verifies CrossPoint sync using CHECKSUM_METHOD.FILENAME (no metadata, MD5 of filename)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        db_path = tmp_path / "internal.db"
        lib_dir = tmp_path / "calibre"
        lib_dir.mkdir()

        from tests.test_calibre_db import create_mock_calibre_db
        from kosync_hub.clients.calibre_db import CalibreDbClient
        from kosync_hub.hasher import compute_filename_md5

        create_mock_calibre_db(lib_dir)
        db = InternalDatabase(str(db_path))
        calibre_client = CalibreDbClient(library_path=str(lib_dir), auto_create_columns=True)
        await calibre_client.test_connection()

        dummy_kavita = DummyClient("MockKavita")
        config = AppConfig()
        sync = Synchronizer(db=db, kavita=dummy_kavita, calibre=calibre_client)
        app = create_app(config=config, db=db, synchronizer=sync)

        # CrossPoint computes MD5 of filename on device (e.g. "Dune (1).epub")
        fn_hash = compute_filename_md5("Dune (1).epub")

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # 1. Device syncs reading progress with NO metadata
            payload = {
                "document": fn_hash,
                "progress": "page:150/300",
                "percentage": 0.50,
                "device": "CrossPoint",
                "device_id": "xteink-x3",
            }
            res = await client.put("/syncs/progress", json=payload)
            assert res.status_code == 200

            # 2. Check that Calibre ID was resolved to 1
            rec = db.get_document(fn_hash)
            assert rec is not None
            assert rec.calibre_id == 1
            assert rec.title == "Dune"
            assert rec.authors == "Frank Herbert"

            # 3. Verify Calibre DB received the progress
            cal_b = calibre_client.get_book_by_id(1)
            assert cal_b is not None
            assert cal_b.percentage == 0.50

            # 4. Verify Kavita received push with canonical KOReader hash, NOT the filename hash
            assert len(dummy_kavita.pushed_records) == 1
            assert dummy_kavita.pushed_records[0].document == "dune_hash_12345"
            assert dummy_kavita.pushed_records[0].percentage == 0.50

            # 5. CrossPoint queries GET /syncs/progress/<fn_hash> and receives 200 OK
            get_res = await client.get(f"/syncs/progress/{fn_hash}")
            assert get_res.status_code == 200
            assert get_res.json()["percentage"] == 0.50
            assert get_res.json()["document"] == fn_hash

            # 6. Check audit log event recorded Calibre ID #1
            events = db.get_recent_events(limit=5)
            assert len(events) >= 1
            assert events[0]["calibre_id"] == 1
            assert "for #1" in events[0]["message"]

            # 7. Test user's exact case: High School DxD/High School DxD - 1 {56134}
            # The hash CrossPoint produced: 49e2f5c0f6f08f860a5268fa6b518623
            dxd_hash = "49e2f5c0f6f08f860a5268fa6b518623"
            dxd_payload = {
                "document": dxd_hash,
                "progress": "/6/4[chap1]!/4",
                "percentage": 0.42,
                "device": "CrossPoint",
                "device_id": "xteink-x3",
            }
            res_dxd = await client.put("/syncs/progress", json=dxd_payload)
            assert res_dxd.status_code == 200

            # Verify Calibre ID resolved to 56134
            dxd_rec = db.get_document(dxd_hash)
            assert dxd_rec is not None
            assert dxd_rec.calibre_id == 56134
            assert dxd_rec.title == "High School DxD, Vol. 1"
            assert dxd_rec.authors == "Ichiei Ishibumi"

            # Verify Calibre DB received the progress
            cal_dxd = calibre_client.get_book_by_id(56134)
            assert cal_dxd is not None
            assert cal_dxd.percentage == 0.42

            # Verify Kavita received canonical KOReader file hash, NOT 49e2f5c0...
            assert len(dummy_kavita.pushed_records) == 2
            assert dummy_kavita.pushed_records[1].document == "dxd_canonical_hash_9876"
            assert dummy_kavita.pushed_records[1].percentage == 0.42

            # Verify GET /syncs/progress/49e2f5c0... works
            get_dxd = await client.get(f"/syncs/progress/{dxd_hash}")
            assert get_dxd.status_code == 200
            assert get_dxd.json()["percentage"] == 0.42



