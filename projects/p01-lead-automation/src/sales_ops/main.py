import re
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Annotated, Any
from uuid import UUID

import structlog
from fastapi import Depends, FastAPI, Header, Query, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError

from sales_ops.approvals import ReviewError, approve, edit_draft, lead_view, reject
from sales_ops.config import get_settings
from sales_ops.db import get_engine, ping
from sales_ops.ingest import IdempotencyKeyReused, ingest_lead
from sales_ops.logging_config import configure_logging
from sales_ops.notifications import (
    claim_alerts,
    claim_review_requests,
    mark_alert_delivered,
    mark_review_request_delivered,
)
from sales_ops.review_page import router as review_page_router
from sales_ops.schemas import (
    AlertOut,
    ApprovalIn,
    DraftEdit,
    LeadAccepted,
    LeadIn,
    LeadView,
    RejectionIn,
    ReviewRequestOut,
)

log = structlog.get_logger(__name__)

_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    # Fail fast: a missing or invalid setting stops startup instead of breaking the first request.
    get_settings()
    yield


app = FastAPI(title="Sales Ops Engine", version="0.1.0", lifespan=lifespan)
app.include_router(review_page_router)


@app.middleware("http")
async def request_context(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Give every request a request_id, bind it to all log lines, and log one line per request."""
    incoming = request.headers.get("x-request-id", "")
    request_id = incoming if _REQUEST_ID.match(incoming) else uuid.uuid4().hex
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(request_id=request_id)
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        log.exception("request.failed", method=request.method, path=request.url.path)
        raise
    response.headers["X-Request-ID"] = request_id
    log.info(
        "request.completed",
        method=request.method,
        path=request.url.path,
        status_code=response.status_code,
        duration_ms=round((time.perf_counter() - started) * 1000, 1),
    )
    return response


@app.exception_handler(ReviewError)
async def review_error(_request: Request, exc: ReviewError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.get("/healthz")
def healthz(engine: Annotated[Engine, Depends(get_engine)]) -> JSONResponse:
    """Liveness plus database reachability. Returns 503 when the database is unreachable."""
    try:
        ping(engine)
    except SQLAlchemyError as exc:
        log.warning("healthz.database_unreachable", error=type(exc).__name__)
        return JSONResponse(status_code=503, content={"status": "error", "db": "unreachable"})
    return JSONResponse(status_code=200, content={"status": "ok", "db": "ok"})


@app.post(
    "/v1/leads",
    status_code=201,
    response_model=LeadAccepted,
    responses={409: {"description": "Idempotency-Key reused with a different body"}},
)
def create_lead(
    payload: LeadIn,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=255)],
    engine: Annotated[Engine, Depends(get_engine)],
) -> JSONResponse:
    """Accept one inquiry. A retry with the same Idempotency-Key and body gets the same answer."""
    try:
        result = ingest_lead(engine, payload, idempotency_key)
    except IdempotencyKeyReused:
        return JSONResponse(
            status_code=409,
            content={"detail": "Idempotency-Key was already used with a different request body"},
        )
    headers = {"Idempotent-Replayed": "true"} if result.replayed else None
    return JSONResponse(status_code=result.status_code, content=result.body, headers=headers)


# Review (M3). Reading never changes anything; every change is a PUT or POST (D-13), so a mail
# client that prefetches links cannot approve by accident.

_REVIEW_ERRORS: dict[int | str, dict[str, Any]] = {
    404: {"description": "No such lead"},
    409: {"description": "The lead is not in a state that allows this, or the draft changed"},
}


@app.get("/v1/leads/{lead_id}", response_model=LeadView, responses=_REVIEW_ERRORS)
def get_lead(lead_id: UUID, engine: Annotated[Engine, Depends(get_engine)]) -> dict[str, Any]:
    """Status, assessment, current draft (with the sha256 to approve it by), approval, sending."""
    return lead_view(engine, lead_id)


@app.put("/v1/leads/{lead_id}/draft", response_model=LeadView, responses=_REVIEW_ERRORS)
def put_draft(
    lead_id: UUID, edit: DraftEdit, engine: Annotated[Engine, Depends(get_engine)]
) -> dict[str, Any]:
    """Replace the draft with a human edit. An approval of the previous text is revoked."""
    edit_draft(engine, lead_id, edit.subject, edit.body, edit.edited_by)
    return lead_view(engine, lead_id)


@app.post("/v1/leads/{lead_id}/approve", response_model=LeadView, responses=_REVIEW_ERRORS)
def post_approve(
    lead_id: UUID, approval: ApprovalIn, engine: Annotated[Engine, Depends(get_engine)]
) -> dict[str, Any]:
    """Approve the current draft, identified by its sha256. Approving it again is a no-op."""
    approve(engine, lead_id, approval.draft_sha256, approval.approved_by)
    return lead_view(engine, lead_id)


@app.post("/v1/leads/{lead_id}/reject", response_model=LeadView, responses=_REVIEW_ERRORS)
def post_reject(
    lead_id: UUID, rejection: RejectionIn, engine: Annotated[Engine, Depends(get_engine)]
) -> dict[str, Any]:
    """Reject the lead: nothing will be sent to it."""
    reject(engine, lead_id, rejection.rejected_by, rejection.reason)
    return lead_view(engine, lead_id)


# Notifications for n8n (M4). n8n claims a batch for a short lease, sends the emails, then marks
# each one delivered; an unmarked claim is handed out again after the lease (at-least-once).

_NOT_FOUND: dict[int | str, dict[str, Any]] = {404: {"description": "No such item"}}
BatchSize = Annotated[int, Query(ge=1, le=50)]


@app.post("/v1/review-requests/claim", response_model=list[ReviewRequestOut])
def claim_review_requests_route(
    engine: Annotated[Engine, Depends(get_engine)], limit: BatchSize = 20
) -> list[dict[str, Any]]:
    """Drafts waiting for a person, with the link to review them (n8n W2)."""
    return claim_review_requests(engine, limit, get_settings().public_base_url)


@app.post("/v1/review-requests/{request_id}/delivered", status_code=204, responses=_NOT_FOUND)
def review_request_delivered(
    request_id: UUID, engine: Annotated[Engine, Depends(get_engine)]
) -> Response:
    if not mark_review_request_delivered(engine, request_id):
        raise ReviewError(404, "review request not found")
    return Response(status_code=204)


@app.post("/v1/alerts/claim", response_model=list[AlertOut])
def claim_alerts_route(
    engine: Annotated[Engine, Depends(get_engine)], limit: BatchSize = 20
) -> list[dict[str, Any]]:
    """Failures a person must know about: processing_failed, send_failed (n8n W3)."""
    return claim_alerts(engine, limit, get_settings().public_base_url)


@app.post("/v1/alerts/{alert_id}/delivered", status_code=204, responses=_NOT_FOUND)
def alert_delivered(alert_id: UUID, engine: Annotated[Engine, Depends(get_engine)]) -> Response:
    if not mark_alert_delivered(engine, alert_id):
        raise ReviewError(404, "alert not found")
    return Response(status_code=204)
