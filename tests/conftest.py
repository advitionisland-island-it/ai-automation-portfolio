import threading
import time
from collections.abc import Iterator

import pytest
import uvicorn
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from sales_ops.config import get_settings
from sales_ops.db import build_engine, get_engine
from sales_ops.main import app
from sales_ops.sending import SMTPMailer
from sales_ops.tables import metadata
from tests.helpers import (
    MAILPIT_SMTP_PORT,
    MAILPIT_URL,
    TEST_DATABASE_URL,
    Mailpit,
    alembic_config,
)


def _reset_cached_config() -> None:
    if get_engine.cache_info().currsize:
        get_engine().dispose()
    get_engine.cache_clear()
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def app_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Configure the app the way a deployment does: through environment variables only."""
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    _reset_cached_config()
    yield
    _reset_cached_config()


@pytest.fixture(scope="session")
def db_engine() -> Iterator[Engine]:
    """Test database built from migrations on an empty schema.

    Starting from an empty schema (instead of downgrading) means a broken earlier run cannot
    block the whole suite. The downgrade paths have their own tests in test_migrations.py.
    """
    engine = build_engine(TEST_DATABASE_URL)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    command.upgrade(alembic_config(TEST_DATABASE_URL), "head")
    yield engine
    engine.dispose()


_ALL_TABLES = ", ".join(table.name for table in metadata.sorted_tables)


@pytest.fixture
def clean_db(db_engine: Engine) -> Engine:
    with db_engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {_ALL_TABLES}"))
    return db_engine


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def live_server() -> Iterator[str]:
    """The app served by a real uvicorn on a free port, for tests that need true concurrency."""
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=0, log_config=None, access_log=False)
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            raise RuntimeError("uvicorn did not start")
        time.sleep(0.02)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


@pytest.fixture(scope="session")
def mailpit() -> Mailpit:
    """Like the database, a missing Mailpit fails the tests that need it; it never skips them."""
    client = Mailpit(MAILPIT_URL)
    client.check_ready()
    return client


@pytest.fixture
def smtp() -> SMTPMailer:
    """The real SMTP sender, pointed at the test Mailpit."""
    return SMTPMailer("127.0.0.1", MAILPIT_SMTP_PORT)
