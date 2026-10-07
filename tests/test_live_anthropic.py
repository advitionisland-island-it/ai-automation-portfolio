"""AC-2.7: one real call to the approved provider (UD-4), with synthetic data only.

Excluded from the default run by the `live` marker. Run it with `make smoke-live`, which takes
ANTHROPIC_API_KEY from the environment or from the git-ignored .env file. Nothing here prints
or stores the key.
"""

import json
import os
from decimal import Decimal

import pytest
from sqlalchemy import Engine

from sales_ops.jobs import JOB_QUALIFY
from sales_ops.providers import build_llm_client
from sales_ops.worker import WorkerSettings, run_until_idle
from tests.helpers import assessment_rows, call_rows, lead_state, new_lead

pytestmark = pytest.mark.live

# Synthetic data only (U5): a reserved example domain, and text that says it is a test.
SYNTHETIC_EMAIL = "smoke.test@example.com"
SYNTHETIC_MESSAGE = (
    "SYNTHETIC TEST DATA, not a real inquiry. We are a 120-person software company evaluating "
    "lead-routing automation for three sales teams. Budget is approved for Q4 and we would "
    "like a call next week."
)
# Far above the expected cost of one call (well under one cent), far below the USD 5.00 cap.
PER_CALL_COST_LIMIT_USD = Decimal("0.10")


def test_live_provider_qualifies_one_synthetic_lead(clean_db: Engine) -> None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        pytest.fail(
            "ANTHROPIC_API_KEY is not set: put it in projects/p01-lead-automation/.env "
            "(git-ignored) and run `make smoke-live`"
        )
    settings = WorkerSettings()  # type: ignore[call-arg]  # set by `make smoke-live`
    assert settings.llm_provider == "anthropic"
    assert SYNTHETIC_EMAIL.endswith("@example.com")  # reserved domain: no real person
    lead_id = new_lead(clean_db, email=SYNTHETIC_EMAIL, message=SYNTHETIC_MESSAGE)

    # Qualification only: exactly one real call (drafting is not part of AC-2.7).
    llm = build_llm_client(settings)
    assert run_until_idle(clean_db, llm, settings, "smoke", kinds=[JOB_QUALIFY]) == 1

    [call] = call_rows(clean_db, lead_id)
    assessments = assessment_rows(clean_db, lead_id)
    status = lead_state(clean_db, lead_id)
    summary = {
        "provider": call.provider,
        "model": call.model,
        "outcome": call.outcome,
        "error_kind": call.error_kind,
        "http_status": call.http_status,
        "response_received": call.response_received,
        "usage_available": call.usage_available,
        "input_tokens": call.input_tokens,
        "output_tokens": call.output_tokens,
        "cost_usd": call.cost_usd,
        "latency_ms": call.latency_ms,
        "lead_status": status,
        "assessment": [(a.score, a.tier, a.confidence) for a in assessments],
        "raw_output": (call.raw_output or "")[:400],
    }
    print("SMOKE", json.dumps(summary, default=str, ensure_ascii=False))  # evidence, no secrets

    assert call.outcome == "ok"
    assert (call.provider, call.model) == ("anthropic", settings.llm_model)
    assert call.response_received and call.usage_available
    assert call.input_tokens > 0 and call.output_tokens > 0
    assert call.cost_usd is not None and Decimal(0) < call.cost_usd < PER_CALL_COST_LIMIT_USD
    assert len(assessments) == 1
    assert status in {("qualified", None), ("needs_review", "low_confidence")}
