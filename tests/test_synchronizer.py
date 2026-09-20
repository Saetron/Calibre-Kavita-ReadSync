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

