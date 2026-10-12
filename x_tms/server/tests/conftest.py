import pytest
from fastapi.testclient import TestClient
from tms.config import get_settings
from tms.db import reset_engine


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("TMS_DATABASE_URL", f"sqlite:///{tmp_path / 'tms.db'}")
    get_settings.cache_clear()
    reset_engine()
    yield
    reset_engine()
    get_settings.cache_clear()


@pytest.fixture
def client():
    from tms.api.app import create_app

    with TestClient(create_app()) as c:
        yield c


@pytest.fixture
def simulated(client):
    """Trois missions complètes jouées instantanément."""
    from tms.simulator.engine import run_simulation

    return run_simulation(["nominal", "deviation", "immobilization"], fast_forward=True, seed=42)
