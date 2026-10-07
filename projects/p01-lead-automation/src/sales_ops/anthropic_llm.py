"""Anthropic adapter (UD-4). The only module that knows the Anthropic SDK.

- SDK retries are off (`max_retries=0`). The worker owns every retry, so each HTTP attempt is
  one row in `llm_calls` with its own outcome, tokens and cost (U5).
- The request asks for JSON that matches the output schema (structured outputs). The API
  accepts only a subset of JSON Schema, so the unsupported keywords are removed here. The full
  constraints are still enforced on the raw text by qualification.py.
- SDK errors become TransientLLMError (429, 408, 409, 5xx, timeout, connection) or
  PermanentLLMError (any other 4xx), with the HTTP status when a response arrived.
- Credentials are resolved by the SDK from the environment (ANTHROPIC_API_KEY). This module
  never reads, stores or logs them.
"""

from typing import Any

import anthropic
from anthropic.types import TextBlock

from sales_ops.llm import LLMRequest, LLMResponse, PermanentLLMError, StopReason, TransientLLMError

# Documented as unsupported by the structured-output API; qualification.py enforces them.
_UNSUPPORTED_SCHEMA_KEYWORDS = frozenset(
    {
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
        "uniqueItems",
    }
)
# Keys whose values map names to sub-schemas: the names themselves are never keywords.
_SCHEMA_MAPS = frozenset({"properties", "$defs", "definitions"})

_STOP_REASONS: dict[str, StopReason] = {
    "end_turn": "end",
    "stop_sequence": "end",
    "max_tokens": "max_tokens",
    "refusal": "refusal",
}


def api_schema(node: Any) -> Any:
    """A copy of a JSON Schema without the keywords the structured-output API rejects."""
    if isinstance(node, list):
        return [api_schema(item) for item in node]
    if not isinstance(node, dict):
        return node
    result: dict[str, Any] = {}
    for key, value in node.items():
        if key in _UNSUPPORTED_SCHEMA_KEYWORDS:
            continue
        if key in _SCHEMA_MAPS and isinstance(value, dict):
            result[key] = {name: api_schema(sub) for name, sub in value.items()}
        else:
            result[key] = api_schema(value)
    return result


class AnthropicLLM:
    provider = "anthropic"

    def __init__(
        self,
        model: str,
        *,
        timeout_s: float,
        http_client: anthropic.DefaultHttpxClient | None = None,
        api_key: str | None = None,
    ) -> None:
        self.model = model
        self._client = anthropic.Anthropic(
            max_retries=0, timeout=timeout_s, http_client=http_client, api_key=api_key
        )

    def complete(self, request: LLMRequest) -> LLMResponse:
        try:
            message = self._client.messages.create(
                model=self.model,
                max_tokens=request.max_output_tokens,
                system=request.system,
                messages=[{"role": "user", "content": request.user}],
                output_config={
                    "format": {"type": "json_schema", "schema": api_schema(request.output_schema)}
                },
            )
        except anthropic.RateLimitError as exc:
            raise TransientLLMError("rate_limit", http_status=exc.status_code) from exc
        except anthropic.APIStatusError as exc:
            # Classify by status code, not by exception class: in SDK 1.8 a 529 raises
            # OverloadedError and a 503 ServiceUnavailableError, and neither is an
            # InternalServerError (evidence/p01/M2/diagnosis-529.txt).
            status = exc.status_code
            if status == 408:
                raise TransientLLMError("timeout", http_status=status) from exc
            if status == 409 or status >= 500:
                raise TransientLLMError("server_error", http_status=status) from exc
            kind = getattr(exc, "type", None) or f"http_{status}"
            raise PermanentLLMError(kind, http_status=status) from exc
        except anthropic.APITimeoutError as exc:  # before APIConnectionError: it is a subclass
            raise TransientLLMError("timeout") from exc
        except anthropic.APIConnectionError as exc:
            raise TransientLLMError("connection") from exc

        text = "".join(block.text for block in message.content if isinstance(block, TextBlock))
        return LLMResponse(
            text=text,
            # Anything unexpected is passed on as "end": the output validation then decides.
            stop_reason=_STOP_REASONS.get(message.stop_reason or "", "end"),
            input_tokens=message.usage.input_tokens,
            output_tokens=message.usage.output_tokens,
        )
