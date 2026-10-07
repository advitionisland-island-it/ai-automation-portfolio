"""Human approval and sending (AC-3.1 to AC-3.4, and the AC-3.6 limit).

The real path: review API -> PostgreSQL -> worker -> SMTP -> Mailpit. Mail is counted in
Mailpit through its API, per recipient; every test uses addresses of its own.
"""

import threading
from collections.abc import Callable
from typing import Any
from uuid import UUID

import httpx2
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, Row, func, select, text, update
from sqlalchemy.exc import IntegrityError, OperationalError

from sales_ops import approvals, jobs, worker
from sales_ops.drafting import Draft, draft_sha256
from sales_ops.sending import Mailer, PermanentSendError, SMTPMailer, TransientSendError
from sales_ops.tables import approvals as approvals_table
from sales_ops.tables import drafts, leads, outbox
from sales_ops.worker import WorkerSettings, process_job, run_once, run_until_idle
from tests.fakes import ScriptedLLM
from tests.helpers import (
    Mailpit,
    job_row,
    lead_awaiting_approval,
    lead_state,
    new_lead,
    unique_email,
)

SEND = [jobs.JOB_SEND]
# Send jobs never call the LLM; if one did, this makes the job fail loudly.
NO_LLM = ScriptedLLM(AssertionError("a send job called the LLM"))


def settings(**overrides: Any) -> WorkerSettings:
    values: dict[str, Any] = {
        "llm_provider": "fake",
        "worker_backoff_base_s": 0,
        "send_record_pause_s": 0,
        **overrides,
    }
    return WorkerSettings(**values)


def view(client: TestClient, lead_id: UUID) -> dict[str, Any]:
    response = client.get(f"/v1/leads/{lead_id}")
    assert response.status_code == 200
    body: dict[str, Any] = response.json()
    return body


def approve(client: TestClient, lead_id: UUID, sha256: str, who: str = "alice") -> httpx2.Response:
    return client.post(
        f"/v1/leads/{lead_id}/approve", json={"draft_sha256": sha256, "approved_by": who}
    )


def edit(client: TestClient, lead_id: UUID, subject: str, body: str) -> httpx2.Response:
    return client.put(
        f"/v1/leads/{lead_id}/draft", json={"subject": subject, "body": body, "edited_by": "bob"}
    )


def run_send_jobs(engine: Engine, mailer: Mailer, worker_id: str = "w1") -> int:
    return run_until_idle(engine, NO_LLM, settings(), worker_id, kinds=SEND, mailer=mailer)


def run_one_send_job(engine: Engine, mailer: Mailer) -> None:
    assert run_once(engine, NO_LLM, settings(), "w1", kinds=SEND, mailer=mailer)


def attempt_send(engine: Engine, lead_id: UUID) -> None:
    """A send request that did not come from an approval (a stray job, a bug, an operator)."""
    with engine.begin() as conn:
        jobs.enqueue(conn, jobs.JOB_SEND, lead_id, requeue=True)


def send_job(engine: Engine, lead_id: UUID) -> Row[Any]:
    return job_row(engine, lead_id, jobs.JOB_SEND)


def outbox_rows(engine: Engine, lead_id: UUID) -> list[Row[Any]]:
    with engine.connect() as conn:
        return list(conn.execute(select(outbox).where(outbox.c.lead_id == lead_id)))


def approval_rows(engine: Engine, lead_id: UUID) -> list[Row[Any]]:
    with engine.connect() as conn:
        return list(
            conn.execute(
                select(approvals_table)
                .where(approvals_table.c.lead_id == lead_id)
                .order_by(approvals_table.c.approved_at)
            )
        )


class ScriptedMailer:
    """Raises the given errors in order, then sends through `then` (or fails if none)."""

    def __init__(self, *errors: Exception, then: Mailer | None = None) -> None:
        self.errors = list(errors)
        self.then = then
        self.calls = 0

    def send(
        self, *, message_id: str, sender: str, recipient: str, subject: str, body: str
    ) -> None:
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        assert self.then is not None, "no more scripted errors and no real mailer"
        self.then.send(
            message_id=message_id, sender=sender, recipient=recipient, subject=subject, body=body
        )


class CallbackMailer:
    """Runs `before` inside the send, then sends for real: puts other work mid-send."""

    def __init__(self, inner: Mailer, before: Callable[[], None]) -> None:
        self.inner = inner
        self.before = before

    def send(
        self, *, message_id: str, sender: str, recipient: str, subject: str, body: str
    ) -> None:
        self.before()
        self.inner.send(
            message_id=message_id, sender=sender, recipient=recipient, subject=subject, body=body
        )


