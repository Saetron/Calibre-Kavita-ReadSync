import os
import tempfile
from pathlib import Path
import pytest
import yaml
from httpx import ASGITransport, AsyncClient

from kosync_hub.config import AppConfig, load_config
from kosync_hub.db import InternalDatabase
from kosync_hub.server import create_app
from kosync_hub.synchronizer import Synchronizer
from kosync_hub.vfs.manager import VFSManager


@pytest.mark.asyncio
async def test_config_api_get_and_save():
    with tempfile.TemporaryDirectory() as tmpdir:
        config_path = os.path.join(tmpdir, "config.yaml")
        initial_yaml = {
            "server": {"host": "0.0.0.0", "port": 8080},
            "calibre": {"enabled": False, "library_path": "/calibre"},
            "kavita": {"enabled": False, "base_url": "http://localhost:5000", "api_key": "testkey"},
            "sync": {"enabled": True, "interval_seconds": 120},
            "vfs": {"enabled": True, "mode": "hardlink", "vfs_dir": "/vfs"},
            "data_dir": tmpdir,
        }
        with open(config_path, "w") as f:
            yaml.safe_dump(initial_yaml, f)

        config = load_config(config_path)
        db_path = os.path.join(tmpdir, "test.db")
        db = InternalDatabase(db_path)
        sync = Synchronizer(db=db)
        vfs = VFSManager(config)

        app = create_app(config=config, db=db, synchronizer=sync, vfs_manager=vfs)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # 1. GET /api/config
            res = await client.get("/api/config")
            assert res.status_code == 200
            resp_body = res.json()
            data = resp_body["config"]
            assert data["sync"]["enabled"] is True
            assert data["vfs"]["enabled"] is True
            assert data["vfs"]["mode"] == "hardlink"

            # 2. POST /api/config with updated settings
            new_payload = data.copy()
            new_payload["vfs"]["mode"] = "symlink"
            new_payload["sync"]["interval_seconds"] = 600

            save_res = await client.post("/api/config", json=new_payload)
            assert save_res.status_code == 200
            save_data = save_res.json()
            assert save_data["status"] == "success"

            # Verify in-memory config updated
            assert config.vfs.mode == "symlink"
            assert config.sync.interval_seconds == 600
            assert vfs.config.vfs.mode == "symlink"

            # Verify file on disk updated
            with open(config_path, "r") as f:
                saved_yaml = yaml.safe_load(f)
            assert saved_yaml["vfs"]["mode"] == "symlink"
            assert saved_yaml["sync"]["interval_seconds"] == 600


@pytest.mark.asyncio
async def test_vfs_endpoints():
    with tempfile.TemporaryDirectory() as tmpdir:
        config = AppConfig(
            data_dir=tmpdir,
            vfs={"enabled": True, "mode": "hardlink", "vfs_dir": os.path.join(tmpdir, "vfs")},
        )
        db_path = os.path.join(tmpdir, "test.db")
        db = InternalDatabase(db_path)
        sync = Synchronizer(db=db)
        vfs = VFSManager(config)

        app = create_app(config=config, db=db, synchronizer=sync, vfs_manager=vfs)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # GET /api/vfs/status
            res = await client.get("/api/vfs/status")
            assert res.status_code == 200
            status_data = res.json()
            assert "total_books" in status_data
            assert "mode" in status_data
            assert status_data["mode"] == "hardlink"

            # GET /api/vfs/books
            books_res = await client.get("/api/vfs/books")
            assert books_res.status_code == 200
            books_data = books_res.json()
            assert "items" in books_data
            assert books_data["total"] == 0

            # POST /api/vfs/sync (Calibre DB doesn't exist yet, should return error message gracefully)
            sync_res = await client.post("/api/vfs/sync")
            assert sync_res.status_code == 200
            assert sync_res.json()["status"] == "error"
