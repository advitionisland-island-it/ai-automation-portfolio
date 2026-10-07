"""Scripted LLM for tests: returns or raises the given steps in order; the last step repeats."""

import json
import threading
from collections.abc import Callable
from typing import Any

from sales_ops.llm import LLMRequest, LLMResponse, StopReason

Step = LLMResponse | Exception | Callable[[LLMRequest], LLMResponse]


def raw(text: str | None, stop_reason: StopReason = "end") -> LLMResponse:
    return LLMResponse(text=text, stop_reason=stop_reason, input_tokens=420, output_tokens=85)


def output(**overrides: Any) -> LLMResponse:
    body: dict[str, Any] = {
        "score": 82,
        "reasons": ["Clear budget and timeline.", "Company fits the target segment."],
        "summary": "Mid-size software company planning to buy this quarter.",
        "confidence": 0.9,
    }
    body.update(overrides)
    return raw(json.dumps(body))


def draft_output(**overrides: Any) -> LLMResponse:
    body: dict[str, Any] = {
        "subject": "Your question about lead automation",
        "body": (
            "Hello,\n\nThank you for your message about automating your lead intake. "
            "We would be glad to show you how it works in a short call.\n\nThe Sales Team"
        ),
    }
    body.update(overrides)
    return raw(json.dumps(body))


def text_of(response: LLMResponse) -> str:
    """The text of a scripted response, for tests that change it character by character."""
    assert response.text is not None
    return response.text


class ScriptedLLM:
    provider = "fake"

    def __init__(self, *steps: Step, model: str = "fake-scripted") -> None:
        self.model = model
        self._steps = list(steps)
        self._lock = threading.Lock()
        self.requests: list[LLMRequest] = []

    def complete(self, request: LLMRequest) -> LLMResponse:
        with self._lock:
            step = self._steps[min(len(self.requests), len(self._steps) - 1)]
            self.requests.append(request)
        if isinstance(step, Exception):
            raise step
        if isinstance(step, LLMResponse):
            return step
        return step(request)