class ProcessDied(BaseException):
    """Stands in for the worker process dying: `except Exception` does not catch it."""


class DiesAfterSending:
    def __init__(self, inner: Mailer) -> None:
        self.inner = inner

    def send(
        self, *, message_id: str, sender: str, recipient: str, subject: str, body: str
    ) -> None:
        self.inner.send(
            message_id=message_id, sender=sender, recipient=recipient, subject=subject, body=body
        )
        raise ProcessDied


# The approved path ----------------------------------------------------------------------------


def test_approved_draft_is_sent_once_exactly_as_approved(
    client: TestClient, clean_db: Engine, mailpit: Mailpit, smtp: SMTPMailer
) -> None:
    lead_id, email = lead_awaiting_approval(clean_db)
    draft = view(client, lead_id)["draft"]

    response = approve(client, lead_id, draft["sha256"])
    assert response.status_code == 200
    assert response.json()["status"] == "approved"
    assert run_send_jobs(clean_db, smtp) == 1

    [mail] = mailpit.messages_to(email)
    assert mail["Subject"] == draft["subject"]
    assert mailpit.text(mail) == draft["body"]
    [record] = outbox_rows(clean_db, lead_id)
    assert f"<{mail['MessageID']}>" == record.message_id
    assert record.sent_at is not None
    assert lead_state(clean_db, lead_id) == ("sent", None)
    assert view(client, lead_id)["sent"]["message_id"] == record.message_id


# AC-3.1 ---------------------------------------------------------------------------------------


def test_unapproved_draft_is_refused_and_nothing_arrives(
    client: TestClient, clean_db: Engine, mailpit: Mailpit, smtp: SMTPMailer
) -> None:
    lead_id, email = lead_awaiting_approval(clean_db)

    attempt_send(clean_db, lead_id)
    assert run_send_jobs(clean_db, smtp) == 1

    job = send_job(clean_db, lead_id)
    assert (job.status, job.last_error) == ("done", "refused:not_approved:awaiting_approval")
    assert lead_state(clean_db, lead_id) == ("awaiting_approval", None)
    assert outbox_rows(clean_db, lead_id) == []
    assert mailpit.messages_to(email) == []


def test_approved_status_without_an_approval_record_is_refused(
    clean_db: Engine, mailpit: Mailpit, smtp: SMTPMailer
) -> None:
    # As if a bug had set the status directly, skipping the approval step.
    lead_id, email = lead_awaiting_approval(clean_db)
    with clean_db.begin() as conn:
        conn.execute(update(leads).where(leads.c.id == lead_id).values(status="approved"))

    attempt_send(clean_db, lead_id)
    run_send_jobs(clean_db, smtp)

    job = send_job(clean_db, lead_id)
    assert (job.status, job.last_error) == ("done", "refused:no_approval")
    assert outbox_rows(clean_db, lead_id) == []
    assert mailpit.messages_to(email) == []


def test_approval_needs_the_hash_of_the_current_draft(
    client: TestClient, clean_db: Engine, mailpit: Mailpit, smtp: SMTPMailer
) -> None:
    lead_id, email = lead_awaiting_approval(clean_db)
    other_text = draft_sha256("Some other subject", "Some other body")

    response = approve(client, lead_id, other_text)

    assert response.status_code == 409
    assert approval_rows(clean_db, lead_id) == []
    assert lead_state(clean_db, lead_id) == ("awaiting_approval", None)
    assert run_send_jobs(clean_db, smtp) == 0  # no send job was created
    assert mailpit.messages_to(email) == []


# AC-3.2 ---------------------------------------------------------------------------------------


