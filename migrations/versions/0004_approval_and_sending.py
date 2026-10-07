"""approval and sending: drafts, approvals, outbox; draft and send jobs; send_failed alerts

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{v}'" for v in values) + ")"


_JOB_KINDS_BEFORE = ("qualify_lead",)
_JOB_KINDS_AFTER = ("qualify_lead", "draft_follow_up", "send_follow_up")
_ALERT_KINDS_BEFORE = ("processing_failed",)
_ALERT_KINDS_AFTER = ("processing_failed", "send_failed")


# The sha256 of subject + two newlines + body in UTF-8, as drafting.draft_sha256 computes it.
_TEXT_SHA256 = "encode(sha256(convert_to(subject || E'\\n\\n' || body, 'UTF8')), 'hex')"
# A one-line subject makes that text split back into (subject, body) in exactly one way.
_SUBJECT_ONE_LINE = "subject !~ '[\\r\\n]'"


def _replace_check(table: str, name: str, condition: str) -> None:
    op.drop_constraint(op.f(name), table, type_="check")
    op.create_check_constraint(op.f(name), table, condition)


def upgrade() -> None:
    _replace_check("jobs", "ck_jobs_kind_known", _in("kind", _JOB_KINDS_AFTER))
    _replace_check("alert_events", "ck_alert_events_kind_known", _in("kind", _ALERT_KINDS_AFTER))

    op.create_table(
        "drafts",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("lead_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("llm_call_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("version >= 1", name=op.f("ck_drafts_version_positive")),
        sa.CheckConstraint(_SUBJECT_ONE_LINE, name=op.f("ck_drafts_subject_one_line")),
        sa.CheckConstraint(f"sha256 = {_TEXT_SHA256}", name=op.f("ck_drafts_sha256_matches_text")),
        sa.ForeignKeyConstraint(
            ["lead_id"], ["leads.id"], name=op.f("fk_drafts_lead_id_leads"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["llm_call_id"],
            ["llm_calls.id"],
            name=op.f("fk_drafts_llm_call_id_llm_calls"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_drafts")),
        sa.UniqueConstraint("lead_id", "version", name=op.f("uq_drafts_lead_id_version")),
        sa.UniqueConstraint("id", "sha256", name=op.f("uq_drafts_id_sha256")),
    )
    op.create_table(
        "approvals",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("lead_id", sa.Uuid(), nullable=False),
        sa.Column("draft_id", sa.Uuid(), nullable=False),
        sa.Column("draft_sha256", sa.Text(), nullable=False),
        sa.Column("approved_by", sa.Text(), nullable=False),
        sa.Column(
            "approved_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoke_reason", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["draft_id", "draft_sha256"],
            ["drafts.id", "drafts.sha256"],
            name=op.f("fk_approvals_draft_id_drafts"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["lead_id"], ["leads.id"], name=op.f("fk_approvals_lead_id_leads"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_approvals")),
        sa.UniqueConstraint("id", "draft_sha256", name=op.f("uq_approvals_id_draft_sha256")),
    )
    op.create_index(
        "uq_approvals_active_lead",
        "approvals",
        ["lead_id"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )
    op.create_table(
        "outbox",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("lead_id", sa.Uuid(), nullable=False),
        sa.Column("approval_id", sa.Uuid(), nullable=False),
        sa.Column("draft_sha256", sa.Text(), nullable=False),
        sa.Column("message_id", sa.Text(), nullable=False),
        sa.Column("recipient", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["approval_id", "draft_sha256"],
            ["approvals.id", "approvals.draft_sha256"],
            name=op.f("fk_outbox_approval_id_approvals"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["lead_id"], ["leads.id"], name=op.f("fk_outbox_lead_id_leads"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_outbox")),
        sa.UniqueConstraint("lead_id", "draft_sha256", name=op.f("uq_outbox_lead_id_draft_sha256")),
        sa.UniqueConstraint("message_id", name=op.f("uq_outbox_message_id")),
        sa.CheckConstraint(_SUBJECT_ONE_LINE, name=op.f("ck_outbox_subject_one_line")),
        sa.CheckConstraint(
            f"draft_sha256 = {_TEXT_SHA256}", name=op.f("ck_outbox_text_matches_approval")
        ),
    )
    # Leads qualified before drafting existed still need a draft.
    op.execute(
        "INSERT INTO jobs (kind, lead_id, max_attempts) "
        "SELECT 'draft_follow_up', id, 5 FROM leads WHERE status = 'qualified'"
    )


def downgrade() -> None:
    # Without drafts, approvals and sending, those statuses lose their meaning: put the leads
    # back to qualified, including leads whose draft job had given up.
    op.execute(
        "UPDATE leads SET status = 'qualified', status_reason = NULL "
        "WHERE status IN ('awaiting_approval', 'approved', 'rejected', 'sent', 'send_failed') "
        "OR (status = 'processing_failed' AND EXISTS (SELECT 1 FROM jobs j "
        "WHERE j.lead_id = leads.id AND j.kind = 'draft_follow_up' AND j.status = 'failed'))"
    )
    op.execute("DELETE FROM jobs WHERE kind IN ('draft_follow_up', 'send_follow_up')")
    op.execute("DELETE FROM alert_events WHERE kind = 'send_failed'")
    op.drop_table("outbox")
    op.drop_index("uq_approvals_active_lead", table_name="approvals")
    op.drop_table("approvals")
    op.drop_table("drafts")
    _replace_check("alert_events", "ck_alert_events_kind_known", _in("kind", _ALERT_KINDS_BEFORE))
    _replace_check("jobs", "ck_jobs_kind_known", _in("kind", _JOB_KINDS_BEFORE))
