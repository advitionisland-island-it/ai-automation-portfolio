"""Built-in fake provider (LLM_PROVIDER=fake), the canonical runtime for development (U6).

Lets the local stack and the e2e test run without an API key or cost. Its output is
deterministic, answers whichever schema was asked for (assessment or draft), and says plainly
that no real model was called. The worker validates it exactly like real output.
"""

import json
import zlib
from typing import Any

from sales_ops.llm import LLMRequest, LLMResponse


class HeuristicFakeLLM:
    provider = "fake"

    def __init__(self, model: str = "fake-heuristic-v1") -> None:
        self.model = model

    def complete(self, request: LLMRequest) -> LLMResponse:
        wants = set(request.output_schema.get("properties", {}))
        body = self._draft(request) if "subject" in wants else self._assessment(request)
        text = json.dumps(body)
        return LLMResponse(
            text=text,
            stop_reason="end",
            input_tokens=(len(request.system) + len(request.user)) // 4,
            output_tokens=len(text) // 4,
        )

    @staticmethod
    def _assessment(request: LLMRequest) -> dict[str, Any]:
        score = 20 + zlib.crc32(request.user.encode("utf-8")) % 71  # 20..90, stable per input
        return {
            "score": score,
            "reasons": ["Synthetic assessment from the fake provider; no real model was called."],
            "summary": "Fake provider output for local development and end-to-end tests.",
            "confidence": 0.9,
        }

    @staticmethod
    def _draft(_request: LLMRequest) -> dict[str, Any]:
        return {
            "subject": "Thank you for your inquiry",
            "body": (
                "Hello,\n\nThank you for reaching out. We would be glad to walk you through how "
                "we can help, and we will suggest a time for a short call.\n\n"
                "The Sales Team\n\n(Fake provider output: no real model was called.)"
            ),
        }