def test_editing_after_approval_revokes_it_and_only_the_reapproved_text_is_sent(
    client: TestClient, clean_db: Engine, mailpit: Mailpit, smtp: SMTPMailer
) -> None:
    lead_id, email = lead_awaiting_approval(clean_db)
    first = view(client, lead_id)["draft"]
    assert approve(client, lead_id, first["sha256"]).status_code == 200

    response = edit(client, lead_id, "Edited subject", "Hello,\n\nEdited body.\n\nThe Sales Team")

    assert response.status_code == 200
    after_edit = response.json()
    assert (after_edit["status"], after_edit["status_reason"]) == (
        "awaiting_approval",
        "approval_revoked:draft_edited",
    )
    assert after_edit["approval"] is None
    assert after_edit["draft"]["version"] == 2
    [revoked] = approval_rows(clean_db, lead_id)
    assert (revoked.revoke_reason, revoked.revoked_at is not None) == ("draft_edited", True)

    # The send job queued by the first approval finds nothing approved.
    assert run_send_jobs(clean_db, smtp) == 1
    assert send_job(clean_db, lead_id).last_error == "refused:not_approved:awaiting_approval"
    assert mailpit.messages_to(email) == []
    # The old approval cannot be reused for the new text.
    assert approve(client, lead_id, first["sha256"]).status_code == 409

    second = after_edit["draft"]
    assert approve(client, lead_id, second["sha256"]).status_code == 200
    run_send_jobs(clean_db, smtp)
    [mail] = mailpit.messages_to(email)
    assert (mail["Subject"], mailpit.text(mail)) == (second["subject"], second["body"])
    assert second["body"] == "Hello,\n\nEdited body.\n\nThe Sales Team"


def test_a_draft_change_that_bypasses_the_api_is_caught_before_sending(
    client: TestClient, clean_db: Engine, mailpit: Mailpit, smtp: SMTPMailer
) -> None:
    lead_id, email = lead_awaiting_approval(clean_db)
    assert approve(client, lead_id, view(client, lead_id)["draft"]["sha256"]).status_code == 200
    # A new draft version written without going through the review API, so nothing revoked
    # the approval.
    with clean_db.begin() as conn:
        approvals.add_draft(
            conn, lead_id, Draft(subject="Changed", body="Changed body"), created_by="llm"
        )

    run_send_jobs(clean_db, smtp)

    assert send_job(clean_db, lead_id).last_error == "refused:draft_changed"
    assert lead_state(clean_db, lead_id) == ("awaiting_approval", "approval_revoked:draft_changed")
    [revoked] = approval_rows(clean_db, lead_id)
    assert revoked.revoke_reason == "draft_changed_after_approval"
    assert outbox_rows(clean_db, lead_id) == []
    assert mailpit.messages_to(email) == []


def test_database_keeps_every_draft_hash_equal_to_its_text(clean_db: Engine) -> None:
    lead_id = new_lead(clean_db, email=unique_email())
    good = Draft(subject="Subject", body="Body")
    with clean_db.begin() as conn:
        approvals.add_draft(conn, lead_id, good, created_by="llm")

    # Changing the text in place, without the hash:
    with pytest.raises(IntegrityError, match="sha256_matches_text"), clean_db.begin() as conn:
        conn.execute(update(drafts).where(drafts.c.lead_id == lead_id).values(body="Tampered"))
    # Storing a hash that is not the hash of the text:
    with pytest.raises(IntegrityError, match="sha256_matches_text"), clean_db.begin() as conn:
        conn.execute(
            drafts.insert().values(
                lead_id=lead_id,
                version=2,
                subject="Subject",
                body="Other body",
                sha256=good.sha256,
                created_by="llm",
            )
        )
    # Another split of the same hashed text: the hash matches, the one-line subject does not.
    paragraphs = Draft(subject="Offer", body="Hello,\n\nThanks.")
    with pytest.raises(IntegrityError, match="subject_one_line"), clean_db.begin() as conn:
        conn.execute(
            drafts.insert().values(
                lead_id=lead_id,
                version=2,
                subject="Offer\n\nHello,",
                body="Thanks.",
                sha256=paragraphs.sha256,
                created_by="llm",
            )
        )


