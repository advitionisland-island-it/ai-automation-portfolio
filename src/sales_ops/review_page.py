"""The review page (D-13, D-66): server-rendered HTML for the person who approves drafts.

Reading never changes anything. Every change is a POSTed form that carries the sha256 of the
draft the reviewer saw. A stale page is refused with 409, and so is a form posted from another
site, which cannot know the text. Form fields go through the same models as the JSON API.
"""

from collections.abc import Callable
from pathlib import Path
from typing import Annotated
from uuid import UUID

import jinja2
from fastapi import APIRouter, Depends, Form, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError
from sqlalchemy import Engine

from sales_ops.approvals import ReviewError, approve, edit_draft, lead_view, reject
from sales_ops.db import get_engine
from sales_ops.schemas import ApprovalIn, DraftEdit, RejectionIn

router = APIRouter(include_in_schema=False)

_templates = jinja2.Environment(
    loader=jinja2.FileSystemLoader(Path(__file__).parent / "templates"),
    autoescape=True,  # draft and inquiry text come from outside: always escaped
    undefined=jinja2.StrictUndefined,
)
_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
        "frame-ancestors 'none'; base-uri 'none'"
    ),
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}
_RESULTS = {
    "approved": "Approved. The worker sends it shortly.",
    "rejected": "Rejected. Nothing will be sent to this lead.",
    "edited": "Saved as a new version. It needs an approval before it can be sent.",
}
EngineDep = Annotated[Engine, Depends(get_engine)]
FormText = Annotated[str, Form()]


def _page(
    engine: Engine,
    lead_id: UUID,
    *,
    status_code: int = 200,
    error: str | None = None,
    result: str | None = None,
) -> HTMLResponse:
    try:
        lead = lead_view(engine, lead_id)
    except ReviewError as exc:
        html = _templates.get_template("not_found.html").render(detail=exc.detail)
        return HTMLResponse(html, status_code=exc.status_code, headers=_HEADERS)
    html = _templates.get_template("review.html").render(
        lead=lead, error=error, result=_RESULTS.get(result or "")
    )
    return HTMLResponse(html, status_code=status_code, headers=_HEADERS)


def _act(engine: Engine, lead_id: UUID, result: str, action: Callable[[], None]) -> Response:
    """Run the change; on success go back to the page (303), otherwise show why."""
    try:
        action()
    except ValidationError as exc:
        fields = ", ".join(sorted({str(error["loc"][0]) for error in exc.errors()}))
        return _page(engine, lead_id, status_code=422, error=f"Check these fields: {fields}")
    except ReviewError as exc:
        return _page(engine, lead_id, status_code=exc.status_code, error=exc.detail)
    return RedirectResponse(f"/review/{lead_id}?result={result}", status_code=303)


@router.get("/review/{lead_id}", response_class=HTMLResponse)
def review_page(lead_id: UUID, engine: EngineDep, result: str | None = None) -> HTMLResponse:
    return _page(engine, lead_id, result=result)


@router.post("/review/{lead_id}/approve")
def approve_form(
    lead_id: UUID, engine: EngineDep, draft_sha256: FormText, reviewer: FormText
) -> Response:
    def action() -> None:
        form = ApprovalIn(draft_sha256=draft_sha256, approved_by=reviewer)
        approve(engine, lead_id, form.draft_sha256, form.approved_by)

    return _act(engine, lead_id, "approved", action)


@router.post("/review/{lead_id}/reject")
def reject_form(
    lead_id: UUID,
    engine: EngineDep,
    draft_sha256: FormText,
    reviewer: FormText,
    reason: Annotated[str, Form()] = "",
) -> Response:
    def action() -> None:
        form = RejectionIn(rejected_by=reviewer, reason=reason)
        reject(engine, lead_id, form.rejected_by, form.reason, seen_sha256=draft_sha256)

    return _act(engine, lead_id, "rejected", action)


@router.post("/review/{lead_id}/draft")
def edit_form(
    lead_id: UUID,
    engine: EngineDep,
    draft_sha256: FormText,
    subject: FormText,
    body: FormText,
    reviewer: FormText,
) -> Response:
    def action() -> None:
        form = DraftEdit(subject=subject, body=body, edited_by=reviewer)
        edit_draft(
            engine, lead_id, form.subject, form.body, form.edited_by, seen_sha256=draft_sha256
        )

    return _act(engine, lead_id, "edited", action)
