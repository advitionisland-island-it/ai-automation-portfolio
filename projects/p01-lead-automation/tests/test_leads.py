"""POST /v1/leads: validation, idempotency and duplicate prevention (AC-1.1 to AC-1.5)."""

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx2
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select

from sales_ops.tables import idempotency_keys, inquiries
from tests.helpers import VALID, row_counts


def post(client: TestClient, body: dict[str, Any], key: str | None) -> httpx2.Response:
    headers = {} if key is None else {"Idempotency-Key": key}
    return client.post("/v1/leads", json=body, headers=headers)


def stored_response(engine: Engine, key: str) -> tuple[Any, ...]:
    with engine.connect() as conn:
        row = conn.execute(
            select(
                idempotency_keys.c.request_sha256,
                idempotency_keys.c.response_status,
                idempotency_keys.c.response_body,
            ).where(idempotency_keys.c.key == key)
        ).one()
    return tuple(row)


# AC-1.1 ---------------------------------------------------------------------------------------


def _without(field: str) -> dict[str, Any]:
    return {k: v for k, v in VALID.items() if k != field}


@pytest.mark.parametrize(
    ("body", "key"),
    [
        pytest.param(_without("email"), "k", id="email-missing"),
        pytest.param({**VALID, "email": "not-an-email"}, "k", id="email-malformed"),
        pytest.param({**VALID, "email": "a" * 65 + "@example.com"}, "k", id="email-local-too-long"),
        pytest.param(
            {
                **VALID,
                "email": "a" * 64 + "@" + "b" * 63 + "." + "c" * 63 + "." + "d" * 60 + ".com",
            },
            "k",
            id="email-total-too-long",
        ),
        pytest.param({**VALID, "name": "x" * 201}, "k", id="name-too-long"),
        pytest.param({**VALID, "company": "x" * 201}, "k", id="company-too-long"),
        pytest.param({**VALID, "message": "x" * 5001}, "k", id="message-too-long"),
        pytest.param({**VALID, "message": "   "}, "k", id="message-blank"),
        pytest.param({**VALID, "unexpected": "field"}, "k", id="unknown-field"),
        # U17 (F2): a NUL was a 500; the other control characters were stored.
        pytest.param({**VALID, "message": "hello\x00there"}, "k", id="nul-in-message"),
        pytest.param({**VALID, "name": "Jane\x00Doe"}, "k", id="nul-in-name"),
        pytest.param({**VALID, "company": "Acme\x00KK"}, "k", id="nul-in-company"),
        pytest.param({**VALID, "message": "hello\x07there"}, "k", id="bell-in-message"),
        pytest.param({**VALID, "message": "hello\x85there"}, "k", id="c1-in-message"),
        pytest.param({**VALID, "name": "Jane Doe\x1f"}, "k", id="control-at-the-end-of-name"),
        # Whitespace stripping would remove a trailing NEL before a later check could see it.
        pytest.param({**VALID, "name": "Jane Doe\x85"}, "k", id="c1-at-the-end-of-name"),
        pytest.param(VALID, None, id="idempotency-key-missing"),
        pytest.param(VALID, "", id="idempotency-key-empty"),
        pytest.param(VALID, "k" * 256, id="idempotency-key-too-long"),
    ],
)
def test_invalid_request_is_rejected_and_changes_nothing(
    client: TestClient, clean_db: Engine, body: dict[str, Any], key: str | None
) -> None:
    before = row_counts(clean_db)
    response = post(client, body, key)
    assert response.status_code == 422
    assert row_counts(clean_db) == before


def test_the_422_names_the_control_character(client: TestClient, clean_db: Engine) -> None:
    response = post(client, {**VALID, "message": "hello\x00there"}, "k")
    assert response.status_code == 422
    assert "U+0000" in response.text


def test_tabs_and_line_breaks_in_a_message_are_kept(client: TestClient, clean_db: Engine) -> None:
    message = "line one\r\n\tline two\nline three"
    response = post(client, {**VALID, "message": message}, "k")
    assert response.status_code == 201
    with clean_db.connect() as conn:
        stored: str = conn.execute(select(inquiries.c.message)).scalar_one()
    assert stored == message


# AC-1.2 ---------------------------------------------------------------------------------------


