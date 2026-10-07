from functools import lru_cache

from sqlalchemy import Engine, create_engine, text

from sales_ops.config import get_settings


def build_engine(database_url: str, connect_timeout_s: int = 2) -> Engine:
    return create_engine(
        database_url,
        pool_pre_ping=True,
        connect_args={"connect_timeout": connect_timeout_s},
    )


@lru_cache
def get_engine() -> Engine:
    settings = get_settings()
    return build_engine(settings.database_url, settings.db_connect_timeout_s)


def ping(engine: Engine) -> None:
    """Raise if the database cannot answer a trivial query."""
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
