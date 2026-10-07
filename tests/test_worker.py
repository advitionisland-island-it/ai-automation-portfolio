"""The qualification worker on the real PostgreSQL with a scripted fake LLM (AC-2.1 to 2.6)."""

import json
import threading
import time
from collections import Counter
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select, text
from sqlalchemy.exc import IntegrityError

from sales_ops import jobs
from sales_ops.llm import (
    LLMClient,
    LLMRequest,
    LLMResponse,
    PermanentLLMError,
    TransientKind,
    TransientLLMError,
)
from sales_ops.qualification import PROMPT_VERSION
from sales_ops.tables import jobs as jobs_table
from sales_ops.tables import lead_assessments, llm_calls
from sales_ops.worker import WorkerSettings, run_until_idle
from tests.fakes import ScriptedLLM, output, raw, text_of
from tests.helpers import (
    VALID,
    alert_count,
    assessment_rows,
    call_rows,
    job_row,
    lead_state,
    new_lead,
)


def settings(**overrides: Any) -> WorkerSettings:
    values: dict[str, Any] = {"llm_provider": "fake", "worker_backoff_base_s": 0, **overrides}
    return WorkerSettings(**values)


def qualify_until_idle(engine: Engine, llm: LLMClient, s: WorkerSettings, worker_id: str) -> int:
    """Qualification jobs only: these tests end where drafting (M3) begins."""
    return run_until_idle(engine, llm, s, worker_id, kinds=[jobs.JOB_QUALIFY])


# AC-2.1 ---------------------------------------------------------------------------------------


def test_valid_output_is_validated_normalized_and_persisted(clean_db: Engine) -> None:
    lead_id = new_lead(clean_db)
    llm = ScriptedLLM(
        output(
            score=82,
            reasons=[
                "  Clear budget and timeline. ",
                "clear budget and timeline.",
                "Fits  segment.",
            ],
            confidence=0.87654,
        )
    )

    assert qualify_until_idle(clean_db, llm, settings(), "w1") == 1

    [assessment] = assessment_rows(clean_db, lead_id)
    assert assessment.score == 82
    assert assessment.tier == "hot"
    assert assessment.reasons == ["Clear budget and timeline.", "Fits segment."]
    assert assessment.confidence == 0.877
    assert assessment.prompt_version == PROMPT_VERSION
    assert lead_state(clean_db, lead_id) == ("qualified", None)
    assert job_row(clean_db, lead_id).status == "done"


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"score": 999}, id="score"),
        pytest.param({"tier": "SUPER_HIGH"}, id="tier"),
        pytest.param({"reasons": []}, id="reasons-empty"),
        pytest.param({"confidence": 1.5}, id="confidence"),
    ],
)
def test_database_rejects_invalid_assessment_even_if_code_let_it_through(
    clean_db: Engine, overrides: dict[str, Any]
) -> None:
    # Defense in depth: the CHECK constraints hold even if a bug bypassed the pipeline.
    lead_id = new_lead(clean_db)
    qualify_until_idle(clean_db, ScriptedLLM(raw("not json")), settings(), "w1")
    [call] = call_rows(clean_db, lead_id)
    values: dict[str, Any] = {
        "lead_id": lead_id,
        "llm_call_id": call.id,
        "prompt_version": PROMPT_VERSION,
        "score": 50,
        "tier": "warm",
        "reasons": ["ok"],
        "summary": "ok",
        "confidence": 0.5,
    }
    values.update(overrides)
    with pytest.raises(IntegrityError), clean_db.begin() as conn:
        conn.execute(lead_assessments.insert().values(**values))


# AC-2.2 ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["rate_limit", "server_error", "timeout", "connection"])
def test_transient_failures_are_retried_until_success(
    clean_db: Engine, kind: TransientKind
) -> None:
    lead_id = new_lead(clean_db)
    llm = ScriptedLLM(TransientLLMError(kind), TransientLLMError(kind), output())

    assert qualify_until_idle(clean_db, llm, settings(), "w1") == 3

    assert lead_state(clean_db, lead_id) == ("qualified", None)
    job = job_row(clean_db, lead_id)
    assert (job.status, job.attempts) == ("done", 3)
    calls = call_rows(clean_db, lead_id)
    assert [(c.attempt, c.outcome, c.error_kind) for c in calls] == [
        (1, "transient_error", kind),
        (2, "transient_error", kind),
        (3, "ok", None),
    ]
    assert len(assessment_rows(clean_db, lead_id)) == 1


