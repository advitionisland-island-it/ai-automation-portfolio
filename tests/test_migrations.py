from uuid import UUID

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Engine, text

from sales_ops.tables import metadata
from tests.helpers import TEST_DATABASE_URL, alembic_config


def _schema_differences(engine: Engine) -> list[object]:
    with engine.connect() as conn:
        context = MigrationContext.configure(conn, opts={"compare_type": True})
        return list(compare_metadata(context, metadata))


def test_migrated_schema_matches_table_definitions(db_engine: Engine) -> None:
    assert _schema_differences(db_engine) == []


def test_full_downgrade_and_upgrade_round_trip(db_engine: Engine) -> None:
    config = alembic_config(TEST_DATABASE_URL)
    command.downgrade(config, "base")
    with db_engine.connect() as conn:
        tables: int = conn.execute(
            text("SELECT count(*) FROM pg_tables WHERE schemaname = 'public'")
        ).scalar_one()
    assert tables == 1  # only alembic_version is left
    command.upgrade(config, "head")
    assert _schema_differences(db_engine) == []


def test_upgrade_to_0002_backfills_jobs_for_leads_waiting_in_received(db_engine: Engine) -> None:
    config = alembic_config(TEST_DATABASE_URL)
    with db_engine.begin() as conn:
        conn.execute(text("TRUNCATE leads CASCADE"))
    command.downgrade(config, "0001")
    try:
        with db_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO leads (email_normalized, name) VALUES "
                    "('before-worker@example.com', 'Early Lead')"
                )
            )
        command.upgrade(config, "head")
        with db_engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT j.kind, j.status, j.max_attempts FROM jobs j "
                    "JOIN leads l ON l.id = j.lead_id "
                    "WHERE l.email_normalized = 'before-worker@example.com'"
                )
            ).all()
        assert [tuple(r) for r in rows] == [("qualify_lead", "queued", 5)]
    finally:
        command.upgrade(config, "head")
        with db_engine.begin() as conn:
            conn.execute(text("TRUNCATE leads CASCADE"))


def test_downgrade_from_0002_returns_worker_states_to_received(db_engine: Engine) -> None:
    config = alembic_config(TEST_DATABASE_URL)
    with db_engine.begin() as conn:
        conn.execute(text("TRUNCATE leads CASCADE"))
        conn.execute(
            text(
                "INSERT INTO leads (email_normalized, name, status) VALUES "
                "('q@example.com', 'Q', 'qualified'), ('r@example.com', 'R', 'needs_review'), "
                "('f@example.com', 'F', 'processing_failed')"
            )
        )
    try:
        command.downgrade(config, "0001")
        with db_engine.connect() as conn:
            statuses: set[str] = set(conn.execute(text("SELECT status FROM leads")).scalars())
        assert statuses == {"received"}
        command.upgrade(config, "head")
        with db_engine.connect() as conn:
            jobs_count: int = conn.execute(text("SELECT count(*) FROM jobs")).scalar_one()
        assert jobs_count == 3  # the backfill picks all of them up again
    finally:
        command.upgrade(config, "head")
        with db_engine.begin() as conn:
            conn.execute(text("TRUNCATE leads CASCADE"))


def test_upgrade_to_0004_gives_leads_qualified_before_drafting_a_draft_job(
    db_engine: Engine,
) -> None:
    config = alembic_config(TEST_DATABASE_URL)
    with db_engine.begin() as conn:
        conn.execute(text("TRUNCATE leads CASCADE"))
    command.downgrade(config, "0003")
    try:
        with db_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO leads (email_normalized, name, status) VALUES "
                    "('q@example.com', 'Q', 'qualified'), ('r@example.com', 'R', 'needs_review')"
                )
            )
        command.upgrade(config, "head")
        with db_engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT l.email_normalized, j.kind, j.status FROM jobs j "
                    "JOIN leads l ON l.id = j.lead_id ORDER BY 1"
                )
            ).all()
        assert [tuple(r) for r in rows] == [("q@example.com", "draft_follow_up", "queued")]
    finally:
        command.upgrade(config, "head")
        with db_engine.begin() as conn:
            conn.execute(text("TRUNCATE leads CASCADE"))


