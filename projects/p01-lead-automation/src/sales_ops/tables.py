"""Database schema (SQLAlchemy Core). Migrations in migrations/versions must match this module."""

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Double,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    Numeric,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

metadata = MetaData(
    naming_convention={
        "ix": "ix_%(column_0_label)s",
        "uq": "uq_%(table_name)s_%(column_0_N_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    }
)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{v}'" for v in values) + ")"


# Every state in MASTER_TASK_P01 §3. Which transitions are legal is enforced in states.py.
LEAD_STATUSES = (
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
JOB_KINDS = ("qualify_lead", "draft_follow_up", "send_follow_up")
JOB_STATUSES = ("queued", "running", "done", "failed")
LLM_CALL_OUTCOMES = ("ok", "rejected_output", "transient_error", "error")
TIERS = ("hot", "warm", "cold")
ALERT_KINDS = ("processing_failed", "send_failed")

leads = Table(
    "leads",
    metadata,
    Column("id", Uuid, primary_key=True, server_default=text("gen_random_uuid()")),
    # Lead identity (D-8): trimmed and lowercased email. The UNIQUE constraint is what stops
    # concurrent inquiries from the same person from creating two leads.
    Column("email_normalized", Text, nullable=False, unique=True),
    Column("name", Text, nullable=False),
    Column("company", Text),
    Column("status", Text, nullable=False, server_default=text("'received'")),
    # Why the lead is in its current status (e.g. "low_confidence", "retries_exhausted:timeout").
    Column("status_reason", Text),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint(_in("status", LEAD_STATUSES), name="status_known"),
)

inquiries = Table(
    "inquiries",
    metadata,
    Column("id", Uuid, primary_key=True, server_default=text("gen_random_uuid()")),
    Column(
        "lead_id",
        Uuid,
        ForeignKey("leads.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    ),
    Column("source", Text, nullable=False),
    Column("message", Text, nullable=False),
    Column("payload", JSONB, nullable=False),
    Column("received_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

# One row per (scope, key). The primary key makes a second claim on the same key wait for,
# then see, the first one (INSERT ... ON CONFLICT DO NOTHING). Stored response = replay body.
idempotency_keys = Table(
    "idempotency_keys",
    metadata,
    Column("scope", Text, primary_key=True),
    Column("key", Text, primary_key=True),
    Column("request_sha256", Text, nullable=False),
    Column("lead_id", Uuid, ForeignKey("leads.id", ondelete="SET NULL")),
    Column("inquiry_id", Uuid, ForeignKey("inquiries.id", ondelete="SET NULL")),
    Column("response_status", Integer),
    Column("response_body", JSONB),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

# Work queue for the worker (D-6). A job is claimed with FOR UPDATE SKIP LOCKED and a lease;
# retries put it back to "queued" with a later run_after.
jobs = Table(
    "jobs",
    metadata,
    Column("id", Uuid, primary_key=True, server_default=text("gen_random_uuid()")),
    Column("kind", Text, nullable=False),
    Column("lead_id", Uuid, ForeignKey("leads.id", ondelete="CASCADE"), nullable=False),
    Column("status", Text, nullable=False, server_default=text("'queued'")),
    Column("attempts", Integer, nullable=False, server_default=text("0")),
    Column("max_attempts", Integer, nullable=False),
    Column("run_after", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("locked_by", Text),
    Column("locked_until", DateTime(timezone=True)),
    Column("last_error", Text),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("kind", "lead_id"),
    CheckConstraint(_in("kind", JOB_KINDS), name="kind_known"),
    CheckConstraint(_in("status", JOB_STATUSES), name="status_known"),
    CheckConstraint("attempts >= 0 AND max_attempts >= 1", name="attempts_valid"),
    Index("ix_jobs_claim", "status", "run_after"),
)

# Every attempted LLM call, successful or not (a row exists only if the call was attempted):
# the cost and latency record (AC-2.6) and the raw output humans read in needs_review.
# NULL means unobserved, not zero (U5): without usage, tokens and cost stay NULL.
llm_calls = Table(
    "llm_calls",
    metadata,
    Column("id", Uuid, primary_key=True, server_default=text("gen_random_uuid()")),
    Column("job_id", Uuid, ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True),
    Column("lead_id", Uuid, ForeignKey("leads.id", ondelete="CASCADE"), nullable=False),
    Column("attempt", Integer, nullable=False),
    Column("provider", Text, nullable=False),
    Column("model", Text, nullable=False),
    Column("prompt_version", Text, nullable=False),
    Column("outcome", Text, nullable=False),
    Column("error_kind", Text),
    Column("input_tokens", Integer),
    Column("output_tokens", Integer),
    Column("latency_ms", Integer, nullable=False),
    Column("raw_output", Text),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("response_received", Boolean, nullable=False),  # a model response arrived
    Column("usage_available", Boolean, nullable=False),  # the provider reported token usage
    Column("cost_usd", Numeric(14, 8)),  # NULL: unknown (no usage or no price configured)
    Column("http_status", Integer),  # NULL when no HTTP response arrived (timeout, network)
    Column("started_at", DateTime(timezone=True)),
    Column("ended_at", DateTime(timezone=True)),
    CheckConstraint(_in("outcome", LLM_CALL_OUTCOMES), name="outcome_known"),
    CheckConstraint("latency_ms >= 0", name="latency_non_negative"),
    CheckConstraint(
        "response_received = (outcome IN ('ok', 'rejected_output'))",
        name="response_matches_outcome",
    ),
    CheckConstraint(
        "(input_tokens IS NULL) = (output_tokens IS NULL)", name="tokens_both_or_neither"
    ),
    CheckConstraint(
        "usage_available = (input_tokens IS NOT NULL AND output_tokens IS NOT NULL)",
        name="usage_matches_tokens",
    ),
    CheckConstraint("cost_usd IS NULL OR usage_available", name="cost_needs_usage"),
    CheckConstraint(
        "(input_tokens IS NULL OR input_tokens >= 0) "
        "AND (output_tokens IS NULL OR output_tokens >= 0) "
        "AND (cost_usd IS NULL OR cost_usd >= 0)",
        name="usage_non_negative",
    ),
)

# Only validated and normalized assessments get here. The CHECK constraints are the last line
# of defense if a bug ever let an unvalidated LLM value through.
lead_assessments = Table(
    "lead_assessments",
    metadata,
    Column("id", Uuid, primary_key=True, server_default=text("gen_random_uuid()")),
    Column(
        "lead_id",
        Uuid,
        ForeignKey("leads.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    ),
    Column("llm_call_id", Uuid, ForeignKey("llm_calls.id", ondelete="CASCADE"), nullable=False),
    Column("prompt_version", Text, nullable=False),
    Column("score", Integer, nullable=False),
    Column("tier", Text, nullable=False),
    Column("reasons", JSONB, nullable=False),
    Column("summary", Text, nullable=False),
    Column("confidence", Double, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint("score BETWEEN 0 AND 100", name="score_range"),
    CheckConstraint(_in("tier", TIERS), name="tier_known"),
    CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_range"),
    CheckConstraint(
        "jsonb_typeof(reasons) = 'array' AND jsonb_array_length(reasons) BETWEEN 1 AND 5",
        name="reasons_shape",
    ),
)

# Outbox of alerts. n8n (W3, M4) delivers them and sets delivered_at.
alert_events = Table(
    "alert_events",
    metadata,
    Column("id", Uuid, primary_key=True, server_default=text("gen_random_uuid()")),
    Column("kind", Text, nullable=False),
    Column("lead_id", Uuid, ForeignKey("leads.id", ondelete="CASCADE")),
    Column("job_id", Uuid, ForeignKey("jobs.id", ondelete="CASCADE")),
    Column("detail", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    # Delivery by n8n W3 (M4): claimed for a short lease, then marked delivered.
    Column("claimed_until", DateTime(timezone=True)),
    Column("delivered_at", DateTime(timezone=True)),
    UniqueConstraint("kind", "job_id"),
    CheckConstraint(_in("kind", ALERT_KINDS), name="kind_known"),
)


def _text_sha256(subject: str, body: str) -> str:
    """SQL for the sha256 of subject + two newlines + body in UTF-8 (drafting.draft_sha256)."""
    return f"encode(sha256(convert_to({subject} || E'\\n\\n' || {body}, 'UTF8')), 'hex')"


# The hashed text can only be split back into (subject, body) at its first line break, so the
# hash names exactly one pair as long as the subject is a single line.
_SUBJECT_ONE_LINE = "subject !~ '[\\r\\n]'"


# Follow-up drafts. Every change is a new version; the current draft is the highest version.
# The database checks that sha256 is the hash of the text, so the stored hash cannot drift.
drafts = Table(
    "drafts",
    metadata,
    Column("id", Uuid, primary_key=True, server_default=text("gen_random_uuid()")),
    Column("lead_id", Uuid, ForeignKey("leads.id", ondelete="CASCADE"), nullable=False),
    Column("version", Integer, nullable=False),
    Column("subject", Text, nullable=False),
    Column("body", Text, nullable=False),
    Column("sha256", Text, nullable=False),
    Column("created_by", Text, nullable=False),  # "llm" or "human:<who>"
    Column("llm_call_id", Uuid, ForeignKey("llm_calls.id", ondelete="SET NULL")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("lead_id", "version"),
    UniqueConstraint("id", "sha256"),  # target of the approvals foreign key
    CheckConstraint("version >= 1", name="version_positive"),
    CheckConstraint(_SUBJECT_ONE_LINE, name="subject_one_line"),
    CheckConstraint(f"sha256 = {_text_sha256('subject', 'body')}", name="sha256_matches_text"),
)

# A human approval is bound to the exact draft text (D-12): the foreign key covers the hash, so
# an approval can only carry the hash of the draft it approves. At most one active approval per
# lead; a revoked approval stays on record.
approvals = Table(
    "approvals",
    metadata,
    Column("id", Uuid, primary_key=True, server_default=text("gen_random_uuid()")),
    Column("lead_id", Uuid, ForeignKey("leads.id", ondelete="CASCADE"), nullable=False),
    Column("draft_id", Uuid, nullable=False),
    Column("draft_sha256", Text, nullable=False),
    Column("approved_by", Text, nullable=False),
    Column("approved_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("revoked_at", DateTime(timezone=True)),
    Column("revoke_reason", Text),
    ForeignKeyConstraint(
        ["draft_id", "draft_sha256"], ["drafts.id", "drafts.sha256"], ondelete="CASCADE"
    ),
    UniqueConstraint("id", "draft_sha256"),  # target of the outbox foreign key
    Index(
        "uq_approvals_active_lead",
        "lead_id",
        unique=True,
        postgresql_where=text("revoked_at IS NULL"),
    ),
)

# One row per approved text to send (D-11). The UNIQUE (lead_id, draft_sha256) and sent_at stop
# a re-run from sending the same text twice; message_id is the email's Message-ID header.
# The foreign key and the CHECK together make the database guarantee that the text in this row
# is the text the referenced approval approved.
outbox = Table(
    "outbox",
    metadata,
    Column("id", Uuid, primary_key=True, server_default=text("gen_random_uuid()")),
    Column("lead_id", Uuid, ForeignKey("leads.id", ondelete="CASCADE"), nullable=False),
    Column("approval_id", Uuid, nullable=False),
    Column("draft_sha256", Text, nullable=False),
    Column("message_id", Text, nullable=False, unique=True),
    Column("recipient", Text, nullable=False),
    Column("subject", Text, nullable=False),
    Column("body", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("sent_at", DateTime(timezone=True)),
    ForeignKeyConstraint(
        ["approval_id", "draft_sha256"],
        ["approvals.id", "approvals.draft_sha256"],
        ondelete="CASCADE",
    ),
    UniqueConstraint("lead_id", "draft_sha256"),
    CheckConstraint(_SUBJECT_ONE_LINE, name="subject_one_line"),
    CheckConstraint(
        f"draft_sha256 = {_text_sha256('subject', 'body')}", name="text_matches_approval"
    ),
)

# "Please review this draft" notifications for n8n W2 (M4), written in the same transaction
# that moves the lead to awaiting_approval. One per draft; delivered at least once.
review_requests = Table(
    "review_requests",
    metadata,
    Column("id", Uuid, primary_key=True, server_default=text("gen_random_uuid()")),
    Column("lead_id", Uuid, ForeignKey("leads.id", ondelete="CASCADE"), nullable=False),
    Column("draft_id", Uuid, ForeignKey("drafts.id", ondelete="CASCADE"), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("claimed_until", DateTime(timezone=True)),
    Column("delivered_at", DateTime(timezone=True)),
    UniqueConstraint("draft_id"),
)
