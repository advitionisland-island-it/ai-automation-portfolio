"""Production entrypoint: `python -m sales_ops`.

Logging is configured before uvicorn starts, and uvicorn's own log config and access log are
turned off, so every line (startup included) is JSON and request lines carry a request_id.
"""

import os

import uvicorn

from sales_ops.logging_config import configure_logging


def main() -> None:
    configure_logging()
    uvicorn.run(
        "sales_ops.main:app",
        host=os.environ.get("API_HOST", "127.0.0.1"),
        port=int(os.environ.get("API_INTERNAL_PORT", "8000")),
        log_config=None,
        access_log=False,
    )


if __name__ == "__main__":
    main()
