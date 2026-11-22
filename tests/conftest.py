import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """每个用例一个全新数据库文件(含种子底表)。"""
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "test.db"))
    db.init_db()
    with TestClient(app) as c:
        yield c
