"""JSON logging (D-10). Every line is one JSON object; email addresses are masked everywhere.

structlog and the standard library (uvicorn, sqlalchemy, ...) share one handler and one set of
processors, so masking also applies to messages that did not come from our own code.
"""

import logging
import os
import re
import sys
from collections.abc import MutableMapping
from typing import Any, TextIO

import structlog

_EMAIL = re.compile(r"([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)")


def mask_email(text: str) -> str:
    """'jane.doe@example.com' -> 'j***@example.com'."""
    return _EMAIL.sub(lambda m: f"{m.group(1)}***@{m.group(2)}", text)


def _mask(value: Any) -> Any:
    if isinstance(value, str):
        return mask_email(value)
    if isinstance(value, dict):
        return {k: _mask(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_mask(v) for v in value]
    return value


def _mask_emails(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    return {key: _mask(value) for key, value in event_dict.items()}


def configure_logging(stream: TextIO | None = None, level: str | None = None) -> None:
    """Install the JSON handler on the root logger. Safe to call more than once."""
    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.dict_tracebacks,
        _mask_emails,
    ]
    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(),
        ],
    )
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(formatter)
    handler.set_name("sales_ops_json")

    root = logging.getLogger()
    for existing in list(root.handlers):
        if existing.get_name() == "sales_ops_json":
            root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level or os.environ.get("LOG_LEVEL", "INFO"))

    # uvicorn installs its own plain-text handlers; route its server logs through the JSON handler.
    for name in ("uvicorn", "uvicorn.error"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True
    # Request lines come from our middleware (with request_id). The access log would duplicate
    # them and record client addresses, so it stays off even if uvicorn was started with it on.
    access_logger = logging.getLogger("uvicorn.access")
    access_logger.handlers.clear()
    access_logger.propagate = False