def test_database_binds_approval_and_outbox_to_the_approved_text(clean_db: Engine) -> None:
    lead_id = new_lead(clean_db, email=unique_email())
    approved = Draft(subject="Offer", body="Hello,\n\nThanks.")
    other = Draft(subject="Other", body="Other body")
    with clean_db.begin() as conn:
        approvals.add_draft(conn, lead_id, approved, created_by="llm")
        draft_id = conn.execute(select(drafts.c.id).where(drafts.c.lead_id == lead_id)).scalar()

    # An approval can only carry the hash of the draft it points to.
    with pytest.raises(IntegrityError, match="fk_approvals_draft_id_drafts"):
        with clean_db.begin() as conn:
            conn.execute(
                approvals_table.insert().values(
                    lead_id=lead_id, draft_id=draft_id, draft_sha256=other.sha256, approved_by="x"
                )
            )
    with clean_db.begin() as conn:
        approval_id: UUID = conn.execute(
            approvals_table.insert()
            .values(
                lead_id=lead_id, draft_id=draft_id, draft_sha256=approved.sha256, approved_by="x"
            )
            .returning(approvals_table.c.id)
        ).scalar_one()

    def outbox_row(draft: Draft, sha256: str) -> dict[str, Any]:
        return {
            "lead_id": lead_id,
            "approval_id": approval_id,
            "draft_sha256": sha256,
            "message_id": f"<{sha256[:12]}@test>",
            "recipient": "someone@example.com",
            "subject": draft.subject,
            "body": draft.body,
        }

    resplit = {
        **outbox_row(approved, approved.sha256),
        "subject": "Offer\n\nHello,",
        "body": "Thanks.",
    }

    # The outbox text must hash to the approval's hash ...
    with pytest.raises(IntegrityError, match="text_matches_approval"), clean_db.begin() as conn:
        conn.execute(outbox.insert().values(**outbox_row(other, approved.sha256)))
    # ... and the hash must be the approval's own.
    with pytest.raises(IntegrityError, match="fk_outbox_approval_id_approvals"):
        with clean_db.begin() as conn:
            conn.execute(outbox.insert().values(**outbox_row(other, other.sha256)))
    # ... and the subject is one line, so the text cannot be split differently.
    with pytest.raises(IntegrityError, match="subject_one_line"), clean_db.begin() as conn:
        conn.execute(outbox.insert().values(**resplit))
    with clean_db.begin() as conn:
        conn.execute(outbox.insert().values(**outbox_row(approved, approved.sha256)))
    # One row per approved text: a second one for the same text is refused.
    second = {**outbox_row(approved, approved.sha256), "message_id": "<second@test>"}
    with pytest.raises(IntegrityError, match="uq_outbox_lead_id_draft_sha256"):
        with clean_db.begin() as conn:
            conn.execute(outbox.insert().values(**second))


def test_database_allows_one_active_approval_per_lead(clean_db: Engine) -> None:
    lead_id = new_lead(clean_db, email=unique_email())
    draft = Draft(subject="Subject", body="Body")
    with clean_db.begin() as conn:
        approvals.add_draft(conn, lead_id, draft, created_by="llm")
        draft_id = conn.execute(select(drafts.c.id).where(drafts.c.lead_id == lead_id)).scalar()
    row = {"lead_id": lead_id, "draft_id": draft_id, "draft_sha256": draft.sha256}
    with clean_db.begin() as conn:
        first: UUID = conn.execute(
            approvals_table.insert().values(**row, approved_by="a").returning(approvals_table.c.id)
        ).scalar_one()

    with pytest.raises(IntegrityError, match="uq_approvals_active_lead"), clean_db.begin() as conn:
        conn.execute(approvals_table.insert().values(**row, approved_by="b"))
    with clean_db.begin() as conn:  # once the first is revoked, a new one may be given
        approvals.revoke(conn, first, "draft_edited")
        conn.execute(approvals_table.insert().values(**row, approved_by="b"))


def test_edit_is_refused_once_sending_has_started(
    client: TestClient, clean_db: Engine, mailpit: Mailpit, smtp: SMTPMailer
) -> None:
    lead_id, email = lead_awaiting_approval(clean_db)
    approved = view(client, lead_id)["draft"]
    assert approve(client, lead_id, approved["sha256"]).status_code == 200
    # The first attempt records what it is sending, then SMTP fails for now.
    run_one_send_job(clean_db, ScriptedMailer(TransientSendError("smtp_451")))
    assert send_job(clean_db, lead_id).status == "queued"
    assert len(outbox_rows(clean_db, lead_id)) == 1

    response = edit(client, lead_id, "Too late", "Hello,\n\nToo late.\n\nThe Sales Team")

    assert response.status_code == 409
    assert view(client, lead_id)["draft"]["sha256"] == approved["sha256"]
    run_send_jobs(clean_db, smtp)  # the retry sends the approved text
    [mail] = mailpit.messages_to(email)
    assert mailpit.text(mail) == approved["body"]


# AC-3.3 ---------------------------------------------------------------------------------------


def test_approving_twice_sends_one_mail(
    client: TestClient, clean_db: Engine, mailpit: Mailpit, smtp: SMTPMailer
) -> None:
    lead_id, email = lead_awaiting_approval(clean_db)
    sha256 = view(client, lead_id)["draft"]["sha256"]

    assert approve(client, lead_id, sha256).status_code == 200
    assert approve(client, lead_id, sha256).status_code == 200  # a double click
    run_send_jobs(clean_db, smtp)
    assert approve(client, lead_id, sha256).status_code == 200  # again, after sending
    run_send_jobs(clean_db, smtp)

    assert len(approval_rows(clean_db, lead_id)) == 1
    assert len(mailpit.messages_to(email)) == 1


