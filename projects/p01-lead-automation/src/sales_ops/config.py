from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Runtime configuration. Every value comes from environment variables.

    DATABASE_URL has no default on purpose: a deployment that forgets to set it
    should fail at startup instead of silently pointing at a developer database.
    """

    database_url: str
    db_connect_timeout_s: int = 2
    # Where people open the review page; used for links in the emails n8n sends (M4).
    public_base_url: str = "http://127.0.0.1:8000"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # values are read from the environment
