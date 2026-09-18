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
