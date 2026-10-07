"""Provider-neutral LLM interface (UD-4: no provider or model is fixed in code).

Business logic depends on `LLMClient` only. Adapters translate provider responses and errors
into `LLMResponse`, `TransientLLMError` and `PermanentLLMError`. Whatever an adapter returns is
treated as untrusted input by qualification.py.
"""

from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

TransientKind = Literal["rate_limit", "timeout", "server_error", "connection"]
StopReason = Literal["end", "refusal", "max_tokens"]


@dataclass(frozen=True)
class LLMRequest:
    system: str
    user: str
    # JSON Schema of the expected output, for providers that can constrain their output.
    # Constraining is a convenience; the output is validated regardless.
    output_schema: dict[str, Any]
    max_output_tokens: int = 1024


@dataclass(frozen=True)
class LLMResponse:
    text: str | None
    stop_reason: StopReason
    input_tokens: int | None
    output_tokens: int | None


class TransientLLMError(Exception):
    """Worth retrying later: rate limit, timeout, provider 5xx, connection failure."""

    def __init__(
        self, kind: TransientKind, detail: str = "", http_status: int | None = None
    ) -> None:
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind: TransientKind = kind
        self.http_status = http_status  # None when no HTTP response arrived (timeout, network)


class PermanentLLMError(Exception):
    """Retrying will not help: authentication, invalid request, unsupported model."""

    def __init__(self, kind: str, detail: str = "", http_status: int | None = None) -> None:
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind = kind
        self.http_status = http_status


@runtime_checkable
class LLMClient(Protocol):
    provider: str
    model: str

    def complete(self, request: LLMRequest) -> LLMResponse: ...
