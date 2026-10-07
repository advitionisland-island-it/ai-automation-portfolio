"""The review page (D-13, D-66): HTML for the person who approves drafts."""

from typing import Any
from uuid import UUID

import httpx2
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select, text, update

from sales_ops import approvals, jobs
from sales_ops.drafting import Draft
from sales_ops.tables import approvals as approvals_table
from sales_ops.tables import drafts, leads
from tests.helpers import job_row, lead_awaiting_approval, lead_state


def page(client: TestClient, lead_id: UUID, **params: str) -> httpx2.Response:
    return client.get(f"/review/{lead_id}", params=params)


def post(client: TestClient, lead_id: UUID, action: str, **form: str) -> httpx2.Response:
    return client.post(f"/review/{lead_id}/{action}", data=form, follow_redirects=False)


def current_sha(client: TestClient, lead_id: UUID) -> str:
    sha256: str = client.get(f"/v1/leads/{lead_id}").json()["draft"]["sha256"]
    return sha256


def test_the_page_shows_what_a_reviewer_needs(client: TestClient, clean_db: Engine) -> None:
    lead_id, email = lead_awaiting_approval(clean_db)

    response = page(client, lead_id)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    body = response.text
    for expected in (
        "Acme KK",
        email,
        "awaiting_approval",
        "Thank you for your inquiry",
        "(Fake provider output: no real model was called.)",
        current_sha(client, lead_id),
        f'action="/review/{lead_id}/approve"',
        f'action="/review/{lead_id}/reject"',
        f'action="/review/{lead_id}/draft"',
    ):
        assert expected in body, expected
    csp = response.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in csp and "form-action 'self'" in csp
    assert response.headers["cache-control"] == "no-store"


def test_text_from_outside_is_escaped(client: TestClient, clean_db: Engine) -> None:
    lead_id, _ = lead_awaiting_approval(clean_db)
    hostile = "<script>alert(1)</script>"
    with clean_db.begin() as conn:
        conn.execute(update(leads).where(leads.c.id == lead_id).values(company=hostile))
        approvals.add_draft(
            conn,
            lead_id,
            Draft(subject=f"Hi {hostile}", body=f'Hello "><img src=x onerror=alert(2)>\n{hostile}'),
            created_by="llm",
        )

    body = page(client, lead_id).text

    assert "<script>" not in body
    assert "<img src=x" not in body
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body


def test_approving_from_the_page_approves_exactly_that_text(
    client: TestClient, clean_db: Engine
) -> None:
    lead_id, _ = lead_awaiting_approval(clean_db)
    sha256 = current_sha(client, lead_id)

    response = post(client, lead_id, "approve", draft_sha256=sha256, reviewer="alice")

    assert response.status_code == 303
    assert response.headers["location"] == f"/review/{lead_id}?result=approved"
    assert lead_state(clean_db, lead_id) == ("approved", None)
    assert job_row(clean_db, lead_id, jobs.JOB_SEND).status == "queued"
    with clean_db.connect() as conn:
        approval = conn.execute(select(approvals_table)).one()
    assert (approval.approved_by, approval.draft_sha256) == ("alice", sha256)
    assert "Approved. The worker sends it shortly." in page(client, lead_id, result="approved").text


def test_a_stale_page_cannot_approve(client: TestClient, clean_db: Engine) -> None:
    lead_id, _ = lead_awaiting_approval(clean_db)
    seen = current_sha(client, lead_id)
    edited = client.put(
        f"/v1/leads/{lead_id}/draft",
        json={"subject": "Changed", "body": "Hello,\n\nChanged.", "edited_by": "bob"},
    )
    assert edited.status_code == 200

    response = post(client, lead_id, "approve", draft_sha256=seen, reviewer="alice")

    assert response.status_code == 409
    assert "draft_sha256 is not the current draft" in response.text
    assert lead_state(clean_db, lead_id)[0] == "awaiting_approval"


def test_a_form_from_another_site_cannot_approve_or_reject(
    client: TestClient, clean_db: Engine
) -> None:
    # Another site can post a form to this page, but it cannot know the draft text.
    lead_id, _ = lead_awaiting_approval(clean_db)
    guessed = "0" * 64

    approve = post(client, lead_id, "approve", draft_sha256=guessed, reviewer="mallory")
    reject = post(client, lead_id, "reject", draft_sha256=guessed, reviewer="mallory")

    assert (approve.status_code, reject.status_code) == (409, 409)
    assert lead_state(clean_db, lead_id) == ("awaiting_approval", None)