def test_retry_waits_for_the_backoff(clean_db: Engine) -> None:
    lead_id = new_lead(clean_db)
    llm = ScriptedLLM(TransientLLMError("rate_limit"), output())

    assert qualify_until_idle(clean_db, llm, settings(worker_backoff_base_s=30), "w1") == 1

    job = job_row(clean_db, lead_id)
    assert (job.status, job.attempts, job.last_error) == ("queued", 1, "rate_limit")
    with clean_db.connect() as conn:
        wait_s = conn.execute(
            select(func.extract("epoch", jobs_table.c.run_after - func.now())).where(
                jobs_table.c.lead_id == lead_id
            )
        ).scalar_one()
    assert 25 < float(wait_s) <= 30
    assert lead_state(clean_db, lead_id) == ("received", None)


# AC-2.3 ---------------------------------------------------------------------------------------


def test_retries_exhausted_fail_the_lead_with_one_alert_and_the_worker_keeps_going(
    clean_db: Engine,
) -> None:
    lead_id = new_lead(clean_db)
    always_timeout = ScriptedLLM(TransientLLMError("timeout"))

    assert qualify_until_idle(clean_db, always_timeout, settings(), "w1") == 5  # returns, no crash

    job = job_row(clean_db, lead_id)
    assert (job.status, job.attempts, job.last_error) == ("failed", 5, "retries_exhausted:timeout")
    assert lead_state(clean_db, lead_id) == ("processing_failed", "retries_exhausted:timeout")
    assert alert_count(clean_db, lead_id) == 1
    assert [c.outcome for c in call_rows(clean_db, lead_id)] == ["transient_error"] * 5
    assert assessment_rows(clean_db, lead_id) == []

    # The same worker goes on to process the next job normally.
    next_lead = new_lead(clean_db, email="next@example.com", message="Another inquiry")
    assert qualify_until_idle(clean_db, ScriptedLLM(output()), settings(), "w1") == 1
    assert lead_state(clean_db, next_lead) == ("qualified", None)


@pytest.mark.parametrize(
    ("failure", "reason", "error_kind"),
    [
        pytest.param(PermanentLLMError("auth"), "llm_error:auth", "auth", id="permanent"),
        pytest.param(
            RuntimeError("bug"), "unexpected:RuntimeError", "unexpected:RuntimeError", id="bug"
        ),
    ],
)
def test_non_retryable_failures_fail_fast_with_one_alert(
    clean_db: Engine, failure: Exception, reason: str, error_kind: str
) -> None:
    lead_id = new_lead(clean_db)

    assert qualify_until_idle(clean_db, ScriptedLLM(failure), settings(), "w1") == 1

    assert job_row(clean_db, lead_id).status == "failed"
    assert lead_state(clean_db, lead_id) == ("processing_failed", reason)
    assert alert_count(clean_db, lead_id) == 1
    # The attempted call is recorded even when it failed in an unexpected way.
    assert [(c.outcome, c.error_kind) for c in call_rows(clean_db, lead_id)] == [
        ("error", error_kind)
    ]


# AC-2.4 ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("response", "kind"),
    [
        pytest.param(raw("I can't help with that.", "refusal"), "refusal", id="refusal"),
        pytest.param(
            raw('{"score": 80, "reasons": ["a"', "max_tokens"), "truncated", id="truncated"
        ),
        pytest.param(raw(""), "empty", id="empty"),
        pytest.param(raw("Here you go: {score: 80}"), "invalid_json", id="invalid-json"),
        pytest.param(
            raw(json.dumps({"score": 999, "priority": "SUPER_HIGH", "reason": None})),
            "schema",
            id="user-example",
        ),
        pytest.param(output(score=101), "schema", id="score-out-of-range"),
        pytest.param(output(tier="hot"), "schema", id="unexpected-field"),
        pytest.param(output(reasons=["   "]), "domain", id="reasons-blank"),
        # U16. The first is the M3 finding: the job failed on the insert, after "ok" was recorded.
        pytest.param(output(summary="Looks good\u0000"), "domain", id="nul-in-summary"),
        pytest.param(output(reasons=["Clear budget\u0000"]), "domain", id="nul-in-a-reason"),
        pytest.param(output(summary="Looks\u001fgood"), "domain", id="control-character"),
    ],
)
def test_unusable_output_goes_to_review_without_score_and_without_retry(
    clean_db: Engine, response: LLMResponse, kind: str
) -> None:
    lead_id = new_lead(clean_db)
    llm = ScriptedLLM(response)

    assert qualify_until_idle(clean_db, llm, settings(), "w1") == 1

    assert len(llm.requests) == 1  # no retry
    assert assessment_rows(clean_db, lead_id) == []
    assert lead_state(clean_db, lead_id) == ("needs_review", f"invalid_output:{kind}")
    assert job_row(clean_db, lead_id).status == "done"
    assert alert_count(clean_db, lead_id) == 0
    [call] = call_rows(clean_db, lead_id)
    assert (call.outcome, call.error_kind, call.raw_output) == (
        "rejected_output",
        kind,
        response.text,
    )


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(
            text_of(output(summary="Looks\u0000good")).replace("\\u0000", "\x00"),
            id="nul-in-a-string",
        ),
        pytest.param(text_of(output()) + "\x00", id="nul-after-the-json"),
    ],
)
def test_a_nul_in_the_response_text_is_recorded_and_goes_to_review(
    clean_db: Engine, text: str
) -> None:
    """U16. PostgreSQL text cannot hold NUL. The call is still recorded, with each NUL stored as
    U+2400, and the lead goes to review like any other unusable output."""
    assert text.count("\x00") == 1
    lead_id = new_lead(clean_db)

    assert qualify_until_idle(clean_db, ScriptedLLM(raw(text)), settings(), "w1") == 1

    assert assessment_rows(clean_db, lead_id) == []
    assert lead_state(clean_db, lead_id) == ("needs_review", "invalid_output:invalid_json")
    assert alert_count(clean_db, lead_id) == 0
    [call] = call_rows(clean_db, lead_id)
    assert (call.outcome, call.error_kind, call.input_tokens) == (
        "rejected_output",
        "invalid_json",
        420,
    )
    assert call.raw_output == text.replace("\x00", "\u2400")