def test_same_key_and_body_twice_creates_one_lead_and_returns_same_lead_id(
    client: TestClient, clean_db: Engine
) -> None:
    first = post(client, VALID, "key-same")
    second = post(client, VALID, "key-same")

    assert first.status_code == 201
    assert second.status_code == 201
    assert second.json()["lead_id"] == first.json()["lead_id"]
    assert second.json() == first.json()
    assert "Idempotent-Replayed" not in first.headers
    assert second.headers["Idempotent-Replayed"] == "true"
    assert row_counts(clean_db) == {"leads": 1, "inquiries": 1, "idempotency_keys": 1}


def test_replay_ignores_key_order_and_surrounding_whitespace(
    client: TestClient, clean_db: Engine
) -> None:
    first = post(client, VALID, "key-canonical")
    reordered = {k: VALID[k] for k in reversed(list(VALID))}
    reordered["name"] = f"  {VALID['name']}  "
    second = post(client, reordered, "key-canonical")

    assert second.status_code == 201
    assert second.headers["Idempotent-Replayed"] == "true"
    assert second.json() == first.json()
    assert row_counts(clean_db) == {"leads": 1, "inquiries": 1, "idempotency_keys": 1}


# AC-1.3 ---------------------------------------------------------------------------------------


def test_same_key_with_different_body_is_409_and_changes_nothing(
    client: TestClient, clean_db: Engine
) -> None:
    assert post(client, VALID, "key-conflict").status_code == 201
    before_counts = row_counts(clean_db)
    before_stored = stored_response(clean_db, "key-conflict")

    conflict = post(client, {**VALID, "message": "A different message"}, "key-conflict")

    assert conflict.status_code == 409
    assert row_counts(clean_db) == before_counts
    assert stored_response(clean_db, "key-conflict") == before_stored


# AC-1.4 ---------------------------------------------------------------------------------------


def _post_in_parallel(
    base_url: str, bodies_and_keys: list[tuple[dict[str, Any], str]]
) -> list[httpx2.Response]:
    barrier = threading.Barrier(len(bodies_and_keys))

    def send(item: tuple[dict[str, Any], str]) -> httpx2.Response:
        body, key = item
        with httpx2.Client(base_url=base_url, timeout=30) as http:
            barrier.wait()  # release all requests at the same moment
            return http.post("/v1/leads", json=body, headers={"Idempotency-Key": key})

    with ThreadPoolExecutor(max_workers=len(bodies_and_keys)) as pool:
        return list(pool.map(send, bodies_and_keys))


def test_twenty_parallel_identical_requests_create_one_lead(
    live_server: str, clean_db: Engine
) -> None:
    responses = _post_in_parallel(live_server, [(VALID, "key-parallel")] * 20)

    assert [r.status_code for r in responses] == [201] * 20
    assert len({r.json()["lead_id"] for r in responses}) == 1
    assert sum(r.headers.get("Idempotent-Replayed") != "true" for r in responses) == 1
    assert row_counts(clean_db) == {"leads": 1, "inquiries": 1, "idempotency_keys": 1}


def test_twenty_parallel_requests_same_email_different_keys_share_one_lead(
    live_server: str, clean_db: Engine
) -> None:
    # Property beyond AC-1.4: the email UNIQUE constraint also holds under concurrency.
    items = [({**VALID, "message": f"inquiry {i}"}, f"key-{i}") for i in range(20)]
    responses = _post_in_parallel(live_server, items)

    assert [r.status_code for r in responses] == [201] * 20
    assert len({r.json()["lead_id"] for r in responses}) == 1
    assert sum(r.json()["lead_created"] for r in responses) == 1
    assert row_counts(clean_db) == {"leads": 1, "inquiries": 20, "idempotency_keys": 20}


# AC-1.5 ---------------------------------------------------------------------------------------


def test_new_key_same_normalized_email_attaches_to_existing_lead(
    client: TestClient, clean_db: Engine
) -> None:
    first = post(client, VALID, "key-a")
    follow_up = {**VALID, "email": "  jane.doe@EXAMPLE.com ", "message": "One more question"}
    second = post(client, follow_up, "key-b")

    assert first.status_code == 201
    assert second.status_code == 201
    assert second.json()["lead_id"] == first.json()["lead_id"]
    assert first.json()["lead_created"] is True
    assert second.json()["lead_created"] is False
    assert second.json()["inquiry_id"] != first.json()["inquiry_id"]
    assert row_counts(clean_db) == {"leads": 1, "inquiries": 2, "idempotency_keys": 2}