def test_downgrade_from_0004_returns_review_and_sending_states_to_qualified(
    db_engine: Engine,
) -> None:
    config = alembic_config(TEST_DATABASE_URL)
    reviewed = ("awaiting_approval", "approved", "rejected", "sent", "send_failed")
    with db_engine.begin() as conn:
        conn.execute(text("TRUNCATE leads CASCADE"))
        for status in (*reviewed, "needs_review"):
            conn.execute(
                text("INSERT INTO leads (email_normalized, name, status) VALUES (:e, 'L', :s)"),
                {"e": f"{status}@example.com", "s": status},
            )
        # processing_failed while drafting goes back; while qualifying it stays.
        for email, kind in [("draft@example.com", "draft_follow_up"), ("q@example.com", None)]:
            lead_id: UUID = conn.execute(
                text(
                    "INSERT INTO leads (email_normalized, name, status) "
                    "VALUES (:e, 'L', 'processing_failed') RETURNING id"
                ),
                {"e": email},
            ).scalar_one()
            conn.execute(
                text(
                    "INSERT INTO jobs (kind, lead_id, status, max_attempts) "
                    "VALUES (:k, :id, 'failed', 5)"
                ),
                {"k": kind or "qualify_lead", "id": lead_id},
            )
    try:
        command.downgrade(config, "0003")
        with db_engine.connect() as conn:
            statuses: dict[str, str] = {
                row.email_normalized: row.status
                for row in conn.execute(text("SELECT email_normalized, status FROM leads"))
            }
            kinds: set[str] = set(conn.execute(text("SELECT kind FROM jobs")).scalars())
        assert statuses == {
            **{f"{status}@example.com": "qualified" for status in reviewed},
            "needs_review@example.com": "needs_review",
            "draft@example.com": "qualified",
            "q@example.com": "processing_failed",
        }
        assert kinds == {"qualify_lead"}
    finally:
        command.upgrade(config, "head")
        with db_engine.begin() as conn:
            conn.execute(text("TRUNCATE leads CASCADE"))


def test_upgrade_to_0005_asks_for_a_review_of_drafts_already_waiting(db_engine: Engine) -> None:
    from sales_ops.drafting import draft_sha256

    config = alembic_config(TEST_DATABASE_URL)
    with db_engine.begin() as conn:
        conn.execute(text("TRUNCATE leads CASCADE"))
    command.downgrade(config, "0004")
    try:
        with db_engine.begin() as conn:
            for email, status, versions in [
                ("waiting@example.com", "awaiting_approval", 2),
                ("approved@example.com", "approved", 1),
            ]:
                lead_id: UUID = conn.execute(
                    text(
                        "INSERT INTO leads (email_normalized, name, status) "
                        "VALUES (:e, 'L', :s) RETURNING id"
                    ),
                    {"e": email, "s": status},
                ).scalar_one()
                for version in range(1, versions + 1):
                    subject, body = f"Subject {version}", f"Body {version}"
                    conn.execute(
                        text(
                            "INSERT INTO drafts (lead_id, version, subject, body, sha256, "
                            "created_by) VALUES (:l, :v, :s, :b, :h, 'llm')"
                        ),
                        {
                            "l": lead_id,
                            "v": version,
                            "s": subject,
                            "b": body,
                            "h": draft_sha256(subject, body),
                        },
                    )
        command.upgrade(config, "head")
        with db_engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT l.email_normalized, d.version FROM review_requests r "
                    "JOIN leads l ON l.id = r.lead_id JOIN drafts d ON d.id = r.draft_id"
                )
            ).all()
        assert [tuple(r) for r in rows] == [("waiting@example.com", 2)]
        command.downgrade(config, "0004")
        with db_engine.connect() as conn:
            left: int = conn.execute(
                text("SELECT count(*) FROM pg_tables WHERE tablename = 'review_requests'")
            ).scalar_one()
        assert left == 0
    finally:
        command.upgrade(config, "head")
        with db_engine.begin() as conn:
            conn.execute(text("TRUNCATE leads CASCADE"))
