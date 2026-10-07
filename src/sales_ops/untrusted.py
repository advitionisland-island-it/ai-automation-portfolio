"""The shared first steps for any LLM output (U4): the output is untrusted input.

    raw response -> schema validation -> (domain validation and normalization, per use)

`parse_output` stops at the schema step. Each use (qualification, drafting) adds its own
domain rules and normalization on top. Anything that fails a step never reaches the database.
"""

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ValidationError

from sales_ops.llm import LLMResponse

RejectionKind = Literal["refusal", "truncated", "empty", "invalid_json", "schema", "domain"]


@dataclass(frozen=True)
class Rejected:
    kind: RejectionKind


def parse_output[Schema: BaseModel](
    response: LLMResponse, schema: type[Schema]
) -> Schema | Rejected:
    if response.stop_reason == "refusal":
        return Rejected("refusal")
    if response.stop_reason == "max_tokens":
        return Rejected("truncated")
    text = (response.text or "").strip()
    if not text:
        return Rejected("empty")
    try:
        return schema.model_validate_json(text)
    except ValidationError as exc:
        invalid_json = any(error["type"] == "json_invalid" for error in exc.errors())
        return Rejected("invalid_json" if invalid_json else "schema")


def format_inquiries(messages: list[tuple[str, str]]) -> str:
    """Inquiry texts wrapped in delimiters, so the prompt can say "this is data"."""
    return "\n".join(
        f'<inquiry index="{i}" source="{source}">\n{_escape(message)}\n</inquiry>'
        for i, (source, message) in enumerate(messages, start=1)
    )


def _escape(message: str) -> str:
    # Keep inquiry text from closing its own delimiter and posing as prompt structure.
    return message.replace("</inquiry", "&lt;/inquiry")
