"""notifications for n8n: review requests (W2) and alert claims (W3)

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("alert_events", sa.Column("claimed_until", sa.DateTime(timezone=True)))
    op.create_table(
        "review_requests",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("lead_id", sa.Uuid(), nullable=False),
        sa.Column("draft_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("claimed_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["draft_id"],
            ["drafts.id"],
            name=op.f("fk_review_requests_draft_id_drafts"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["lead_id"],
            ["leads.id"],
            name=op.f("fk_review_requests_lead_id_leads"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_review_requests")),
        sa.UniqueConstraint("draft_id", name=op.f("uq_review_requests_draft_id")),
    )
    # Leads that were already waiting for approval get their request, for the current draft.
    op.execute(
        "INSERT INTO review_requests (lead_id, draft_id) "
        "SELECT DISTINCT ON (d.lead_id) d.lead_id, d.id FROM drafts d "
        "JOIN leads l ON l.id = d.lead_id AND l.status = 'awaiting_approval' "
        "ORDER BY d.lead_id, d.version DESC"
    )


def downgrade() -> None:
    op.drop_table("review_requests")
    op.drop_column("alert_events", "claimed_until")
