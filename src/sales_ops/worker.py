"""Worker: `python -m sales_ops.worker`. Qualifies leads, drafts follow-ups, sends approved ones.

LLM jobs (qualify, draft): build the prompt, call the LLM outside any transaction, record the
call, then apply the outcome in one transaction that is conditional on still holding the lease.

    transient error (429, 5xx, timeout, connection) -> retry later with backoff (AC-2.2)
    retries used up, permanent error, unexpected bug -> failure route + one alert (AC-2.3)
    unusable output (refusal, truncation, bad JSON, schema, domain) -> needs_review, no retry
    valid output -> stored; the lead moves on (qualified -> draft job -> awaiting_approval)

Send jobs (AC-3.1 to AC-3.3): under the lead row lock, check that the lead is approved and that
the approval is bound to the current draft, and record the outbox row; then send over SMTP;
then mark the outbox row sent. A re-run finds it sent and does not send again. A crash between
the SMTP send and that mark sends the mail again on the retry (README, Failure cases).

Every attempted LLM call is recorded. Token counts and cost are recorded only when the provider
reported usage; otherwise they stay NULL, which means unknown (U5).
"""

import os
import signal
import socket
import threading
import time
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Self
from uuid import UUID

import structlog
from pydantic import model_validator
from sqlalchemy import Engine, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError

from sales_ops import approvals, drafting, jobs
from sales_ops.config import get_settings
from sales_ops.db import get_engine
from sales_ops.enrichment import company_profile
from sales_ops.llm import LLMClient, LLMRequest, LLMResponse, PermanentLLMError, TransientLLMError
from sales_ops.logging_config import configure_logging
from sales_ops.providers import ProviderSettings, build_llm_client
from sales_ops.qualification import PROMPT_VERSION, Accepted, LeadContext, build_request, evaluate
from sales_ops.sending import (
    MAILPIT_HOSTS,
    Mailer,
    PermanentSendError,
    SMTPMailer,
    TransientSendError,
)
from sales_ops.states import InvalidTransition, transition
from sales_ops.tables import inquiries, lead_assessments, leads, outbox, review_requests

log = structlog.get_logger(__name__)
Log = structlog.stdlib.BoundLogger


class SentButNotRecorded(Exception):
    """The SMTP server accepted the mail, but the database would not take the record."""


class WorkerSettings(ProviderSettings):
    llm_max_output_tokens: int = 4096
    # USD per million tokens for LLM_MODEL. Unset -> cost_usd is recorded as NULL (unknown).
    llm_price_input_usd_per_mtok: Decimal | None = None
    llm_price_output_usd_per_mtok: Decimal | None = None
    worker_backoff_base_s: float = 30.0
    worker_backoff_max_s: float = 600.0
    worker_lease_s: int = 300
    worker_poll_interval_s: float = 1.0
    qualify_low_confidence_threshold: float = 0.5
    # P01 sends to Mailpit only (D-11); any other host is refused at startup.
    smtp_host: str = "127.0.0.1"
    smtp_port: int = 1025
    smtp_timeout_s: float = 10.0
    mail_from: str = "sales@sales-ops.example"
    # Recording a mail the SMTP server accepted is retried this often before the job is left to
    # its lease (it is never recorded as a send failure).
    send_record_attempts: int = 3
    send_record_pause_s: float = 1.0

    @model_validator(mode="after")
    def _mailpit_only(self) -> Self:
        if self.smtp_host not in MAILPIT_HOSTS:
            raise ValueError(f"P01 sends only to Mailpit; SMTP_HOST {self.smtp_host!r} refused")
        return self


def backoff_seconds(settings: WorkerSettings, attempt: int) -> float:
    return min(settings.worker_backoff_max_s, settings.worker_backoff_base_s * 2.0 ** (attempt - 1))


def call_cost_usd(
    settings: WorkerSettings, input_tokens: int | None, output_tokens: int | None
) -> Decimal | None:
    """Cost of one call, or None when it cannot be known (no usage, or no price configured)."""
    price_in = settings.llm_price_input_usd_per_mtok
    price_out = settings.llm_price_output_usd_per_mtok
    if input_tokens is None or output_tokens is None or price_in is None or price_out is None:
        return None
    return (Decimal(input_tokens) * price_in + Decimal(output_tokens) * price_out) / 1_000_000


