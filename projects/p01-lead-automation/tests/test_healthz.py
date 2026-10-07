import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import Engine, text

from sales_ops.main import app


def test_test_database_is_real_postgresql(db_engine: Engine) -> None:
    # AC-0.2: the suite must run against PostgreSQL, not SQLite or a mock.
    with db_engine.connect() as conn:
        version: str = conn.execute(text("SELECT version()")).scalar_one()
    assert version.startswith("PostgreSQL ")


def test_healthz_reports_db_ok(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "db": "ok"}


def test_healthz_returns_503_when_db_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    # Nothing listens on port 1, so the connection is refused immediately.
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://nobody@127.0.0.1:1/none")
    with TestClient(app) as test_client:
        response = test_client.get("/healthz")
    assert response.status_code == 503
    assert response.json() == {"status": "error", "db": "unreachable"}


def test_startup_fails_without_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    # A deployment that forgets DATABASE_URL must fail at startup, not on the first request.
    monkeypatch.delenv("DATABASE_URL")
    with pytest.raises(ValidationError), TestClient(app):
        pass
