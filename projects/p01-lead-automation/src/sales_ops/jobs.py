"""Job queue on PostgreSQL (D-6): enqueue, claim, finish, retry, fail.

A worker claims one job with FOR UPDATE SKIP LOCKED and takes a lease (locked_until). The LLM
call happens outside any transaction. Every write that ends an attempt is conditional on the
worker still holding the lease, so a worker whose lease expired cannot apply its late result.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, Engine, and_, func, insert, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from sales_ops.states import InvalidTransition, transition
from sales_ops.tables import alert_events, jobs, llm_calls

JOB_QUALIFY = "qualify_lead"
JOB_DRAFT = "draft_follow_up"
JOB_SEND = "send_follow_up"
DEFAULT_MAX_ATTEMPTS = 5  # D-9
RAW_OUTPUT_LIMIT = 20_000


@dataclass(frozen=True)
class FailureRoute:
    """Where a lead goes, and which alert is raised, when a job of this kind gives up."""

    from_status: str
    to_status: str
    alert_kind: str


FAILURE_ROUTES = {
    JOB_QUALIFY: FailureRoute("received", "processing_failed", "processing_failed"),
    JOB_DRAFT: FailureRoute("qualified", "processing_failed", "processing_failed"),
    JOB_SEND: FailureRoute("approved", "send_failed", "send_failed"),
}


@dataclass(frozen=True)
class ClaimedJob:
    id: UUID
    kind: str
    lead_id: UUID
    attempts: int  # including the attempt that was just claimed
    max_attempts: int


def enqueue(conn: Connection, kind: str, lead_id: UUID, *, requeue: bool = False) -> None:
    """One job per (kind, lead). With requeue=True an existing job starts over from queued
    (a new approval after a revoked one needs the send job to run once more).

    A running job is left alone: resetting it would let a second worker claim it while the
    first is still working, and both could send. The running attempt has not yet read the
    lead under its lock (anything later would have blocked the approval), so it sees the new
    approval itself.
    """
    statement = pg_insert(jobs).values(
        kind=kind, lead_id=lead_id, max_attempts=DEFAULT_MAX_ATTEMPTS
    )
    if requeue:
        statement = statement.on_conflict_do_update(
            index_elements=["kind", "lead_id"],
            set_={
                "status": "queued",
                "attempts": 0,
                "run_after": func.now(),
                "locked_by": None,
                "locked_until": None,
                "last_error": None,
                "updated_at": func.now(),
            },
            where=jobs.c.status != "running",
        )
    else:
        statement = statement.on_conflict_do_nothing(index_elements=["kind", "lead_id"])
    conn.execute(statement)


def claim_next(
    engine: Engine, worker_id: str, lease_s: int, kinds: Sequence[str] | None = None
) -> ClaimedJob | None:
    now = func.now()
    candidate = (
        select(jobs.c.id)
        .where(
            or_(
                and_(jobs.c.status == "queued", jobs.c.run_after <= now),
                # A lease that ran out means the worker died mid-attempt: take the job over.
                and_(jobs.c.status == "running", jobs.c.locked_until < now),
            ),
            jobs.c.kind.in_(kinds) if kinds is not None else jobs.c.kind.isnot(None),
        )
        .order_by(jobs.c.run_after, jobs.c.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
        .scalar_subquery()
    )
    with engine.begin() as conn:
        row = conn.execute(
            update(jobs)
            .where(jobs.c.id == candidate)
            .values(
                status="running",
                attempts=jobs.c.attempts + 1,
                locked_by=worker_id,
                locked_until=now + timedelta(seconds=lease_s),
                updated_at=now,
            )
            .returning(jobs.c.id, jobs.c.kind, jobs.c.lead_id, jobs.c.attempts, jobs.c.max_attempts)
        ).first()
    if row is None:
        return None
    return ClaimedJob(row.id, row.kind, row.lead_id, row.attempts, row.max_attempts)


def complete(conn: Connection, job: ClaimedJob, worker_id: str, note: str | None = None) -> bool:
    """Mark the job done. False if this worker no longer holds the lease (nothing written).
    `note` records why a job ended without doing its work (e.g. "refused:not_approved")."""
    return _end_attempt(conn, job, worker_id, status="done", last_error=note)


def retry_later(
    engine: Engine, job: ClaimedJob, worker_id: str, delay_s: float, error: str
) -> bool:
    with engine.begin() as conn:
        return _end_attempt(
            conn,
            job,
            worker_id,
            status="queued",
            last_error=error,
            run_after=func.now() + timedelta(seconds=delay_s),
        )


def fail(conn: Connection, job: ClaimedJob, worker_id: str, reason: str) -> bool:
    """Give up on the job: move the lead along its failure route and raise exactly one alert
    (AC-2.3; for sending, approved -> send_failed)."""
    if not _end_attempt(conn, job, worker_id, status="failed", last_error=reason):
        return False
    route = FAILURE_ROUTES[job.kind]
    detail: dict[str, Any] = {"reason": reason, "attempts": job.attempts, "job_kind": job.kind}
    try:
        transition(conn, job.lead_id, route.from_status, route.to_status, reason)
    except InvalidTransition as exc:
        # The alert still goes out; it says why the lead itself did not move.
        detail["lead_not_moved"] = str(exc)
    conn.execute(
        pg_insert(alert_events)
        .values(kind=route.alert_kind, lead_id=job.lead_id, job_id=job.id, detail=detail)
        .on_conflict_do_nothing(index_elements=["kind", "job_id"])
    )
    return True


def record_llm_call(
    engine: Engine,
    job: ClaimedJob,
    *,
    provider: str,
    model: str,
    prompt_version: str,
    outcome: str,
    latency_ms: int,
    started_at: datetime,
    ended_at: datetime,
    response_received: bool,
    error_kind: str | None = None,
    http_status: int | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cost_usd: Decimal | None = None,
    raw_output: str | None = None,
) -> UUID:
    """One row per attempted call, in its own transaction: a call costs money even if the job
    is lost. Missing usage is stored as NULL, never as 0."""
    with engine.begin() as conn:
        call_id: UUID = conn.execute(
            insert(llm_calls)
            .values(
                job_id=job.id,
                lead_id=job.lead_id,
                attempt=job.attempts,
                provider=provider,
                model=model,
                prompt_version=prompt_version,
                outcome=outcome,
                error_kind=error_kind,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                usage_available=input_tokens is not None and output_tokens is not None,
                cost_usd=cost_usd,
                response_received=response_received,
                http_status=http_status,
                latency_ms=latency_ms,
                started_at=started_at,
                ended_at=ended_at,
                raw_output=_storable(raw_output),
            )
            .returning(llm_calls.c.id)
        ).scalar_one()
    return call_id


def _storable(raw_output: str | None) -> str | None:
    """The raw output as a text column can hold it. PostgreSQL text cannot contain NUL, so each
    NUL is stored as U+2400 (the symbol for NUL): one visible character for one, and distinct from
    the escape \\u0000 that an escaped NUL leaves in the text. JSON allows NUL only as that escape,
    so an output with a NUL in its text is always rejected as invalid_json; this copy is only for
    the person who reviews it (U16, D-97)."""
    if raw_output is None:
        return None
    return raw_output.replace("\x00", "\u2400")[:RAW_OUTPUT_LIMIT]


def _end_attempt(
    conn: Connection, job: ClaimedJob, worker_id: str, *, status: str, **values: Any
) -> bool:
    result = conn.execute(
        update(jobs)
        .where(jobs.c.id == job.id, jobs.c.status == "running", jobs.c.locked_by == worker_id)
        .values(status=status, locked_by=None, locked_until=None, updated_at=func.now(), **values)
    )
    return result.rowcount == 1
