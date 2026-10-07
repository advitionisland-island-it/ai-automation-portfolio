"""Notifications that n8n delivers (M4): review requests (W2) and alerts (W3).

Python writes each one in the same transaction as the state change that causes it. n8n polls:
it claims a batch for a short lease, sends the emails, and marks each one delivered. A claim that
is never marked delivered (n8n stopped half-way) is handed out again when its lease runs out,
so delivery is at-least-once. FOR UPDATE SKIP LOCKED keeps two overlapping polls from claiming
the same row.
"""

from datetime import timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import Engine, Table, and_, exists, func, or_, select, update

from sales_ops.tables import alert_events, drafts, lead_assessments, leads, review_requests

CLAIM_LEASE_S = 60


def _claimable(table: Table) -> Any:
    return and_(
        table.c.delivered_at.is_(None),
        or_(table.c.claimed_until.is_(None), table.c.claimed_until < func.now()),
    )


def _claim(engine: Engine, table: Table, where: Any, limit: int, lease_s: int) -> list[UUID]:
    with engine.begin() as conn:
        ids: list[UUID] = list(
            conn.execute(
                select(table.c.id)
                .where(where)
                .order_by(table.c.created_at)
                .limit(limit)
                .with_for_update(skip_locked=True)
            ).scalars()
        )
        if ids:
            conn.execute(
                update(table)
                .where(table.c.id.in_(ids))
                .values(claimed_until=func.now() + timedelta(seconds=lease_s))
            )
    return ids


def claim_review_requests(
    engine: Engine, limit: int, base_url: str, lease_s: int = CLAIM_LEASE_S
) -> list[dict[str, Any]]:
    """Review requests still worth sending: the lead still waits for approval of that draft."""
    current_draft = (
        select(drafts.c.id)
        .where(drafts.c.lead_id == review_requests.c.lead_id)
        .order_by(drafts.c.version.desc())
        .limit(1)
        .scalar_subquery()
    )
    still_waiting = exists().where(
        leads.c.id == review_requests.c.lead_id, leads.c.status == "awaiting_approval"
    )
    where = and_(
        _claimable(review_requests), still_waiting, review_requests.c.draft_id == current_draft
    )
    ids = _claim(engine, review_requests, where, limit, lease_s)
    if not ids:
        return []
    with engine.connect() as conn:
        rows = conn.execute(
            select(
                review_requests.c.id,
                review_requests.c.lead_id,
                review_requests.c.created_at,
                leads.c.company,
                drafts.c.version,
                drafts.c.subject,
                lead_assessments.c.tier,
                lead_assessments.c.score,
                lead_assessments.c.summary,
            )
            .join(leads, leads.c.id == review_requests.c.lead_id)
            .join(drafts, drafts.c.id == review_requests.c.draft_id)
            .outerjoin(lead_assessments, lead_assessments.c.lead_id == review_requests.c.lead_id)
            .where(review_requests.c.id.in_(ids))
            .order_by(review_requests.c.created_at)
        ).all()
    return [
        {
            "id": row.id,
            "lead_id": row.lead_id,
            "company": row.company,
            "tier": row.tier,
            "score": row.score,
            "summary": row.summary,
            "draft_version": row.version,
            "draft_subject": row.subject,
            "review_url": f"{base_url}/review/{row.lead_id}",
            "created_at": row.created_at,
        }
        for row in rows
    ]


def claim_alerts(
    engine: Engine, limit: int, base_url: str, lease_s: int = CLAIM_LEASE_S
) -> list[dict[str, Any]]:
    ids = _claim(engine, alert_events, _claimable(alert_events), limit, lease_s)
    if not ids:
        return []
    with engine.connect() as conn:
        rows = conn.execute(
            select(alert_events, leads.c.company)
            .outerjoin(leads, leads.c.id == alert_events.c.lead_id)
            .where(alert_events.c.id.in_(ids))
            .order_by(alert_events.c.created_at)
        ).all()
    return [
        {
            "id": row.id,
            "kind": row.kind,
            "lead_id": row.lead_id,
            "company": row.company,
            "job_kind": row.detail.get("job_kind"),
            "reason": row.detail.get("reason"),
            "attempts": row.detail.get("attempts"),
            "lead_url": f"{base_url}/review/{row.lead_id}" if row.lead_id else None,
            "created_at": row.created_at,
        }
        for row in rows
    ]


def _mark_delivered(engine: Engine, table: Table, item_id: UUID) -> bool:
    """True if the row exists. Marking it again changes nothing."""
    with engine.begin() as conn:
        found = conn.execute(select(table.c.id).where(table.c.id == item_id)).first()
        conn.execute(
            update(table)
            .where(table.c.id == item_id, table.c.delivered_at.is_(None))
            .values(delivered_at=func.now())
        )
    return found is not None


def mark_review_request_delivered(engine: Engine, request_id: UUID) -> bool:
    return _mark_delivered(engine, review_requests, request_id)


def mark_alert_delivered(engine: Engine, alert_id: UUID) -> bool:
    return _mark_delivered(engine, alert_events, alert_id)
