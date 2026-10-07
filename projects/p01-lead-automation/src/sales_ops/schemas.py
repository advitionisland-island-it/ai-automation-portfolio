from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, BeforeValidator, ConfigDict, EmailStr, Field, field_validator

from sales_ops.drafting import MAX_BODY, MAX_SUBJECT
from sales_ops.text import find_control_character

# RFC 5321 limits. email-validator 2.3 checks the domain side but not the local part
# (evidence/p01/M1/diagnosis-email-length.txt), so both limits are enforced here.
MAX_EMAIL_LOCAL_PART = 64
MAX_EMAIL_LENGTH = 254


def _no_control_characters(value: object) -> object:
    """Free text from outside follows the rule for LLM output (sales_ops.text): a control
    character makes it invalid (422). Checked before whitespace is stripped (U17, F2)."""
    if isinstance(value, str) and (found := find_control_character(value)) is not None:
        raise ValueError(f"contains the control character U+{ord(found):04X}")
    return value


PlainText = BeforeValidator(_no_control_characters)


class LeadIn(BaseModel):
    """Inbound inquiry, as posted by the n8n inbound workflow (or curl).

    Unknown fields are rejected so that a changed upstream form fails loudly instead of
    silently dropping data.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    email: EmailStr
    name: Annotated[str, Field(min_length=1, max_length=200), PlainText]
    company: Annotated[str | None, Field(max_length=200), PlainText] = None
    message: Annotated[str, Field(min_length=1, max_length=5000), PlainText]
    source: Annotated[str, Field(min_length=1, max_length=50, pattern=r"^[a-z0-9_]+$")] = "web_form"

    @field_validator("email")
    @classmethod
    def _email_within_rfc_limits(cls, value: str) -> str:
        local_part = value.rpartition("@")[0]
        if len(local_part) > MAX_EMAIL_LOCAL_PART or len(value) > MAX_EMAIL_LENGTH:
            raise ValueError(
                f"email is too long (at most {MAX_EMAIL_LOCAL_PART} characters before the @ "
                f"and {MAX_EMAIL_LENGTH} in total)"
            )
        return value


class LeadAccepted(BaseModel):
    lead_id: UUID
    inquiry_id: UUID
    lead_created: bool
    status: str


# Review API (M3). A reviewer name is recorded with every decision. P01 runs locally without
# authentication, so the name is what the caller says it is (README, Security).
Reviewer = Annotated[str, Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9._-]+$")]


class DraftEdit(BaseModel):
    """A human edit. It becomes a new draft version; an approval of the old text is revoked."""

    model_config = ConfigDict(extra="forbid")

    subject: Annotated[str, Field(min_length=1, max_length=MAX_SUBJECT)]
    body: Annotated[str, Field(min_length=1, max_length=MAX_BODY)]
    edited_by: Reviewer


class ApprovalIn(BaseModel):
    """Approve exactly the text that was reviewed: its sha256 comes from GET /v1/leads/{id}."""

    model_config = ConfigDict(extra="forbid")

    draft_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    approved_by: Reviewer


class RejectionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rejected_by: Reviewer
    reason: Annotated[str, Field(max_length=400), PlainText] = ""


class AssessmentOut(BaseModel):
    score: int
    tier: str
    summary: str
    reasons: list[str]
    confidence: float


class DraftOut(BaseModel):
    version: int
    subject: str
    body: str
    sha256: str
    created_by: str


class ApprovalOut(BaseModel):
    approved_by: str
    approved_at: datetime
    draft_sha256: str


class SentOut(BaseModel):
    sent_at: datetime
    message_id: str


class LeadView(BaseModel):
    lead_id: UUID
    status: str
    status_reason: str | None
    recipient: str
    company: str | None
    assessment: AssessmentOut | None
    draft: DraftOut | None
    approval: ApprovalOut | None
    sent: SentOut | None


# Notifications for n8n (M4)


class ReviewRequestOut(BaseModel):
    id: UUID
    lead_id: UUID
    company: str | None
    tier: str | None
    score: int | None
    summary: str | None
    draft_version: int
    draft_subject: str
    review_url: str
    created_at: datetime


class AlertOut(BaseModel):
    id: UUID
    kind: str
    lead_id: UUID | None
    company: str | None
    job_kind: str | None
    reason: str | None
    attempts: int | None
    lead_url: str | None
    created_at: datetime
