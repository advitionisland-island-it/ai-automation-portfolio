"""AC-3.5: every transition that is not an edge of the lead state machine is refused in code.

The expected edges are written out here, not read from states.py, so the test can disagree
with the code. Source: MASTER_TASK_P01 §3, plus D-61, D-62 and D-63 in DECISIONS.
"""

import itertools
from uuid import UUID

import pytest
from sqlalchemy import Engine, update

from sales_ops.states import InvalidTransition, transition
from sales_ops.tables import LEAD_STATUSES, leads
from tests.helpers import lead_state, new_lead, unique_email

EDGES = {
    ("received", "qualified"),
    ("received", "needs_review"),
    ("received", "processing_failed"),
    ("qualified", "awaiting_approval"),
    ("qualified", "needs_review"),
    ("qualified", "processing_failed"),  # D-61: drafting used up its retries
    ("awaiting_approval", "approved"),
    ("awaiting_approval", "rejected"),
    ("approved", "sent"),
    ("approved", "send_failed"),  # D-63: the send step fails before anything was sent
    ("approved", "awaiting_approval"),  # D-62: the approval was revoked (draft changed)
}
STATUSES = (
    "received",
    "qualified",
    "needs_review",
    "processing_failed",
    "awaiting_approval",
    "approved",
    "rejected",
    "sent",
    "send_failed",
)
ALL_PAIRS = list(itertools.product(STATUSES, STATUSES))


def test_the_table_covers_every_status_the_database_allows() -> None:
    assert set(STATUSES) == set(LEAD_STATUSES)


def lead_in(engine: Engine, status: str) -> UUID:
    lead_id = new_lead(engine, email=unique_email())
    with engine.begin() as conn:
        conn.execute(update(leads).where(leads.c.id == lead_id).values(status=status))
    return lead_id


@pytest.mark.parametrize(
    ("from_status", "to_status"), ALL_PAIRS, ids=[f"{a}->{b}" for a, b in ALL_PAIRS]
)
def test_transition_table(clean_db: Engine, from_status: str, to_status: str) -> None:
    lead_id = lead_in(clean_db, from_status)

    if (from_status, to_status) in EDGES:
        with clean_db.begin() as conn:
            transition(conn, lead_id, from_status, to_status, "test")
        assert lead_state(clean_db, lead_id) == (to_status, "test")
    else:
        with pytest.raises(InvalidTransition), clean_db.begin() as conn:
            transition(conn, lead_id, from_status, to_status, "test")
        assert lead_state(clean_db, lead_id) == (from_status, None)


def test_received_to_sent_is_refused(clean_db: Engine) -> None:
    # The example named in AC-3.5.
    lead_id = lead_in(clean_db, "received")
    with pytest.raises(InvalidTransition, match="received -> sent"), clean_db.begin() as conn:
        transition(conn, lead_id, "received", "sent")
    assert lead_state(clean_db, lead_id) == ("received", None)


@pytest.mark.parametrize(
    ("from_status", "to_status"), sorted(EDGES), ids=[f"{a}->{b}" for a, b in sorted(EDGES)]
)
def test_a_legal_edge_is_refused_when_the_lead_is_not_in_its_start_state(
    clean_db: Engine, from_status: str, to_status: str
) -> None:
    # Someone else moved the lead first: the conditional UPDATE notices instead of overwriting.
    elsewhere = next(s for s in STATUSES if s != from_status)
    lead_id = lead_in(clean_db, elsewhere)
    with pytest.raises(InvalidTransition, match="is not in status"), clean_db.begin() as conn:
        transition(conn, lead_id, from_status, to_status)
    assert lead_state(clean_db, lead_id) == (elsewhere, None)