def test_parallel_approvals_make_one_approval_and_one_mail(
    live_server: str, clean_db: Engine, mailpit: Mailpit, smtp: SMTPMailer
) -> None:
    lead_id, email = lead_awaiting_approval(clean_db)
    sha256 = approvals.lead_view(clean_db, lead_id)["draft"]["sha256"]
    barrier = threading.Barrier(10)
    codes: list[int] = []

    def approve_once(i: int) -> None:
        with httpx2.Client(base_url=live_server) as http:
            barrier.wait()
            response = http.post(
                f"/v1/leads/{lead_id}/approve",
                json={"draft_sha256": sha256, "approved_by": f"reviewer{i}"},
            )
            codes.append(response.status_code)

    threads = [threading.Thread(target=approve_once, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    run_send_jobs(clean_db, smtp)

    assert codes == [200] * 10
    assert len(approval_rows(clean_db, lead_id)) == 1
    assert len(mailpit.messages_to(email)) == 1


def test_rerunning_the_send_job_after_sending_does_not_send_again(
    client: TestClient, clean_db: Engine, mailpit: Mailpit, smtp: SMTPMailer
) -> None:
    lead_id, email = lead_awaiting_approval(clean_db)
    assert approve(client, lead_id, view(client, lead_id)["draft"]["sha256"]).status_code == 200
    run_send_jobs(clean_db, smtp)

    attempt_send(clean_db, lead_id)  # the same send job, queued once more
    assert run_send_jobs(clean_db, smtp) == 1

    assert send_job(clean_db, lead_id).last_error == "skipped:already_sent"
    assert len(mailpit.messages_to(email)) == 1


def test_a_send_job_taken_over_after_a_crash_before_smtp_sends_once(
    client: TestClient, clean_db: Engine, mailpit: Mailpit, smtp: SMTPMailer
) -> None:
    lead_id, email = lead_awaiting_approval(clean_db)
    assert approve(client, lead_id, view(client, lead_id)["draft"]["sha256"]).status_code == 200
    stale = jobs.claim_next(clean_db, "crashed-worker", lease_s=300, kinds=SEND)
    assert stale is not None
    with clean_db.begin() as conn:
        conn.execute(text("UPDATE jobs SET locked_until = now() - interval '1 second'"))

    assert run_send_jobs(clean_db, smtp, worker_id="w2") == 1

    with clean_db.begin() as conn:  # the crashed worker comes back
        assert jobs.complete(conn, stale, "crashed-worker") is False
    assert len(mailpit.messages_to(email)) == 1
    assert lead_state(clean_db, lead_id) == ("sent", None)


def test_reapproval_during_a_send_attempt_does_not_hand_the_job_to_a_second_worker(
    client: TestClient, clean_db: Engine, mailpit: Mailpit, smtp: SMTPMailer
) -> None:
    lead_id, email = lead_awaiting_approval(clean_db)
    assert approve(client, lead_id, view(client, lead_id)["draft"]["sha256"]).status_code == 200
    # Worker A has claimed the send job but not yet looked at the lead.
    job_a = jobs.claim_next(clean_db, "A", lease_s=300, kinds=SEND)
    assert job_a is not None
    # Meanwhile the reviewer edits the draft (revoking the approval) and approves the new text.
    edited = edit(client, lead_id, "Edited", "Hello,\n\nEdited.\n\nThe Sales Team").json()
    assert approve(client, lead_id, edited["draft"]["sha256"]).status_code == 200
    second_worker_processed: list[int] = []

    def worker_b_runs() -> None:
        second_worker_processed.append(run_send_jobs(clean_db, smtp, worker_id="B"))

    process_job(
        clean_db, NO_LLM, settings(), job_a, "A", mailer=CallbackMailer(smtp, worker_b_runs)
    )

    assert second_worker_processed == [0]  # the job stayed with A
    [mail] = mailpit.messages_to(email)
    assert mailpit.text(mail) == edited["draft"]["body"]
    assert lead_state(clean_db, lead_id) == ("sent", None)


def test_a_record_of_sending_is_never_sent_again(
    client: TestClient, clean_db: Engine, mailpit: Mailpit, smtp: SMTPMailer
) -> None:
    # Not reachable through the code (the record and the status change commit together);
    # the guard holds anyway if the two ever disagree.
    lead_id, email = lead_awaiting_approval(clean_db)
    assert approve(client, lead_id, view(client, lead_id)["draft"]["sha256"]).status_code == 200
    run_send_jobs(clean_db, smtp)
    with clean_db.begin() as conn:
        conn.execute(update(leads).where(leads.c.id == lead_id).values(status="approved"))
    attempt_send(clean_db, lead_id)

    run_send_jobs(clean_db, smtp)

    assert send_job(clean_db, lead_id).last_error == "skipped:already_sent"
    assert lead_state(clean_db, lead_id) == ("sent", "already_sent")
    assert len(mailpit.messages_to(email)) == 1


# AC-3.4 ---------------------------------------------------------------------------------------


def test_rejected_lead_gets_nothing(
    client: TestClient, clean_db: Engine, mailpit: Mailpit, smtp: SMTPMailer
) -> None:
    lead_id, email = lead_awaiting_approval(clean_db)
    draft = view(client, lead_id)["draft"]

    response = client.post(
        f"/v1/leads/{lead_id}/reject", json={"rejected_by": "carol", "reason": "not a fit"}
    )

    assert response.status_code == 200
    assert lead_state(clean_db, lead_id) == ("rejected", "rejected by carol: not a fit")
    assert approve(client, lead_id, draft["sha256"]).status_code == 409
    assert edit(client, lead_id, "Still", "Hello,\n\nStill.\n\nThe Sales Team").status_code == 409
    attempt_send(clean_db, lead_id)
    run_send_jobs(clean_db, smtp)
    assert send_job(clean_db, lead_id).last_error == "refused:not_approved:rejected"
    assert outbox_rows(clean_db, lead_id) == []
    assert mailpit.messages_to(email) == []
    # Rejecting again changes nothing.
    again = client.post(f"/v1/leads/{lead_id}/reject", json={"rejected_by": "dave"})
    assert again.status_code == 200
    assert lead_state(clean_db, lead_id) == ("rejected", "rejected by carol: not a fit")


# AC-3.6: the known limit, shown rather than only described ------------------------------------


def test_known_limit_a_crash_between_smtp_and_the_record_sends_twice_with_one_message_id(
    client: TestClient, clean_db: Engine, mailpit: Mailpit, smtp: SMTPMailer
) -> None:
    lead_id, email = lead_awaiting_approval(clean_db)
    assert approve(client, lead_id, view(client, lead_id)["draft"]["sha256"]).status_code == 200

    with pytest.raises(ProcessDied):  # SMTP accepted the mail, then the worker died
        run_send_jobs(clean_db, DiesAfterSending(smtp), worker_id="A")
    with clean_db.begin() as conn:  # its lease runs out
        conn.execute(text("UPDATE jobs SET locked_until = now() - interval '1 second'"))
    run_send_jobs(clean_db, smtp, worker_id="B")

    mails = mailpit.messages_to(email)
    assert len(mails) == 2  # at-least-once: the retry could not know the first send succeeded
    assert len({m["MessageID"] for m in mails}) == 1  # a receiver can drop the duplicate
    assert lead_state(clean_db, lead_id) == ("sent", None)
    assert len(outbox_rows(clean_db, lead_id)) == 1


# Sending failures -----------------------------------------------------------------------------


def test_transient_smtp_failure_is_retried_with_the_same_message_id(
    client: TestClient, clean_db: Engine, mailpit: Mailpit, smtp: SMTPMailer
) -> None:
    lead_id, email = lead_awaiting_approval(clean_db)
    assert approve(client, lead_id, view(client, lead_id)["draft"]["sha256"]).status_code == 200
    mailer = ScriptedMailer(TransientSendError("smtp_421"), then=smtp)

    assert run_send_jobs(clean_db, mailer) == 2

    job = send_job(clean_db, lead_id)
    assert (job.status, job.attempts) == ("done", 2)
    [mail] = mailpit.messages_to(email)
    [record] = outbox_rows(clean_db, lead_id)
    assert f"<{mail['MessageID']}>" == record.message_id
    assert lead_state(clean_db, lead_id) == ("sent", None)


def test_sending_that_keeps_failing_ends_in_send_failed_with_one_alert(
    client: TestClient, clean_db: Engine, mailpit: Mailpit
) -> None:
    lead_id, email = lead_awaiting_approval(clean_db)
    assert approve(client, lead_id, view(client, lead_id)["draft"]["sha256"]).status_code == 200
    always = ScriptedMailer(*[TransientSendError("ConnectionRefusedError")] * 5)

    assert run_send_jobs(clean_db, always) == 5

    job = send_job(clean_db, lead_id)
    assert (job.status, job.attempts) == ("failed", 5)
    assert lead_state(clean_db, lead_id) == (
        "send_failed",
        "retries_exhausted:ConnectionRefusedError",
    )
    with clean_db.connect() as conn:
        alerts = conn.execute(
            text("SELECT kind, detail->>'job_kind' FROM alert_events WHERE lead_id = :id"),
            {"id": lead_id},
        ).all()
    assert [tuple(a) for a in alerts] == [("send_failed", "send_follow_up")]
    assert mailpit.messages_to(email) == []


def fail_recording_sent(monkeypatch: pytest.MonkeyPatch, times: int | None) -> None:
    """The database write that records a sent mail fails `times` times (None: every time)."""
    real = worker.transition
    failures = 0

    def flaky(
        conn: Any, lead_id: UUID, from_status: str, to_status: str, reason: Any = None
    ) -> None:
        nonlocal failures
        if to_status == "sent" and (times is None or failures < times):
            failures += 1
            raise OperationalError("UPDATE leads", None, ConnectionError("database went away"))
        real(conn, lead_id, from_status, to_status, reason)

    monkeypatch.setattr(worker, "transition", flaky)


def test_a_database_error_right_after_sending_is_retried_and_recorded(
    client: TestClient,
    clean_db: Engine,
    mailpit: Mailpit,
    smtp: SMTPMailer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lead_id, email = lead_awaiting_approval(clean_db)
    assert approve(client, lead_id, view(client, lead_id)["draft"]["sha256"]).status_code == 200
    fail_recording_sent(monkeypatch, times=1)

    run_send_jobs(clean_db, smtp)

    assert lead_state(clean_db, lead_id) == ("sent", None)
    assert send_job(clean_db, lead_id).status == "done"
    assert [r.sent_at is not None for r in outbox_rows(clean_db, lead_id)] == [True]
    assert len(mailpit.messages_to(email)) == 1


def test_a_mail_that_went_out_is_never_recorded_as_a_send_failure(
    client: TestClient,
    clean_db: Engine,
    mailpit: Mailpit,
    smtp: SMTPMailer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The SMTP server accepted the mail, and the database will not take the record. Marking
    # the lead send_failed would be false. The job is left to its lease instead, like a crash.
    lead_id, email = lead_awaiting_approval(clean_db)
    assert approve(client, lead_id, view(client, lead_id)["draft"]["sha256"]).status_code == 200
    fail_recording_sent(monkeypatch, times=None)

    run_send_jobs(clean_db, smtp, worker_id="A")

    assert len(mailpit.messages_to(email)) == 1
    assert lead_state(clean_db, lead_id) == ("approved", None)
    assert send_job(clean_db, lead_id).status == "running"
    with clean_db.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM alert_events")).scalar_one() == 0
    # The database is back and A's lease runs out: the next worker sends again (the known
    # limit in README), with the same Message-ID, and records it.
    monkeypatch.undo()
    with clean_db.begin() as conn:
        conn.execute(text("UPDATE jobs SET locked_until = now() - interval '1 second'"))
    run_send_jobs(clean_db, smtp, worker_id="B")
    mails = mailpit.messages_to(email)
    assert (len(mails), len({m["MessageID"] for m in mails})) == (2, 1)
    assert lead_state(clean_db, lead_id) == ("sent", None)


def test_a_permanent_smtp_failure_is_not_retried(client: TestClient, clean_db: Engine) -> None:
    lead_id, _ = lead_awaiting_approval(clean_db)
    assert approve(client, lead_id, view(client, lead_id)["draft"]["sha256"]).status_code == 200
    mailer = ScriptedMailer(PermanentSendError("smtp_550"))

    assert run_send_jobs(clean_db, mailer) == 1

    assert mailer.calls == 1
    assert lead_state(clean_db, lead_id) == ("send_failed", "send_error:smtp_550")


# The review API -------------------------------------------------------------------------------


def test_reading_a_lead_changes_nothing(client: TestClient, clean_db: Engine) -> None:
    lead_id, _ = lead_awaiting_approval(clean_db)

    def snapshot() -> tuple[Any, ...]:
        with clean_db.connect() as conn:
            return (
                conn.execute(select(leads).where(leads.c.id == lead_id)).one(),
                conn.execute(select(func.count()).select_from(approvals_table)).scalar_one(),
                conn.execute(select(func.count()).select_from(drafts)).scalar_one(),
                conn.execute(text("SELECT count(*) FROM jobs")).scalar_one(),
            )

    before = snapshot()
    for _ in range(3):
        view(client, lead_id)
    assert snapshot() == before


def test_unknown_lead_is_404_everywhere(client: TestClient, clean_db: Engine) -> None:
    missing = "00000000-0000-4000-8000-000000000000"
    sha256 = "0" * 64
    assert client.get(f"/v1/leads/{missing}").status_code == 404
    assert approve(client, UUID(missing), sha256).status_code == 404
    assert edit(client, UUID(missing), "S", "B").status_code == 404
    rejected = client.post(f"/v1/leads/{missing}/reject", json={"rejected_by": "x"})
    assert rejected.status_code == 404


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"draft_sha256": "abc", "approved_by": "alice"}, id="short-hash"),
        pytest.param({"draft_sha256": "G" * 64, "approved_by": "alice"}, id="not-hex"),
        pytest.param({"draft_sha256": "0" * 64}, id="no-reviewer"),
        pytest.param({"draft_sha256": "0" * 64, "approved_by": "a b"}, id="reviewer-with-space"),
        pytest.param(
            {"draft_sha256": "0" * 64, "approved_by": "alice", "force": True}, id="unknown-field"
        ),
    ],
)
def test_malformed_approval_is_422_and_changes_nothing(
    client: TestClient, clean_db: Engine, body: dict[str, Any]
) -> None:
    lead_id, _ = lead_awaiting_approval(clean_db)
    assert client.post(f"/v1/leads/{lead_id}/approve", json=body).status_code == 422
    assert lead_state(clean_db, lead_id) == ("awaiting_approval", None)


