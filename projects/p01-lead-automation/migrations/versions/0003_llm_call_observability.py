"""llm call observability: response/usage flags, cost, HTTP status, timestamps (U5)

NULL means unobserved, not zero: when no usage came back, tokens and cost stay NULL, and the
CHECK constraints make it impossible to record them as 0 in that case.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CHECKS = {
    "ck_llm_calls_response_matches_outcome": (
        "response_received = (outcome IN ('ok', 'rejected_output'))"
    ),
    "ck_llm_calls_tokens_both_or_neither": "(input_tokens IS NULL) = (output_tokens IS NULL)",
    "ck_llm_calls_usage_matches_tokens": (
        "usage_available = (input_tokens IS NOT NULL AND output_tokens IS NOT NULL)"
    ),
    "ck_llm_calls_cost_needs_usage": "cost_usd IS NULL OR usage_available",
    "ck_llm_calls_usage_non_negative": (
        "(input_tokens IS NULL OR input_tokens >= 0) "
        "AND (output_tokens IS NULL OR output_tokens >= 0) "
        "AND (cost_usd IS NULL OR cost_usd >= 0)"
    ),
}


def upgrade() -> None:
    op.add_column("llm_calls", sa.Column("response_received", sa.Boolean(), nullable=True))
    op.add_column("llm_calls", sa.Column("usage_available", sa.Boolean(), nullable=True))
    op.add_column("llm_calls", sa.Column("cost_usd", sa.Numeric(14, 8), nullable=True))
    op.add_column("llm_calls", sa.Column("http_status", sa.Integer(), nullable=True))
    op.add_column("llm_calls", sa.Column("started_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("llm_calls", sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True))
    # Rows written before 0003: a model response arrived exactly when its output was evaluated.
    op.execute(
        "UPDATE llm_calls SET "
        "response_received = outcome IN ('ok', 'rejected_output'), "
        "usage_available = (input_tokens IS NOT NULL AND output_tokens IS NOT NULL)"
    )
    op.alter_column("llm_calls", "response_received", nullable=False)
    op.alter_column("llm_calls", "usage_available", nullable=False)
    for name, condition in _CHECKS.items():
        op.create_check_constraint(op.f(name), "llm_calls", condition)


def downgrade() -> None:
    for name in _CHECKS:
        op.drop_constraint(op.f(name), "llm_calls", type_="check")
    for column in (
        "ended_at",
        "started_at",
        "http_status",
        "cost_usd",
        "usage_available",
        "response_received",
    ):
        op.drop_column("llm_calls", column)
