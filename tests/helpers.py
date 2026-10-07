"""Shared test data and helpers (import from here, not from conftest)."""

import os
import uuid
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx2
from alembic.config import Config
from sqlalchemy import Engine, Row, func, select

from sales_ops.ingest import ingest_lead
from sales_ops.jobs import JOB_QUALIFY
from sales_ops.schemas import LeadIn
from sales_ops.tables import (
    alert_events,
    idempotency_keys,
    inquiries,
    jobs,
    lead_assessments,
    leads,
    llm_calls,
)

# Tests run against a real PostgreSQL (D-14). A missing database is a test failure,
# never a skip: a skipped integration test looks green while proving nothing.
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://sales_ops:sales_ops@127.0.0.1:5432/sales_ops_test",
)
PROJECT_ROOT = Path(__file__).resolve().parents[1]
# Mailpit is required the same way (D-11): the tests count what really arrived over SMTP.
MAILPIT_URL = os.environ.get("TEST_MAILPIT_URL", "http://127.0.0.1:8025")
MAILPIT_SMTP_PORT = int(os.environ.get("TEST_MAILPIT_SMTP_PORT", "1025"))


def alembic_config(database_url: str) -> Config:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


VALID: dict[str, Any] = {
    "email": "Jane.Doe@Example.com",
    "name": "Jane Doe",
    "company": "Acme KK",
    "message": "We would like to automate our lead intake.",
    "source": "web_form",
}


def row_counts(engine: Engine) -> dict[str, int]:
    with engine.connect() as conn:
        return {
            table.name: conn.execute(select(func.count()).select_from(table)).scalar_one()
            for table in (leads, inquiries, idempotency_keys)
        }


def new_lead(engine: Engine, email: str = VALID["email"], message: str = VALID["message"]) -> UUID:
    payload = LeadIn(**{**VALID, "email": email, "message": message})
    result = ingest_lead(engine, payload, idempotency_key=f"key-{email}-{message}")
    return UUID(result.body["lead_id"])


def lead_state(engine: Engine, lead_id: UUID) -> tuple[str, str | None]:
    with engine.connect() as conn:
        row = conn.execute(
            select(leads.c.status, leads.c.status_reason).where(leads.c.id == lead_id)
        ).one()
    return row.status, row.status_reason


def job_row(engine: Engine, lead_id: UUID, kind: str = JOB_QUALIFY) -> Row[Any]:
    with engine.connect() as conn:
        return conn.execute(
            select(jobs).where(jobs.c.lead_id == lead_id, jobs.c.kind == kind)
        ).one()


def call_rows(engine: Engine, lead_id: UUID) -> list[Row[Any]]:
    with engine.connect() as conn:
        return list(
            conn.execute(
                select(llm_calls)
                .where(llm_calls.c.lead_id == lead_id)
                .order_by(llm_calls.c.attempt, llm_calls.c.created_at)
            )
        )


def assessment_rows(engine: Engine, lead_id: UUID) -> list[Row[Any]]:
    with engine.connect() as conn:
        return list(
            conn.execute(select(lead_assessments).where(lead_assessments.c.lead_id == lead_id))
        )


def alert_count(engine: Engine, lead_id: UUID) -> int:
    with engine.connect() as conn:
        return conn.execute(
            select(func.count()).select_from(alert_events).where(alert_events.c.lead_id == lead_id)
        ).scalar_one()


def unique_email() -> str:
    """An address no other test or earlier run uses, so Mailpit counts belong to one test."""
    return f"lead-{uuid.uuid4().hex}@example.com"


class Mailpit:
    """What arrived in Mailpit, read through its API. Each test counts its own recipients.

    Mailpit stores a message before it answers the SMTP transaction, so a count taken after
    the sender returned is complete (evidence/p01/M3/mailpit-consistency.txt).
    """

    def __init__(self, base_url: str) -> None:
        self._http = httpx2.Client(base_url=base_url, timeout=5)

    def check_ready(self) -> None:
        self._http.get("/readyz").raise_for_status()

    def messages_to(self, recipient: str) -> list[dict[str, Any]]:
        response = self._http.get("/api/v1/search", params={"query": f"to:{recipient}"})
        response.raise_for_status()
        messages: list[dict[str, Any]] = response.json()["messages"]
        # The search matches substrings; keep exact recipients only.
        return [m for m in messages if [to["Address"] for to in m["To"]] == [recipient]]

    def text(self, message: dict[str, Any]) -> str:
        response = self._http.get(f"/api/v1/message/{message['ID']}")
        response.raise_for_status()
        text: str = response.json()["Text"]
        return text.replace("\r\n", "\n").removesuffix("\n")


def lead_awaiting_approval(engine: Engine) -> tuple[UUID, str]:
    """A lead qualified and drafted by the built-in fake provider (the U6 runtime)."""
    from sales_ops.fake_llm import HeuristicFakeLLM
    from sales_ops.jobs import JOB_DRAFT
    from sales_ops.worker import WorkerSettings, run_until_idle

    email = unique_email()
    lead_id = new_lead(engine, email=email, message=f"Please tell us more ({email})")
    settings = WorkerSettings(llm_provider="fake", worker_backoff_base_s=0)
    run_until_idle(engine, HeuristicFakeLLM(), settings, "setup", kinds=[JOB_QUALIFY, JOB_DRAFT])
    assert lead_state(engine, lead_id) == ("awaiting_approval", None)
    return lead_id, email