def run_once(
    engine: Engine,
    llm: LLMClient,
    settings: WorkerSettings,
    worker_id: str,
    *,
    kinds: Sequence[str] | None = None,
    mailer: Mailer | None = None,
) -> bool:
    """Process at most one job (of `kinds`, default all). False when there was nothing to do."""
    job = jobs.claim_next(engine, worker_id, settings.worker_lease_s, kinds)
    if job is None:
        return False
    process_job(engine, llm, settings, job, worker_id, mailer=mailer)
    return True


def run_until_idle(
    engine: Engine,
    llm: LLMClient,
    settings: WorkerSettings,
    worker_id: str,
    *,
    kinds: Sequence[str] | None = None,
    mailer: Mailer | None = None,
) -> int:
    processed = 0
    while run_once(engine, llm, settings, worker_id, kinds=kinds, mailer=mailer):
        processed += 1
    return processed


def process_job(
    engine: Engine,
    llm: LLMClient,
    settings: WorkerSettings,
    job: jobs.ClaimedJob,
    worker_id: str,
    *,
    mailer: Mailer | None = None,
) -> None:
    """Run one claimed job to its end."""
    try:
        _dispatch(engine, llm, mailer, settings, job, worker_id)
    except SentButNotRecorded:
        # Failing the job would mark the lead send_failed although the mail went out. Leave
        # the job to its lease instead, as if the worker had crashed: the next attempt finds the
        # outbox row unmarked and sends again with the same Message-ID (README, known limit).
        log.error("send.not_recorded", job_id=str(job.id), lead_id=str(job.lead_id))
    except Exception as exc:
        # A bug or a database error fails this job; it must not stop the worker (AC-2.3).
        log.exception("job.crashed", job_id=str(job.id), lead_id=str(job.lead_id))
        with engine.begin() as conn:
            jobs.fail(conn, job, worker_id, f"unexpected:{type(exc).__name__}")


def _dispatch(
    engine: Engine,
    llm: LLMClient,
    mailer: Mailer | None,
    settings: WorkerSettings,
    job: jobs.ClaimedJob,
    worker_id: str,
) -> None:
    bound = log.bind(
        job_id=str(job.id), lead_id=str(job.lead_id), job_kind=job.kind, attempt=job.attempts
    )
    if job.attempts > job.max_attempts:
        # A worker died during the last allowed attempt and its lease ran out.
        with engine.begin() as conn:
            jobs.fail(conn, job, worker_id, "retries_exhausted:lease_expired")
        return
    if job.kind == jobs.JOB_QUALIFY:
        _qualify(engine, llm, settings, job, worker_id, bound)
    elif job.kind == jobs.JOB_DRAFT:
        _draft(engine, llm, settings, job, worker_id, bound)
    elif job.kind == jobs.JOB_SEND:
        if mailer is None:
            mailer = SMTPMailer(settings.smtp_host, settings.smtp_port, settings.smtp_timeout_s)
        _send(engine, mailer, settings, job, worker_id, bound)
    else:
        raise ValueError(f"unknown job kind {job.kind!r}")


def _give_up(engine: Engine, job: jobs.ClaimedJob, worker_id: str, reason: str, bound: Log) -> None:
    with engine.begin() as conn:
        jobs.fail(conn, job, worker_id, reason)
    bound.warning("job.failed", reason=reason)


def _transient_failure(
    engine: Engine,
    settings: WorkerSettings,
    job: jobs.ClaimedJob,
    worker_id: str,
    error_kind: str,
    bound: Log,
) -> None:
    if job.attempts >= job.max_attempts:
        _give_up(engine, job, worker_id, f"retries_exhausted:{error_kind}", bound)
    else:
        delay = backoff_seconds(settings, job.attempts)
        jobs.retry_later(engine, job, worker_id, delay, error_kind)
        bound.info("job.retry_scheduled", error_kind=error_kind, delay_s=delay)


# LLM jobs -------------------------------------------------------------------------------------


