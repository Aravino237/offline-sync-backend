import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture()
def client():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.environ["SYNC_DB_PATH"] = path

    from app import db as db_module
    db_module.DB_PATH = path
    db_module._local.conn = None

    from app.main import app
    from fastapi.testclient import TestClient

    db_module.init_db()
    with TestClient(app) as c:
        yield c

    db_module._local.conn = None
    if os.path.exists(path):
        os.remove(path)
    for ext in ("-wal", "-shm"):
        if os.path.exists(path + ext):
            os.remove(path + ext)
