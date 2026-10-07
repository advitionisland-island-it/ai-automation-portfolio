"""Lead intake: idempotency, duplicate prevention, persistence (AC-1.2 to AC-1.5).

A new lead also gets its qualification job, in the same transaction (D-6, D-41).

Everything happens in one transaction, and every race is settled by a database constraint
(INSERT ... ON CONFLICT), never by "check, then insert".
"""

import hashlib
import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import Connection, Engine, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from sales_ops.jobs import JOB_QUALIFY, enqueue
from sales_ops.schemas import LeadIn
from sales_ops.tables import idempotency_keys, inquiries, leads

log = structlog.get_logger(__name__)

IDEMPOTENCY_SCOPE = "POST /v1/leads"


class IdempotencyKeyReused(Exception):
    """The key was already used with a different request body."""


@dataclass(frozen=True)
class IngestResult:
    status_code: int
    body: dict[str, Any]
    replayed: bool


def normalize_email(email: str) -> str:
    return email.strip().lower()


def request_fingerprint(payload: LeadIn) -> str:
    """sha256 of the validated payload as canonical JSON (key order and whitespace ignored)."""
    canonical = json.dumps(
        payload.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def ingest_lead(engine: Engine, payload: LeadIn, idempotency_key: str) -> IngestResult:
    fingerprint = request_fingerprint(payload)
    with engine.begin() as conn:
        claimed = conn.execute(
            pg_insert(idempotency_keys)
            .values(scope=IDEMPOTENCY_SCOPE, key=idempotency_key, request_sha256=fingerprint)
            .on_conflict_do_nothing(index_elements=["scope", "key"])
            .returning(idempotency_keys.c.key)
        ).first()
        if claimed is None:
            return _replay(conn, idempotency_key, fingerprint)

        lead_id, lead_status, lead_created = _upsert_lead(conn, payload)
        if lead_created:
            enqueue(conn, JOB_QUALIFY, lead_id)
        inquiry_id: UUID = conn.execute(
            insert(inquiries)
            .values(
                lead_id=lead_id,
                source=payload.source,
                message=payload.message,
                payload=payload.model_dump(mode="json"),
            )
            .returning(inquiries.c.id)
        ).scalar_one()

        body: dict[str, Any] = {
            "lead_id": str(lead_id),
            "inquiry_id": str(inquiry_id),
            "lead_created": lead_created,
            "status": lead_status,
        }
        conn.execute(
            update(idempotency_keys)
            .where(
                idempotency_keys.c.scope == IDEMPOTENCY_SCOPE,
                idempotency_keys.c.key == idempotency_key,
            )
            .values(lead_id=lead_id, inquiry_id=inquiry_id, response_status=201, response_body=body)
        )

    log.info(
        "lead.ingested",
        lead_id=str(lead_id),
        inquiry_id=str(inquiry_id),
        lead_created=lead_created,
        email=payload.email,
        source=payload.source,
    )
    return IngestResult(status_code=201, body=body, replayed=False)


def _replay(conn: Connection, idempotency_key: str, fingerprint: str) -> IngestResult:
    # Reaching this point means another transaction claimed the key and has committed:
    # ON CONFLICT DO NOTHING waits for an in-flight claim to finish before giving up.
    row = conn.execute(
        select(
            idempotency_keys.c.request_sha256,
            idempotency_keys.c.response_status,
            idempotency_keys.c.response_body,
        ).where(
            idempotency_keys.c.scope == IDEMPOTENCY_SCOPE,
            idempotency_keys.c.key == idempotency_key,
        )
    ).one()
    if row.request_sha256 != fingerprint:
        log.warning("lead.idempotency_key_reused")
        raise IdempotencyKeyReused
    if row.response_status is None or row.response_body is None:
        # Cannot happen: the claim and the stored response are written in the same transaction.
        raise RuntimeError("idempotency key has no stored response")
    log.info("lead.replayed", lead_id=row.response_body.get("lead_id"))
    return IngestResult(status_code=row.response_status, body=row.response_body, replayed=True)


def _upsert_lead(conn: Connection, payload: LeadIn) -> tuple[UUID, str, bool]:
    """Return (lead_id, status, created). The first inquiry from an email creates the lead."""
    email_normalized = normalize_email(payload.email)
    created = conn.execute(
        pg_insert(leads)
        .values(email_normalized=email_normalized, name=payload.name, company=payload.company)
        .on_conflict_do_nothing(index_elements=["email_normalized"])
        .returning(leads.c.id, leads.c.status)
    ).first()
    if created is not None:
        return created.id, created.status, True
    # The conflicting insert (if it was concurrent) has committed by now, so it is visible.
    existing = conn.execute(
        select(leads.c.id, leads.c.status).where(leads.c.email_normalized == email_normalized)
    ).one()
    return existing.id, existing.status, False