def _qualify(
    engine: Engine,
    llm: LLMClient,
    settings: WorkerSettings,
    job: jobs.ClaimedJob,
    worker_id: str,
    bound: Log,
) -> None:
    request = build_request(
        _load_context(engine, job.lead_id), max_output_tokens=settings.llm_max_output_tokens
    )
    called = _call_llm(engine, llm, settings, job, worker_id, request, PROMPT_VERSION, bound)
    if called is None:
        return
    response, started_at, started = called

    outcome = evaluate(response, settings.qualify_low_confidence_threshold)
    call_id = _record(
        engine,
        job,
        llm,
        settings,
        PROMPT_VERSION,
        "ok" if isinstance(outcome, Accepted) else "rejected_output",
        started_at,
        started,
        response=response,
        error_kind=None if isinstance(outcome, Accepted) else outcome.kind,
    )

    with engine.begin() as conn:
        if not jobs.complete(conn, job, worker_id):
            bound.warning("job.lease_lost")  # another worker took over; drop this result
            return
        if isinstance(outcome, Accepted):
            a = outcome.assessment
            conn.execute(
                pg_insert(lead_assessments)
                .values(
                    lead_id=job.lead_id,
                    llm_call_id=call_id,
                    prompt_version=PROMPT_VERSION,
                    score=a.score,
                    tier=a.tier,
                    reasons=list(a.reasons),
                    summary=a.summary,
                    confidence=a.confidence,
                )
                .on_conflict_do_nothing(index_elements=["lead_id"])
            )
            new_status = "needs_review" if outcome.needs_review else "qualified"
            status_reason: str | None = "low_confidence" if outcome.needs_review else None
        else:
            new_status, status_reason = "needs_review", f"invalid_output:{outcome.kind}"
        transition(conn, job.lead_id, "received", new_status, status_reason)
        if new_status == "qualified":
            jobs.enqueue(conn, jobs.JOB_DRAFT, job.lead_id)  # same transaction as the status
    bound.info("job.done", lead_status=new_status, reason=status_reason)


def _draft(
    engine: Engine,
    llm: LLMClient,
    settings: WorkerSettings,
    job: jobs.ClaimedJob,
    worker_id: str,
    bound: Log,
) -> None:
    request = drafting.build_request(
        _load_draft_context(engine, job.lead_id), max_output_tokens=settings.llm_max_output_tokens
    )
    prompt_version = drafting.DRAFT_PROMPT_VERSION
    called = _call_llm(engine, llm, settings, job, worker_id, request, prompt_version, bound)
    if called is None:
        return
    response, started_at, started = called

    outcome = drafting.evaluate(response)
    call_id = _record(
        engine,
        job,
        llm,
        settings,
        prompt_version,
        "ok" if isinstance(outcome, drafting.Draft) else "rejected_output",
        started_at,
        started,
        response=response,
        error_kind=None if isinstance(outcome, drafting.Draft) else outcome.kind,
    )

    with engine.begin() as conn:
        if not jobs.complete(conn, job, worker_id):
            bound.warning("job.lease_lost")
            return
        if isinstance(outcome, drafting.Draft):
            draft_id = approvals.add_draft(
                conn, job.lead_id, outcome, created_by="llm", llm_call_id=call_id
            )
            # n8n W2 asks a person to review it (M4); same transaction as the status change.
            conn.execute(insert(review_requests).values(lead_id=job.lead_id, draft_id=draft_id))
            new_status, status_reason = "awaiting_approval", None
        else:
            new_status, status_reason = "needs_review", f"invalid_draft:{outcome.kind}"
        transition(conn, job.lead_id, "qualified", new_status, status_reason)
    bound.info("job.done", lead_status=new_status, reason=status_reason)


def _call_llm(
    engine: Engine,
    llm: LLMClient,
    settings: WorkerSettings,
    job: jobs.ClaimedJob,
    worker_id: str,
    request: LLMRequest,
    prompt_version: str,
    bound: Log,
) -> tuple[LLMResponse, datetime, float] | None:
    """Call the LLM once. A failed call is recorded here and the job is retried later or given
    up (returns None). A response is returned for the caller to validate and record."""
    started_at = datetime.now(UTC)
    started = time.perf_counter()
    try:
        return llm.complete(request), started_at, started
    except TransientLLMError as exc:
        _record(
            engine,
            job,
            llm,
            settings,
            prompt_version,
            "transient_error",
            started_at,
            started,
            error=exc,
        )
        _transient_failure(engine, settings, job, worker_id, exc.kind, bound)
        return None
    except PermanentLLMError as exc:
        _record(engine, job, llm, settings, prompt_version, "error", started_at, started, error=exc)
        _give_up(engine, job, worker_id, f"llm_error:{exc.kind}", bound)
        return None
    except Exception as exc:
        # The call was attempted: record it, then let run_once fail the job.
        _record(engine, job, llm, settings, prompt_version, "error", started_at, started, error=exc)
        raise