def test_the_raw_output_record_is_cut_at_its_limit(clean_db: Engine) -> None:
    """A stored NUL takes one character of the limit, like any other character."""
    text = "\x00" + "x" * jobs.RAW_OUTPUT_LIMIT
    lead_id = new_lead(clean_db)

    qualify_until_idle(clean_db, ScriptedLLM(raw(text)), settings(), "w1")

    [call] = call_rows(clean_db, lead_id)
    assert call.raw_output == "\u2400" + "x" * (jobs.RAW_OUTPUT_LIMIT - 1)


def test_low_confidence_is_stored_and_sent_to_review(clean_db: Engine) -> None:
    lead_id = new_lead(clean_db)
    qualify_until_idle(clean_db, ScriptedLLM(output(confidence=0.3)), settings(), "w1")
    assert len(assessment_rows(clean_db, lead_id)) == 1
    assert lead_state(clean_db, lead_id) == ("needs_review", "low_confidence")


# AC-2.5 ---------------------------------------------------------------------------------------


def test_two_workers_in_parallel_process_each_job_exactly_once(clean_db: Engine) -> None:
    lead_ids = [
        new_lead(clean_db, email=f"lead{i}@example.com", message=f"inquiry-{i:03d}-end")
        for i in range(30)
    ]

    def slow_valid_output(_request: LLMRequest) -> LLMResponse:
        time.sleep(0.02)  # long enough for the two workers to overlap
        return output()

    llm = ScriptedLLM(slow_valid_output)
    processed: dict[str, int] = {}

    def work(worker_id: str) -> None:
        processed[worker_id] = qualify_until_idle(clean_db, llm, settings(), worker_id)

    threads = [threading.Thread(target=work, args=(w,)) for w in ("w1", "w2")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert processed["w1"] + processed["w2"] == 30
    assert processed["w1"] > 0 and processed["w2"] > 0  # both really worked in parallel
    prompts = Counter(
        next(f"inquiry-{i:03d}-end" for i in range(30) if f"inquiry-{i:03d}-end" in r.user)
        for r in llm.requests
    )
    assert set(prompts.values()) == {1} and len(prompts) == 30
    with clean_db.connect() as conn:
        assert conn.execute(select(func.count()).select_from(lead_assessments)).scalar_one() == 30
        assert conn.execute(select(func.count()).select_from(llm_calls)).scalar_one() == 30
        statuses: list[str] = list(
            conn.execute(
                select(jobs_table.c.status).where(jobs_table.c.kind == jobs.JOB_QUALIFY)
            ).scalars()
        )
    assert statuses == ["done"] * 30
    assert all(lead_state(clean_db, lead_id)[0] == "qualified" for lead_id in lead_ids)


def test_expired_lease_is_taken_over_and_the_late_result_is_dropped(clean_db: Engine) -> None:
    lead_id = new_lead(clean_db)
    stale = jobs.claim_next(clean_db, "crashed-worker", lease_s=300)
    assert stale is not None
    with clean_db.begin() as conn:
        conn.execute(text("UPDATE jobs SET locked_until = now() - interval '1 second'"))

    assert qualify_until_idle(clean_db, ScriptedLLM(output()), settings(), "w2") == 1
    assert job_row(clean_db, lead_id).attempts == 2

    with clean_db.begin() as conn:  # the crashed worker comes back with its old result
        assert jobs.complete(conn, stale, "crashed-worker") is False
    assert len(assessment_rows(clean_db, lead_id)) == 1
    assert lead_state(clean_db, lead_id) == ("qualified", None)


# AC-2.6 ---------------------------------------------------------------------------------------


def test_every_llm_call_records_model_prompt_version_tokens_and_latency(clean_db: Engine) -> None:
    lead_id = new_lead(clean_db)
    llm = ScriptedLLM(TransientLLMError("rate_limit", http_status=429), output(), model="m-v2")

    qualify_until_idle(clean_db, llm, settings(), "w1")

    failed, ok = call_rows(clean_db, lead_id)
    for call in (failed, ok):
        assert (call.provider, call.model, call.prompt_version) == ("fake", "m-v2", PROMPT_VERSION)
        assert isinstance(call.latency_ms, int) and call.latency_ms >= 0
        assert call.started_at is not None and call.ended_at >= call.started_at
    # A response with usage: measured token counts are stored.
    assert (ok.response_received, ok.usage_available) == (True, True)
    assert (ok.input_tokens, ok.output_tokens) == (420, 85)
    # No response, no usage: NULL (unknown), never 0 (U5).
    assert (failed.response_received, failed.usage_available) == (False, False)
    assert (failed.input_tokens, failed.output_tokens, failed.cost_usd) == (None, None, None)
    assert (failed.error_kind, failed.http_status) == ("rate_limit", 429)


def test_cost_is_computed_only_from_reported_usage_and_configured_prices(clean_db: Engine) -> None:
    lead_id = new_lead(clean_db)
    llm = ScriptedLLM(TransientLLMError("timeout"), output())
    priced = settings(llm_price_input_usd_per_mtok=2, llm_price_output_usd_per_mtok=10)

    qualify_until_idle(clean_db, llm, priced, "w1")

    failed, ok = call_rows(clean_db, lead_id)
    assert ok.cost_usd == Decimal("0.00169000")  # (420 * 2 + 85 * 10) / 1,000,000
    assert failed.cost_usd is None  # timeout: unknown, not free


def test_cost_is_null_when_prices_are_not_configured(clean_db: Engine) -> None:
    lead_id = new_lead(clean_db)
    qualify_until_idle(clean_db, ScriptedLLM(output()), settings(), "w1")
    [call] = call_rows(clean_db, lead_id)
    assert call.usage_available is True
    assert call.cost_usd is None


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param(
            {"usage_available": False, "input_tokens": 0, "output_tokens": 0},
            id="zero-tokens-recorded-as-unavailable",
        ),
        pytest.param(
            {"usage_available": False, "cost_usd": Decimal("0")}, id="zero-cost-without-usage"
        ),
        pytest.param(
            {"usage_available": False, "input_tokens": 0}, id="one-token-count-recorded-as-zero"
        ),
        pytest.param({"response_received": True}, id="response-flag-contradicts-outcome"),
    ],
)
def test_database_refuses_to_turn_unknown_usage_into_zero(
    clean_db: Engine, overrides: dict[str, Any]
) -> None:
    lead_id = new_lead(clean_db)
    qualify_until_idle(clean_db, ScriptedLLM(TransientLLMError("timeout")), settings(), "w1")
    job = job_row(clean_db, lead_id)
    values: dict[str, Any] = {
        "job_id": job.id,
        "lead_id": lead_id,
        "attempt": 9,
        "provider": "fake",
        "model": "m",
        "prompt_version": PROMPT_VERSION,
        "outcome": "transient_error",
        "latency_ms": 1,
        "response_received": False,
        "usage_available": False,
    }
    values.update(overrides)
    with pytest.raises(IntegrityError), clean_db.begin() as conn:
        conn.execute(llm_calls.insert().values(**values))


# Properties beyond the ACs ----------------------------------------------------------------------


def test_prompt_contains_no_name_and_no_email_address(clean_db: Engine) -> None:
    new_lead(clean_db)
    llm = ScriptedLLM(output())
    qualify_until_idle(clean_db, llm, settings(), "w1")
    [request] = llm.requests
    sent = (request.system + request.user).lower()
    assert "jane.doe@example.com" not in sent
    assert VALID["name"].lower() not in sent
    assert "email domain: example.com" in sent


def test_only_a_new_lead_gets_a_qualification_job(client: TestClient, clean_db: Engine) -> None:
    for key, message in [("k1", "first"), ("k2", "second")]:
        body = {**VALID, "message": message}
        assert (
            client.post("/v1/leads", json=body, headers={"Idempotency-Key": key}).status_code == 201
        )
    with clean_db.connect() as conn:
        assert conn.execute(select(func.count()).select_from(jobs_table)).scalar_one() == 1
