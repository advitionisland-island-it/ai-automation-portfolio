"""The Anthropic adapter against a mocked HTTP transport: no network, no key, no cost."""

import json
from collections.abc import Callable
from typing import Any

import anthropic
import httpx2
import pytest

from sales_ops.anthropic_llm import AnthropicLLM, api_schema
from sales_ops.llm import PermanentLLMError, TransientLLMError
from sales_ops.qualification import LeadContext, LLMAssessment, build_request

Handler = Callable[[httpx2.Request], httpx2.Response]


def make_llm(handler: Handler) -> tuple[AnthropicLLM, list[httpx2.Request]]:
    seen: list[httpx2.Request] = []

    def record(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return handler(request)

    http_client = anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(record))
    llm = AnthropicLLM("test-model", timeout_s=5, http_client=http_client, api_key="test-key")
    return llm, seen


def message(
    content: list[dict[str, Any]], stop_reason: str = "end_turn", usage: tuple[int, int] = (321, 54)
) -> httpx2.Response:
    return httpx2.Response(
        200,
        json={
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "model": "test-model",
            "content": content,
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {"input_tokens": usage[0], "output_tokens": usage[1]},
        },
    )


def text(value: str) -> dict[str, Any]:
    return {"type": "text", "text": value}


def error(status: int, error_type: str) -> httpx2.Response:
    return httpx2.Response(
        status, json={"type": "error", "error": {"type": error_type, "message": "test"}}
    )


REQUEST = build_request(
    LeadContext(
        company="Synthetic Test Co",
        email_domain="example.com",
        company_profile=None,
        messages=[("web_form", "Synthetic inquiry text.")],
    ),
    max_output_tokens=4096,
)


def test_request_carries_model_prompt_and_an_api_compatible_schema() -> None:
    llm, seen = make_llm(lambda _r: message([text('{"score": 1}')]))
    llm.complete(REQUEST)

    [sent] = seen
    body = json.loads(sent.content)
    assert body["model"] == "test-model"
    assert body["max_tokens"] == 4096
    assert body["system"] == REQUEST.system
    assert body["messages"] == [{"role": "user", "content": REQUEST.user}]
    schema = body["output_config"]["format"]["schema"]
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {"score", "reasons", "summary", "confidence"}
    unsupported = {"minimum", "maximum", "minLength", "maxLength", "minItems", "maxItems"}
    assert not unsupported & set(_all_keys(schema))
    # The full constraints still exist on our side; only the API copy is reduced.
    assert "maximum" in json.dumps(LLMAssessment.model_json_schema())


def test_success_returns_text_stop_reason_and_usage() -> None:
    llm, _ = make_llm(lambda _r: message([text('{"a": '), text("1}")], usage=(700, 150)))
    response = llm.complete(REQUEST)
    assert (response.text, response.stop_reason) == ('{"a": 1}', "end")
    assert (response.input_tokens, response.output_tokens) == (700, 150)


def test_thinking_blocks_are_not_part_of_the_output_text() -> None:
    thinking = {"type": "thinking", "thinking": "hidden", "signature": "sig"}
    llm, _ = make_llm(lambda _r: message([thinking, text('{"score": 5}')]))
    assert llm.complete(REQUEST).text == '{"score": 5}'


@pytest.mark.parametrize(
    ("api_stop_reason", "stop_reason"),
    [("end_turn", "end"), ("max_tokens", "max_tokens"), ("refusal", "refusal")],
)
def test_stop_reasons_are_mapped(api_stop_reason: str, stop_reason: str) -> None:
    llm, _ = make_llm(lambda _r: message([text("x")], stop_reason=api_stop_reason))
    assert llm.complete(REQUEST).stop_reason == stop_reason


@pytest.mark.parametrize(
    ("status", "error_type", "kind"),
    [
        (429, "rate_limit_error", "rate_limit"),
        (500, "api_error", "server_error"),
        (503, "api_error", "server_error"),
        (529, "overloaded_error", "server_error"),
        (408, "timeout_error", "timeout"),
        (409, "conflict_error", "server_error"),
    ],
)
def test_retryable_http_errors_are_transient_and_not_retried_by_the_sdk(
    status: int, error_type: str, kind: str
) -> None:
    llm, seen = make_llm(lambda _r: error(status, error_type))
    with pytest.raises(TransientLLMError) as caught:
        llm.complete(REQUEST)
    assert (caught.value.kind, caught.value.http_status) == (kind, status)
    assert len(seen) == 1  # max_retries=0: the worker, not the SDK, decides about retries


@pytest.mark.parametrize(
    ("status", "error_type"),
    [
        (400, "invalid_request_error"),
        (401, "authentication_error"),
        (403, "permission_error"),
        (404, "not_found_error"),
    ],
)
def test_other_http_errors_are_permanent(status: int, error_type: str) -> None:
    llm, seen = make_llm(lambda _r: error(status, error_type))
    with pytest.raises(PermanentLLMError) as caught:
        llm.complete(REQUEST)
    assert (caught.value.kind, caught.value.http_status) == (error_type, status)
    assert len(seen) == 1


@pytest.mark.parametrize(
    ("exception", "kind"),
    [
        (httpx2.ReadTimeout("slow"), "timeout"),
        (httpx2.ConnectError("refused"), "connection"),
    ],
)
def test_network_failures_are_transient_without_http_status(
    exception: Exception, kind: str
) -> None:
    def fail(_request: httpx2.Request) -> httpx2.Response:
        raise exception

    llm, seen = make_llm(fail)
    with pytest.raises(TransientLLMError) as caught:
        llm.complete(REQUEST)
    assert (caught.value.kind, caught.value.http_status) == (kind, None)
    assert len(seen) == 1


def test_api_schema_keeps_properties_whose_names_look_like_keywords() -> None:
    schema = {
        "type": "object",
        "properties": {"maximum": {"type": "integer", "maximum": 5}, "n": {"minimum": 0}},
        "required": ["maximum"],
        "additionalProperties": False,
    }
    assert api_schema(schema) == {
        "type": "object",
        "properties": {"maximum": {"type": "integer"}, "n": {}},
        "required": ["maximum"],
        "additionalProperties": False,
    }


def _all_keys(node: Any) -> list[str]:
    if isinstance(node, dict):
        keys = [k for k in node if k != "properties"]
        children = (
            list(node["properties"].values()) + [v for k, v in node.items() if k != "properties"]
            if "properties" in node
            else list(node.values())
        )
        return keys + [key for child in children for key in _all_keys(child)]
    if isinstance(node, list):
        return [key for item in node for key in _all_keys(item)]
    return []