def _record(
    engine: Engine,
    job: jobs.ClaimedJob,
    llm: LLMClient,
    settings: WorkerSettings,
    prompt_version: str,
    outcome: str,
    started_at: datetime,
    started: float,
    *,
    response: LLMResponse | None = None,
    error: Exception | None = None,
    error_kind: str | None = None,
) -> UUID:
    """Record one attempted call. Usage and cost come only from a provider response."""
    if isinstance(error, TransientLLMError | PermanentLLMError):
        error_kind, http_status = error.kind, error.http_status
    elif error is not None:
        error_kind, http_status = f"unexpected:{type(error).__name__}", None
    else:
        http_status = None
    input_tokens = response.input_tokens if response is not None else None
    output_tokens = response.output_tokens if response is not None else None
    return jobs.record_llm_call(
        engine,
        job,
        provider=llm.provider,
        model=llm.model,
        prompt_version=prompt_version,
        outcome=outcome,
        latency_ms=round((time.perf_counter() - started) * 1000),
        started_at=started_at,
        ended_at=datetime.now(UTC),
        response_received=response is not None,
        error_kind=error_kind,
        http_status=http_status,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=call_cost_usd(settings, input_tokens, output_tokens),
        raw_output=response.text if response is not None else None,
    )


def _load_context(engine: Engine, lead_id: UUID) -> LeadContext:
    with engine.connect() as conn:
        lead = conn.execute(
            select(leads.c.company, leads.c.email_normalized).where(leads.c.id == lead_id)
        ).one()
        messages = conn.execute(
            select(inquiries.c.source, inquiries.c.message)
            .where(inquiries.c.lead_id == lead_id)
            .order_by(inquiries.c.received_at, inquiries.c.id)
        ).all()
    domain = lead.email_normalized.rpartition("@")[2]
    return LeadContext(
        company=lead.company,
        email_domain=domain,
        company_profile=company_profile(domain),
        messages=[(m.source, m.message) for m in messages],
    )


def _load_draft_context(engine: Engine, lead_id: UUID) -> drafting.DraftContext:
    context = _load_context(engine, lead_id)
    with engine.connect() as conn:
        assessment = conn.execute(
            select(lead_assessments.c.tier, lead_assessments.c.summary).where(
                lead_assessments.c.lead_id == lead_id
            )
        ).one()
    return drafting.DraftContext(
        company=context.company,
        email_domain=context.email_domain,
        tier=assessment.tier,
        summary=assessment.summary,
        messages=context.messages,
    )


# Send jobs ------------------------------------------------------------------------------------


