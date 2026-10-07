from alembic import context
from sqlalchemy import create_engine

from sales_ops.logging_config import configure_logging
from sales_ops.tables import metadata

config = context.config
configure_logging()


def _database_url() -> str:
    url = config.get_main_option("sqlalchemy.url")
    if url:
        return url
    from sales_ops.config import get_settings

    return get_settings().database_url


def run_migrations_online() -> None:
    engine = create_engine(_database_url())
    try:
        with engine.connect() as connection:
            context.configure(connection=connection, target_metadata=metadata, compare_type=True)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    raise SystemExit("Offline (SQL script) mode is not supported; run against a database.")
run_migrations_online()
