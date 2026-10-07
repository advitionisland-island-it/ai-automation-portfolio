"""Human review of follow-up drafts (AC-3.1 to AC-3.4): approve, reject, edit.

Every operation starts by locking the lead row (SELECT ... FOR UPDATE). The send step takes the
same lock before it records what it is about to send, so for one lead, approving, editing and
the start of sending happen one after another, never interleaved.

An approval is bound to the sha256 of the exact draft text that was reviewed (D-12). If the
draft changes after approval, the approval is revoked and the lead waits for a new approval.
"""

from typing import Any
from uuid import UUID

from sqlalchemy import Connection, Engine, Row, func, insert, select, update

from sales_ops.drafting import Draft, normalize
from sales_ops.jobs import JOB_SEND, enqueue
from sales_ops.states import transition
from sales_ops.tables import approvals, drafts, lead_assessments, leads, outbox
from sales_ops.untrusted import Rejected


class ReviewError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def lock_lead(conn: Connection, lead_id: UUID) -> Row[Any]:
    lead = conn.execute(
        select(leads.c.id, leads.c.status, leads.c.email_normalized)
        .where(leads.c.id == lead_id)
        .with_for_update()
    ).first()
    if lead is None:
        raise ReviewError(404, "lead not found")
    return lead


def current_draft(conn: Connection, lead_id: UUID) -> Row[Any] | None:
    return conn.execute(
        select(drafts).where(drafts.c.lead_id == lead_id).order_by(drafts.c.version.desc()).limit(1)
    ).first()


def active_approval(conn: Connection, lead_id: UUID) -> Row[Any] | None:
    return conn.execute(
        select(approvals).where(approvals.c.lead_id == lead_id, approvals.c.revoked_at.is_(None))
    ).first()


def add_draft(
    conn: Connection,
    lead_id: UUID,
    draft: Draft,
    created_by: str,
    llm_call_id: UUID | None = None,
) -> UUID:
    """Store `draft` as the lead's next version and return its id."""
    version = (
        conn.execute(
            select(func.coalesce(func.max(drafts.c.version), 0)).where(drafts.c.lead_id == lead_id)
        ).scalar_one()
        + 1
    )
    draft_id: UUID = conn.execute(
        insert(drafts)
        .values(
            lead_id=lead_id,
            version=version,
            subject=draft.subject,
            body=draft.body,
            sha256=draft.sha256,
            created_by=created_by,
            llm_call_id=llm_call_id,
        )
        .returning(drafts.c.id)
    ).scalar_one()
    return draft_id


def revoke(conn: Connection, approval_id: UUID, reason: str) -> None:
    conn.execute(
        update(approvals)
        .where(approvals.c.id == approval_id, approvals.c.revoked_at.is_(None))
        .values(revoked_at=func.now(), revoke_reason=reason)
    )


def approve(engine: Engine, lead_id: UUID, draft_sha256: str, approved_by: str) -> None:
    with engine.begin() as conn:
        lead = lock_lead(conn, lead_id)
        draft = current_draft(conn, lead_id)
        if lead.status in ("approved", "sent"):
            approval = active_approval(conn, lead_id)
            if approval is not None and approval.draft_sha256 == draft_sha256:
                return  # this exact text is already approved: approving again changes nothing
            raise ReviewError(409, f"lead is already {lead.status} with a different draft")
        if lead.status != "awaiting_approval":
            raise ReviewError(409, f"lead is {lead.status}, not awaiting_approval")
        if draft is None:
            raise ReviewError(409, "lead has no draft")
        if draft.sha256 != draft_sha256:
            raise ReviewError(
                409,
                "draft_sha256 is not the current draft's (it may have changed since it was read)",
            )
        conn.execute(
            insert(approvals).values(
                lead_id=lead_id,
                draft_id=draft.id,
                draft_sha256=draft.sha256,
                approved_by=approved_by,
            )
        )
        transition(conn, lead_id, "awaiting_approval", "approved")
        enqueue(conn, JOB_SEND, lead_id, requeue=True)