def _send(
    engine: Engine,
    mailer: Mailer,
    settings: WorkerSettings,
    job: jobs.ClaimedJob,
    worker_id: str,
    bound: Log,
) -> None:
    with engine.begin() as conn:
        lead = approvals.lock_lead(conn, job.lead_id)  # approve and edit take the same lock
        if lead.status == "sent":  # AC-3.3: a re-run after the mail went out
            jobs.complete(conn, job, worker_id, note="skipped:already_sent")
            bound.info("send.skipped", reason="already_sent")
            return
        if lead.status != "approved":  # AC-3.1, AC-3.4: nothing is sent without an approval
            note = f"refused:not_approved:{lead.status}"
            jobs.complete(conn, job, worker_id, note=note)
            bound.warning("send.refused", reason=note)
            return
        approval = approvals.active_approval(conn, job.lead_id)
        draft = approvals.current_draft(conn, job.lead_id)
        if approval is None or draft is None:
            jobs.complete(conn, job, worker_id, note="refused:no_approval")
            bound.warning("send.refused", reason="refused:no_approval")
            return
        if approval.draft_sha256 != draft.sha256:  # AC-3.2: the draft is not what was approved
            approvals.revoke(conn, approval.id, "draft_changed_after_approval")
            transition(
                conn, job.lead_id, "approved", "awaiting_approval", "approval_revoked:draft_changed"
            )
            jobs.complete(conn, job, worker_id, note="refused:draft_changed")
            bound.warning("send.refused", reason="refused:draft_changed")
            return
        pending = conn.execute(
            select(outbox).where(
                outbox.c.lead_id == job.lead_id, outbox.c.draft_sha256 == draft.sha256
            )
        ).first()
        if pending is None:
            pending = conn.execute(
                insert(outbox)
                .values(
                    lead_id=job.lead_id,
                    approval_id=approval.id,
                    draft_sha256=draft.sha256,
                    # Stored with the row, so a resend carries the same Message-ID.
                    message_id=f"<{uuid.uuid4()}@sales-ops.example>",
                    recipient=lead.email_normalized,
                    subject=draft.subject,
                    body=draft.body,
                )
                .returning(outbox)
            ).one()
        elif pending.sent_at is not None:
            # Defensive: marked sent while the lead stayed approved. The mark and the status
            # change commit together below, so this needs a manual change to the database.
            transition(conn, job.lead_id, "approved", "sent", "already_sent")
            jobs.complete(conn, job, worker_id, note="skipped:already_sent")
            bound.info("send.skipped", reason="already_sent")
            return

    try:
        mailer.send(
            message_id=pending.message_id,
            sender=settings.mail_from,
            recipient=pending.recipient,
            subject=pending.subject,
            body=pending.body,
        )
    except TransientSendError as exc:
        _transient_failure(engine, settings, job, worker_id, exc.kind, bound)
        return
    except PermanentSendError as exc:
        _give_up(engine, job, worker_id, f"send_error:{exc.kind}", bound)
        return

    _record_sent(engine, settings, job, worker_id, pending.id, bound)
    bound.info("send.done", message_id=pending.message_id)


def _record_sent(
    engine: Engine,
    settings: WorkerSettings,
    job: jobs.ClaimedJob,
    worker_id: str,
    outbox_id: UUID,
    bound: Log,
) -> None:
    """Record a mail the SMTP server accepted. Safe to repeat. Never records a failure."""
    for attempt in range(1, settings.send_record_attempts + 1):
        try:
            with engine.begin() as conn:
                # Recorded even if this worker lost its lease meanwhile: the mail is out, and
                # whoever runs this job next must see that before sending.
                conn.execute(
                    update(outbox)
                    .where(outbox.c.id == outbox_id, outbox.c.sent_at.is_(None))
                    .values(sent_at=datetime.now(UTC))
                )
                try:
                    transition(conn, job.lead_id, "approved", "sent")
                except InvalidTransition:
                    bound.warning("send.lead_already_moved")  # e.g. recorded by another worker
                jobs.complete(conn, job, worker_id)
            return
        except SQLAlchemyError:
            bound.warning("send.record_failed", record_attempt=attempt)
            if attempt < settings.send_record_attempts:
                time.sleep(settings.send_record_pause_s)
    raise SentButNotRecorded(str(outbox_id))


def main() -> None:
    configure_logging()
    settings = WorkerSettings()  # type: ignore[call-arg]  # values come from the environment
    get_settings()  # fail fast on a missing DATABASE_URL
    engine = get_engine()
    llm = build_llm_client(settings)
    mailer = SMTPMailer(settings.smtp_host, settings.smtp_port, settings.smtp_timeout_s)
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    stopping = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopping.set())
    signal.signal(signal.SIGINT, lambda *_: stopping.set())
    log.info("worker.started", worker_id=worker_id, provider=llm.provider, model=llm.model)
    while not stopping.is_set():
        try:
            worked = run_once(engine, llm, settings, worker_id, mailer=mailer)
        except Exception:
            # e.g. the database is briefly unreachable: log, wait, keep going.
            log.exception("worker.loop_error")
            worked = False
        if not worked:
            stopping.wait(settings.worker_poll_interval_s)
    log.info("worker.stopped", worker_id=worker_id)


if __name__ == "__main__":
    main()