def test_the_reviewer_name_is_checked(client: TestClient, clean_db: Engine) -> None:
    lead_id, _ = lead_awaiting_approval(clean_db)

    response = post(
        client, lead_id, "approve", draft_sha256=current_sha(client, lead_id), reviewer="a b"
    )

    assert response.status_code == 422
    assert "approved_by" in response.text
    assert lead_state(clean_db, lead_id)[0] == "awaiting_approval"


def test_rejecting_from_the_page(client: TestClient, clean_db: Engine) -> None:
    lead_id, _ = lead_awaiting_approval(clean_db)

    response = post(
        client,
        lead_id,
        "reject",
        draft_sha256=current_sha(client, lead_id),
        reviewer="carol",
        reason="not a fit",
    )

    assert response.status_code == 303
    assert lead_state(clean_db, lead_id) == ("rejected", "rejected by carol: not a fit")
    after = page(client, lead_id).text
    assert f'action="/review/{lead_id}/approve"' not in after  # no decision is offered any more
    assert f'action="/review/{lead_id}/draft"' not in after


def test_editing_from_the_page_makes_a_version_and_withdraws_the_approval(
    client: TestClient, clean_db: Engine
) -> None:
    lead_id, _ = lead_awaiting_approval(clean_db)
    sha256 = current_sha(client, lead_id)
    assert (
        post(client, lead_id, "approve", draft_sha256=sha256, reviewer="alice").status_code == 303
    )

    response = post(
        client,
        lead_id,
        "draft",
        draft_sha256=sha256,
        subject="A better subject",
        body="Hello,\r\n\r\nA better body.\r\n",
        reviewer="bob",
    )

    assert response.status_code == 303
    assert response.headers["location"] == f"/review/{lead_id}?result=edited"
    assert lead_state(clean_db, lead_id) == ("awaiting_approval", "approval_revoked:draft_edited")
    with clean_db.connect() as conn:
        latest = conn.execute(select(drafts).order_by(drafts.c.version.desc()).limit(1)).one()
    assert (latest.version, latest.created_by, latest.body) == (
        2,
        "human:bob",
        "Hello,\n\nA better body.",
    )


def test_an_edit_on_a_stale_page_is_refused(client: TestClient, clean_db: Engine) -> None:
    lead_id, _ = lead_awaiting_approval(clean_db)
    seen = current_sha(client, lead_id)
    first = post(
        client, lead_id, "draft", draft_sha256=seen, subject="First", body="Hi", reviewer="bob"
    )
    assert first.status_code == 303

    second = post(
        client, lead_id, "draft", draft_sha256=seen, subject="Second", body="Hi", reviewer="eve"
    )

    assert second.status_code == 409
    assert current_sha(client, lead_id) != seen
    assert client.get(f"/v1/leads/{lead_id}").json()["draft"]["subject"] == "First"


def test_an_edit_that_is_not_ready_to_send_is_refused(client: TestClient, clean_db: Engine) -> None:
    lead_id, _ = lead_awaiting_approval(clean_db)
    seen = current_sha(client, lead_id)

    response = post(
        client, lead_id, "draft", draft_sha256=seen, subject="Hi [Name]", body="Hi", reviewer="bob"
    )

    assert response.status_code == 422
    assert current_sha(client, lead_id) == seen


def test_an_unknown_lead_is_a_404_page(client: TestClient, clean_db: Engine) -> None:
    response = page(client, UUID("00000000-0000-4000-8000-000000000000"))
    assert response.status_code == 404
    assert "lead not found" in response.text


def test_reading_the_page_changes_nothing(client: TestClient, clean_db: Engine) -> None:
    lead_id, _ = lead_awaiting_approval(clean_db)

    def snapshot() -> tuple[Any, ...]:
        with clean_db.connect() as conn:
            return (
                conn.execute(select(leads).where(leads.c.id == lead_id)).one(),
                conn.execute(select(func.count()).select_from(approvals_table)).scalar_one(),
                conn.execute(select(func.count()).select_from(drafts)).scalar_one(),
                conn.execute(text("SELECT count(*), max(updated_at) FROM jobs")).one(),
            )

    before = snapshot()
    for result in ("", "approved", "rejected"):
        assert page(client, lead_id, result=result).status_code == 200
    assert snapshot() == before
