"""JSON request logs with request_id and masked email addresses (AC-1.6)."""

import io
import json
import logging

import httpx2
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from sales_ops.logging_config import configure_logging, mask_email
from sales_ops.main import app
from tests.helpers import VALID


def test_every_request_log_line_is_json_with_request_id_and_masked_email(
    clean_db: Engine,
) -> None:
    stream = io.StringIO()
    with TestClient(app) as client:
        configure_logging(stream=stream)  # after startup, so only request-time lines are captured
        responses = [
            client.get("/healthz"),
            client.post("/v1/leads", json=VALID, headers={"Idempotency-Key": "log-1"}),
            client.post("/v1/leads", json=VALID, headers={"Idempotency-Key": "log-1"}),
            client.post(
                "/v1/leads",
                json={**VALID, "message": "different"},
                headers={"Idempotency-Key": "log-1"},
            ),
            client.post(
                "/v1/leads", json={**VALID, "email": "bad"}, headers={"Idempotency-Key": "x"}
            ),
        ]

    raw = stream.getvalue()
    lines = [json.loads(line) for line in raw.splitlines()]  # every line must parse as JSON
    # The test client's own HTTP library also logs through the root logger; those lines
    # describe the client side of the call and are not request logs of the application.
    app_lines = [line for line in lines if not line["logger"].startswith("httpx")]

    assert [r.status_code for r in responses] == [200, 201, 201, 409, 422]
    assert app_lines, "no application log lines captured"
    assert all(line.get("request_id") for line in app_lines)
    completed = [line for line in app_lines if line["event"] == "request.completed"]
    assert len(completed) == len(responses)
    assert [line["request_id"] for line in completed] == [
        r.headers["X-Request-ID"] for r in responses
    ]
    assert len({line["request_id"] for line in completed}) == len(responses)

    assert "jane.doe@example.com" not in raw.lower()
    ingested = [line for line in app_lines if line["event"] == "lead.ingested"]
    assert [line["email"] for line in ingested] == ["J***@example.com"]


def test_incoming_request_id_is_kept_when_well_formed(client: TestClient) -> None:
    kept = client.get("/healthz", headers={"X-Request-ID": "n8n-exec-42"})
    replaced = client.get("/healthz", headers={"X-Request-ID": "bad id with spaces"})
    assert kept.headers["X-Request-ID"] == "n8n-exec-42"
    assert replaced.headers["X-Request-ID"] != "bad id with spaces"


def test_masking_applies_to_standard_library_loggers_too() -> None:
    stream = io.StringIO()
    configure_logging(stream=stream)
    logging.getLogger("third.party").warning("could not reach jane.doe@example.com")
    line = json.loads(stream.getvalue().splitlines()[-1])
    assert line["event"] == "could not reach j***@example.com"


def test_mask_email() -> None:
    assert mask_email("jane.doe@example.com") == "j***@example.com"
    assert (
        mask_email("to: a@b.example, x.y@sub.example.org")
        == "to: a***@b.example, x***@sub.example.org"
    )
    assert mask_email("no address here") == "no address here"


def test_real_server_logs_each_request_once_and_uvicorn_access_log_stays_off(
    live_server: str,
) -> None:
    # Request lines come from our middleware (with request_id). uvicorn's access log would
    # duplicate them and add the client address, so it must stay off even after the app's
    # lifespan reconfigures logging.
    stream = io.StringIO()
    configure_logging(stream=stream)
    with httpx2.Client(base_url=live_server, timeout=10) as http:
        response = http.get("/healthz")
    lines = [json.loads(line) for line in stream.getvalue().splitlines()]
    server_lines = [line for line in lines if not line["logger"].startswith("httpx")]

    assert response.status_code == 200
    assert [line["logger"] for line in server_lines].count("uvicorn.access") == 0
    completed = [line for line in server_lines if line["event"] == "request.completed"]
    assert [line["request_id"] for line in completed] == [response.headers["X-Request-ID"]]
