"""Lead state machine (MASTER_TASK_P01 §3). The only code path that changes leads.status."""

from uuid import UUID

from sqlalchemy import Connection, func, update

from sales_ops.tables import leads

# Edges drawn in MASTER_TASK_P01 §3, plus two recorded in DECISIONS: qualified ->
# processing_failed (drafting used up its retries, D-61) and approved -> awaiting_approval (the
# approval was revoked because the draft changed, D-62). send_failed is reached from approved,
# when the send step fails (D-63). Anything else is rejected (AC-3.5).
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "received": frozenset({"qualified", "needs_review", "processing_failed"}),
    "qualified": frozenset({"awaiting_approval", "needs_review", "processing_failed"}),
    "awaiting_approval": frozenset({"approved", "rejected"}),
    "approved": frozenset({"sent", "send_failed", "awaiting_approval"}),
}


class InvalidTransition(Exception):
    pass


def transition(
    conn: Connection, lead_id: UUID, from_status: str, to_status: str, reason: str | None = None
) -> None:
    """Move a lead from `from_status` to `to_status`, or raise InvalidTransition.

    The UPDATE is conditional on the current status, so a concurrent change is detected
    instead of silently overwritten.
    """
    if to_status not in ALLOWED_TRANSITIONS.get(from_status, frozenset()):
        raise InvalidTransition(f"{from_status} -> {to_status} is not allowed")
    result = conn.execute(
        update(leads)
        .where(leads.c.id == lead_id, leads.c.status == from_status)
        .values(status=to_status, status_reason=reason, updated_at=func.now())
    )
    if result.rowcount != 1:
        raise InvalidTransition(f"lead {lead_id} is not in status {from_status}")