def _check_seen(conn: Connection, lead_id: UUID, seen_sha256: str | None) -> None:
    """With `seen_sha256` (the review page sends it), refuse if the draft changed since then."""
    if seen_sha256 is None:
        return
    draft = current_draft(conn, lead_id)
    if draft is None or draft.sha256 != seen_sha256:
        raise ReviewError(409, "the draft changed since it was read; reload and review it again")


def reject(
    engine: Engine,
    lead_id: UUID,
    rejected_by: str,
    reason: str,
    seen_sha256: str | None = None,
) -> None:
    with engine.begin() as conn:
        lead = lock_lead(conn, lead_id)
        if lead.status == "rejected":
            return  # rejecting again changes nothing
        if lead.status != "awaiting_approval":
            raise ReviewError(409, f"lead is {lead.status}, not awaiting_approval")
        _check_seen(conn, lead_id, seen_sha256)
        note = f"rejected by {rejected_by}" + (f": {reason}" if reason else "")
        transition(conn, lead_id, "awaiting_approval", "rejected", note[:500])


def edit_draft(
    engine: Engine,
    lead_id: UUID,
    subject: str,
    body: str,
    edited_by: str,
    seen_sha256: str | None = None,
) -> None:
    edited = normalize(subject, body)
    if isinstance(edited, Rejected):
        raise ReviewError(
            422,
            "the draft is empty, or contains a placeholder such as [Name] or a control character",
        )
    with engine.begin() as conn:
        lead = lock_lead(conn, lead_id)
        if lead.status not in ("awaiting_approval", "approved"):
            raise ReviewError(409, f"lead is {lead.status}; its draft can no longer change")
        _check_seen(conn, lead_id, seen_sha256)
        draft = current_draft(conn, lead_id)
        if draft is not None and draft.sha256 == edited.sha256:
            return  # same text: nothing to change, and an approval of it stays valid
        if lead.status == "approved":
            approval = active_approval(conn, lead_id)
            if approval is not None:
                started = conn.execute(
                    select(outbox.c.id).where(
                        outbox.c.lead_id == lead_id,
                        outbox.c.draft_sha256 == approval.draft_sha256,
                    )
                ).first()
                if started is not None:
                    raise ReviewError(409, "sending has started; the draft can no longer change")
                revoke(conn, approval.id, "draft_edited")
            transition(
                conn, lead_id, "approved", "awaiting_approval", "approval_revoked:draft_edited"
            )
        add_draft(conn, lead_id, edited, created_by=f"human:{edited_by}")


def lead_view(engine: Engine, lead_id: UUID) -> dict[str, Any]:
    """Everything a reviewer needs to decide: status, assessment, current draft, approval."""
    with engine.connect() as conn:
        lead = conn.execute(select(leads).where(leads.c.id == lead_id)).first()
        if lead is None:
            raise ReviewError(404, "lead not found")
        assessment = conn.execute(
            select(lead_assessments).where(lead_assessments.c.lead_id == lead_id)
        ).first()
        draft = current_draft(conn, lead_id)
        approval = active_approval(conn, lead_id)
        sent = conn.execute(
            select(outbox.c.sent_at, outbox.c.message_id)
            .where(outbox.c.lead_id == lead_id, outbox.c.sent_at.is_not(None))
            .order_by(outbox.c.sent_at.desc())
            .limit(1)
        ).first()
    return {
        "lead_id": lead.id,
        "status": lead.status,
        "status_reason": lead.status_reason,
        "recipient": lead.email_normalized,
        "company": lead.company,
        "assessment": None
        if assessment is None
        else {
            "score": assessment.score,
            "tier": assessment.tier,
            "summary": assessment.summary,
            "reasons": assessment.reasons,
            "confidence": assessment.confidence,
        },
        "draft": None
        if draft is None
        else {
            "version": draft.version,
            "subject": draft.subject,
            "body": draft.body,
            "sha256": draft.sha256,
            "created_by": draft.created_by,
        },
        "approval": None
        if approval is None
        else {
            "approved_by": approval.approved_by,
            "approved_at": approval.approved_at,
            "draft_sha256": approval.draft_sha256,
        },
        "sent": None if sent is None else {"sent_at": sent.sent_at, "message_id": sent.message_id},
    }
