import sqlite3
import tempfile
from pathlib import Path
import pytest

from kosync_hub.clients.calibre_db import CalibreDbClient
from kosync_hub.clients.kavita import KavitaClient
from kosync_hub.db import InternalDatabase
from kosync_hub.models import KavitaRecentRead, ProgressRecord
from kosync_hub.synchronizer import Synchronizer
from tests.test_calibre_db import create_mock_calibre_db


class MockKavitaClient(KavitaClient):
    def __init__(self):
        super().__init__(base_url="http://mock.kavita", api_key="dummy_token")
        self.koreader_store = {}
        self.on_deck_items = []
        self.pushed_records = []

    async def test_connection(self) -> bool:
        return True

    async def get_progress(self, document: str):
        return self.koreader_store.get(document)

    async def update_progress(self, record: ProgressRecord) -> bool:
        self.koreader_store[record.document] = record
        self.pushed_records.append(record)
        return True

    async def get_on_deck_reads(self, limit: int = 30):
        return self.on_deck_items


@pytest.mark.asyncio
async def test_bidirectional_sync_with_calibre_id():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        db_file = tmp_path / "kosync_hub.sqlite3"
        internal_db = InternalDatabase(str(db_file))

        calibre_dir = tmp_path / "calibre"
        calibre_dir.mkdir()
        create_mock_calibre_db(calibre_dir)

        calibre = CalibreDbClient(library_path=str(calibre_dir))
        await calibre.test_connection()

        mock_kavita = MockKavitaClient()

        sync = Synchronizer(
            db=internal_db,
            kavita=mock_kavita,
            calibre=calibre,
            interval_seconds=60,
        )

        # -------------------------------------------------------------
        # Scenario 1: User reads Book 2 ("The Hobbit", ID 2) on Kavita / KOReader
        # Filename in Kavita has {2}
        # -------------------------------------------------------------
        mock_kavita.on_deck_items = [
            KavitaRecentRead(
                series_id=1,
                series_name="The Hobbit Series",
                filename="The Hobbit {2}.epub",
                calibre_id=2,
                pages_read=150,
                total_pages=300,
                percentage=0.50,
            )
        ]

        res1 = await sync.sync_all()
        assert res1["updated_calibre"] == 1

        # Verify Calibre DB was updated for book 2 to 50%
        book2 = calibre.get_book_by_id(2)
        assert book2 is not None
        assert abs(book2.percentage - 0.50) < 0.01

        # -------------------------------------------------------------
        # Scenario 2: User reads Book 1 ("Dune", ID 1) in Calibre to 75%
        # -------------------------------------------------------------
        calibre.update_book_progress(book_id=1, percentage=0.75, progress_str="page:75%")

        res2 = await sync.sync_all()
        assert res2["updated_kavita"] == 1

        # Verify Kavita received progress for Dune (hash: 'dune_hash_12345')
        assert "dune_hash_12345" in mock_kavita.koreader_store
        assert mock_kavita.koreader_store["dune_hash_12345"].percentage == 0.75

        # -------------------------------------------------------------
        # Scenario 3: Device synced Book 55746 ("5 Centimeters Per Second")
        # to Hub tracked_documents (2%). Verify sync_all pushes to both Calibre and Kavita!
        # -------------------------------------------------------------
        with internal_db._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO tracked_documents (document, device, percentage, progress, timestamp, calibre_book_id, title)
                VALUES ('b77348322aece846b1c2a623ce97d4e4', 'CrossPoint', 0.02, 'page:2%', 1726794500, 55746, '5 Centimeters Per Second')
                """
            )
            conn.commit()

        res3 = await sync.sync_all()
        assert res3["updated_hub"] >= 1

        # Check Calibre DB updated to 2% (0.02)
        book_55746 = calibre.get_book_by_id(55746)
        assert book_55746 is not None
        assert abs(book_55746.percentage - 0.02) < 0.001

        # Check Kavita received progress
        assert any(r.calibre_id == 55746 and abs(r.percentage - 0.02) < 0.001 for r in mock_kavita.pushed_records)


@pytest.mark.asyncio
async def test_star_topology_isolated_phases():
    """Tests the distinct phases of the Star Topology synchronizer in isolation."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        db_file = tmp_path / "kosync_hub.sqlite3"
        internal_db = InternalDatabase(str(db_file))

        calibre_dir = tmp_path / "calibre"
        calibre_dir.mkdir()
        create_mock_calibre_db(calibre_dir)

        calibre = CalibreDbClient(library_path=str(calibre_dir))
        await calibre.test_connection()
        mock_kavita = MockKavitaClient()

        sync = Synchronizer(
            db=internal_db,
            kavita=mock_kavita,
            calibre=calibre,
        )

        # 1. Ingress Phase: Calibre -> Hub
        calibre.update_book_progress(book_id=1, percentage=0.85, progress_str="page:85%")
        pulled_cal = await sync.pull_calibre_to_hub()
        assert pulled_cal == 1
        hub_book1 = internal_db.get_tracked_document_by_calibre_id(1)
        assert hub_book1 is not None
        assert abs(float(hub_book1["percentage"]) - 0.85) < 0.01

        # 2. Ingress Phase: Kavita -> Hub
        mock_kavita.on_deck_items = [
            KavitaRecentRead(
                series_id=1,
                series_name="The Hobbit Series",
                filename="The Hobbit {2}.epub",
                calibre_id=2,
                pages_read=180,
                total_pages=300,
                percentage=0.60,
            )
        ]
        pulled_kav = await sync.pull_kavita_to_hub()
        assert pulled_kav == 1
        hub_book2 = internal_db.get_tracked_document_by_calibre_id(2)
        assert hub_book2 is not None
        assert abs(float(hub_book2["percentage"]) - 0.60) < 0.01

        # 3. Reconciliation Phase: reconcile_hub()
        # Seed an unlinked device document hash for book 56134
        with internal_db._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO tracked_documents (document, device, percentage, progress, timestamp, calibre_book_id, title)
                VALUES ('dxd_canonical_hash_9876', 'CrossPoint', 0.40, 'page:40%', 1726795000, NULL, NULL)
                """
            )
            conn.commit()
        reconciled = await sync.reconcile_hub()
        assert reconciled >= 1
        hub_dxd = internal_db.get_tracked_document_by_calibre_id(56134)
        assert hub_dxd is not None
        assert hub_dxd["title"] == "High School DxD, Vol. 1"

        # 4. Egress Phase: Hub -> Calibre
        # Calibre currently has Book 2 at 0% and Book 56134 at 0%
        pushed_cal = await sync.push_hub_to_calibre()
        assert pushed_cal >= 2
        cal_book2 = calibre.get_book_by_id(2)
        assert abs(cal_book2.percentage - 0.60) < 0.01
        cal_dxd = calibre.get_book_by_id(56134)
        assert abs(cal_dxd.percentage - 0.40) < 0.01

        # 5. Egress Phase: Hub -> Kavita
        # Kavita has not yet received Book 1 (85%) or Book 56134 (40%)
        pushed_kav = await sync.push_hub_to_kavita()
        assert pushed_kav >= 2
        assert any(r.calibre_id == 1 and abs(r.percentage - 0.85) < 0.01 for r in mock_kavita.pushed_records)
        assert any(r.calibre_id == 56134 and abs(r.percentage - 0.40) < 0.01 for r in mock_kavita.pushed_records)


@pytest.mark.asyncio
async def test_crosspoint_unlinked_hash_full_star_sync():
    """
    Simulates the exact user scenario:
    1. CrossPoint device pushes a filename MD5 hash ('b77348322aece846b1c2a623ce97d4e4')
       for '5 Centimeters Per Second + Children Who Chase Lost Voices' with NULL calibre_book_id at 2%.
    2. Star topology sync_all runs:
       - Phase 1: Ingress
       - Phase 2: Hub Reconciliation links the hash to Calibre ID 55746 via sanitized filename hash lookup
       - Phase 3: Hub Egress pushes 2% progress to Calibre DB and to Kavita!
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        db_file = tmp_path / "kosync_hub.sqlite3"
        internal_db = InternalDatabase(str(db_file))

        calibre_dir = tmp_path / "calibre"
        calibre_dir.mkdir()
        create_mock_calibre_db(calibre_dir)

        calibre = CalibreDbClient(library_path=str(calibre_dir))
        await calibre.test_connection()
        mock_kavita = MockKavitaClient()

        sync = Synchronizer(
            db=internal_db,
            kavita=mock_kavita,
            calibre=calibre,
        )

        # Unlinked entry in tracked_documents from CrossPoint (NULL calibre_book_id, unknown title)
        device_hash = "b77348322aece846b1c2a623ce97d4e4"
        with internal_db._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO tracked_documents (document, device, percentage, progress, timestamp, calibre_book_id, title)
                VALUES (?, 'CrossPoint', 0.02, 'page:2%', 1726796000, NULL, 'Unknown')
                """,
                (device_hash,),
            )
            conn.commit()

        # Run complete Star sync
        res = await sync.sync_all()
        assert res["status"] == "success"
        assert res["reconciled"] >= 1
        assert res["pushed_calibre"] >= 1
        assert res["pushed_kavita"] >= 1

        # Check Hub has the reconciled Calibre ID and proper title
        doc = internal_db.get_tracked_document_by_calibre_id(55746)
        assert doc is not None
        assert doc["calibre_book_id"] == 55746
        assert "5 Centimeters" in doc["title"]
        assert abs(float(doc["percentage"]) - 0.02) < 0.001

        # Check Calibre DB updated to 2%
        cal_book = calibre.get_book_by_id(55746)
        assert cal_book is not None
        assert abs(cal_book.percentage - 0.02) < 0.001

        # Check Kavita received the 2% progress
        assert any(r.calibre_id == 55746 and abs(r.percentage - 0.02) < 0.001 for r in mock_kavita.pushed_records)


@pytest.mark.asyncio
async def test_pull_kavita_to_hub_with_existing_tracked_document():
    """Verify pull_kavita_to_hub does not crash with AttributeError on sqlite3.Row when a book is already tracked."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        db_file = tmp_path / "kosync_hub.sqlite3"
        internal_db = InternalDatabase(str(db_file))

        calibre_dir = tmp_path / "calibre"
        calibre_dir.mkdir()
        create_mock_calibre_db(calibre_dir)

        calibre = CalibreDbClient(library_path=str(calibre_dir))
        await calibre.test_connection()

        # Pre-insert existing tracked document for book 2 ("The Hobbit")
        with internal_db._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO tracked_documents (
                    document, device, percentage, progress, timestamp, calibre_book_id, title, authors
                ) VALUES (
                    'hobbit_existing_hash', 'KOReader', 0.20, 'page:20%', 1726790000, 2, 'The Hobbit', 'J.R.R. Tolkien'
                )
                """
            )
            conn.commit()

        # Verify get_tracked_document_by_calibre_id returns an sqlite3.Row
        existing = internal_db.get_tracked_document_by_calibre_id(2)
        assert isinstance(existing, sqlite3.Row)

        mock_kavita = MockKavitaClient()
        mock_kavita.on_deck_items = [
            KavitaRecentRead(
                series_id=1,
                series_name="The Hobbit Series",
                filename="The Hobbit {2}.epub",
                calibre_id=2,
                pages_read=180,
                total_pages=300,
                percentage=0.60,
            )
        ]

        sync = Synchronizer(
            db=internal_db,
            kavita=mock_kavita,
            calibre=calibre,
            interval_seconds=60,
        )

        # Pull from Kavita into Hub — must NOT raise AttributeError: 'sqlite3.Row' object has no attribute 'get'
        pulled = await sync.pull_kavita_to_hub()
        assert pulled == 1

        updated = internal_db.get_tracked_document_by_calibre_id(2)
        assert updated is not None
        assert abs(float(updated["percentage"]) - 0.60) < 0.01
        assert "The Hobbit" in updated["title"]




