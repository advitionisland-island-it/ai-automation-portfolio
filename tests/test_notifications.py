"""Notifications n8n delivers (M4): claim a batch for a lease, send, mark delivered."""

import threading
from typing import Any
from uuid import UUID

import httpx2
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select, text

from sales_ops import jobs
from sales_ops.llm import TransientLLMError
from sales_ops.tables import review_requests
from sales_ops.worker import WorkerSettings, run_until_idle
from tests.fakes import ScriptedLLM
from tests.helpers import lead_awaiting_approval, new_lead, unique_email


def claim(client: TestClient, what: str, limit: int = 20) -> list[dict[str, Any]]:
    response = client.post(f"/v1/{what}/claim", params={"limit": limit})
    assert response.status_code == 200
    items: list[dict[str, Any]] = response.json()
    return items


def expire_claims(engine: Engine, table: str) -> None:
    # `table` is one of two fixed names from this module, never input.
    statement = f"UPDATE {table} SET claimed_until = now() - interval '1 second'"  # noqa: S608
    with engine.begin() as conn:
        conn.execute(text(statement))


def failed_lead(engine: Engine) -> UUID:
    lead_id = new_lead(engine, email=unique_email())
    settings = WorkerSettings(llm_provider="fake", worker_backoff_base_s=0)
    run_until_idle(
        engine, ScriptedLLM(TransientLLMError("timeout")), settings, "w1", kinds=[jobs.JOB_QUALIFY]
    )
    return lead_id


# Review requests (W2) -------------------------------------------------------------------------


def test_a_new_draft_asks_for_one_review(client: TestClient, clean_db: Engine) -> None:
    lead_id, _ = lead_awaiting_approval(clean_db)

    [item] = claim(client, "review-requests")

    assert item["lead_id"] == str(lead_id)
    assert item["review_url"] == f"http://127.0.0.1:8000/review/{lead_id}"
    assert (item["company"], item["draft_version"]) == ("Acme KK", 1)
    assert item["draft_subject"] == "Thank you for your inquiry"
    assert item["tier"] in ("hot", "warm", "cold") and isinstance(item["score"], int)


def test_a_claimed_request_is_not_handed_out_again_until_its_lease_ends(
    client: TestClient, clean_db: Engine
) -> None:
    lead_awaiting_approval(clean_db)
    [first] = claim(client, "review-requests")

    assert claim(client, "review-requests") == []  # still claimed
    expire_claims(clean_db, "review_requests")  # n8n stopped before marking it delivered
    [again] = claim(client, "review-requests")
    assert again["id"] == first["id"]


def test_a_delivered_request_is_never_handed_out_again(
    client: TestClient, clean_db: Engine
) -> None:
    lead_awaiting_approval(clean_db)
    [item] = claim(client, "review-requests")

    assert client.post(f"/v1/review-requests/{item['id']}/delivered").status_code == 204
    assert client.post(f"/v1/review-requests/{item['id']}/delivered").status_code == 204
    expire_claims(clean_db, "review_requests")
    assert claim(client, "review-requests") == []


def test_a_request_that_no_longer_needs_a_review_is_not_sent(
    client: TestClient, clean_db: Engine
) -> None:
    approved, _ = lead_awaiting_approval(clean_db)
    sha256 = client.get(f"/v1/leads/{approved}").json()["draft"]["sha256"]
    assert (
        client.post(
            f"/v1/leads/{approved}/approve", json={"draft_sha256": sha256, "approved_by": "a"}
        ).status_code
        == 200
    )
    edited, _ = lead_awaiting_approval(clean_db)  # the request is for draft 1; now draft 2
    assert (
        client.put(
            f"/v1/leads/{edited}/draft",
            json={"subject": "New", "body": "Hello,\n\nNew text.", "edited_by": "b"},
        ).status_code
        == 200
    )

    assert claim(client, "review-requests") == []


def test_parallel_claims_never_hand_out_the_same_request(
    live_server: str, clean_db: Engine
) -> None:
    for _ in range(12):
        lead_awaiting_approval(clean_db)
    seen: list[list[str]] = [[], []]
    barrier = threading.Barrier(2)

    def poll(slot: int) -> None:
        with httpx2.Client(base_url=live_server) as http:
            barrier.wait()
            while batch := http.post("/v1/review-requests/claim", params={"limit": 2}).json():
                seen[slot].extend(item["id"] for item in batch)

    threads = [threading.Thread(target=poll, args=(i,)) for i in (0, 1)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    everything = seen[0] + seen[1]
    assert len(everything) == len(set(everything)) == 12


@pytest.mark.parametrize("limit", [0, 51])
def test_the_batch_size_is_bounded(client: TestClient, clean_db: Engine, limit: int) -> None:
    assert client.post("/v1/review-requests/claim", params={"limit": limit}).status_code == 422
    assert client.post("/v1/alerts/claim", params={"limit": limit}).status_code == 422


def test_unknown_ids_are_404(client: TestClient, clean_db: Engine) -> None:
    missing = "00000000-0000-4000-8000-000000000000"
    assert client.post(f"/v1/review-requests/{missing}/delivered").status_code == 404
    assert client.post(f"/v1/alerts/{missing}/delivered").status_code == 404


def test_one_review_request_per_draft(clean_db: Engine) -> None:
    lead_id, _ = lead_awaiting_approval(clean_db)
    with clean_db.connect() as conn:
        count = conn.execute(
            select(func.count())
            .select_from(review_requests)
            .where(review_requests.c.lead_id == lead_id)
        ).scalar_one()
    assert count == 1


# Alerts (W3) ----------------------------------------------------------------------------------


def test_a_failed_lead_raises_an_alert_that_is_delivered_once(
    client: TestClient, clean_db: Engine
) -> None:
    lead_id = failed_lead(clean_db)

    [alert] = claim(client, "alerts")

    assert (alert["kind"], alert["lead_id"], alert["job_kind"]) == (
        "processing_failed",
        str(lead_id),
        "qualify_lead",
    )
    assert (alert["reason"], alert["attempts"]) == ("retries_exhausted:timeout", 5)
    assert alert["lead_url"] == f"http://127.0.0.1:8000/review/{lead_id}"
    assert client.post(f"/v1/alerts/{alert['id']}/delivered").status_code == 204
    expire_claims(clean_db, "alert_events")
    assert claim(client, "alerts") == []
