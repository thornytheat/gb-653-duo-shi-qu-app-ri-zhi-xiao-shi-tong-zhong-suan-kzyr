import os
import sys
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

@pytest.fixture
def client(tmp_path):
    os.environ["DB_PATH"] = str(tmp_path / "test.db")
    os.environ["EVENT_DB_PATH"] = str(tmp_path / "events.db")
    from app.main import create_app
    with TestClient(create_app()) as value:
        yield value


@pytest.fixture
def event_store(tmp_path):
    from app.modules.m3_events.state import EventStore
    return EventStore(str(tmp_path / "m3-events.db"))