@pytest.mark.parametrize(
    ("subject", "body"),
    [
        pytest.param("Hi [Name]", "Hello,\n\nText.", id="placeholder-in-subject"),
        pytest.param("Hi", "Hello {{first_name}},\n\nText.", id="template-in-body"),
        pytest.param("Hi", "Hello,\x00\n\nText.", id="nul"),
        # U17 (F1): these were accepted, the control character dropped without a trace.
        pytest.param("Hi", "Hello,\x85\n\nText.", id="c1-at-a-line-end"),
        pytest.param("Hi", "Hello,\x1f\n\nText.", id="control-at-a-line-end"),
        pytest.param("Hi\x9b", "Hello,\n\nText.", id="c1-in-subject"),
        pytest.param("   ", "Hello", id="blank-subject"),
        pytest.param("Hi", " \n \n ", id="blank-body"),
    ],
)
def test_an_edit_that_is_not_ready_to_send_is_422(
    client: TestClient, clean_db: Engine, subject: str, body: str
) -> None:
    lead_id, _ = lead_awaiting_approval(clean_db)
    before = view(client, lead_id)["draft"]
    assert edit(client, lead_id, subject, body).status_code == 422
    assert view(client, lead_id)["draft"] == before


@pytest.mark.parametrize("reason", ["not a fit\x00", "not a fit\x07"], ids=["nul", "bell"])
def test_a_reject_reason_with_a_control_character_is_422_and_changes_nothing(
    client: TestClient, clean_db: Engine, reason: str
) -> None:
    """U17 (F2): a NUL here was a 500 (it could not be stored); a bell was stored."""
    lead_id, _ = lead_awaiting_approval(clean_db)
    response = client.post(
        f"/v1/leads/{lead_id}/reject", json={"rejected_by": "carol", "reason": reason}
    )
    assert response.status_code == 422
    assert "control character" in response.text
    assert lead_state(clean_db, lead_id) == ("awaiting_approval", None)


def test_an_edit_to_the_same_text_keeps_the_approval(
    client: TestClient, clean_db: Engine, mailpit: Mailpit, smtp: SMTPMailer
) -> None:
    lead_id, email = lead_awaiting_approval(clean_db)
    draft = view(client, lead_id)["draft"]
    assert approve(client, lead_id, draft["sha256"]).status_code == 200

    # Different whitespace, same text after normalization.
    response = edit(client, lead_id, f"  {draft['subject']} ", draft["body"] + "\n\n\n")

    assert response.status_code == 200
    assert (response.json()["status"], response.json()["draft"]["version"]) == ("approved", 1)
    run_send_jobs(clean_db, smtp)
    assert len(mailpit.messages_to(email)) == 1
