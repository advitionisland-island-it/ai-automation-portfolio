"""Which LLM provider to use: the only module that maps provider names to adapters.

Business logic (ingest, qualification, jobs, states, worker) depends on the `LLMClient`
interface only. The canonical runtime for development is the fake provider (U6); a real
adapter is imported lazily, only when it is selected, so the fake runtime never loads a
provider SDK.
"""

from typing import Literal, Self

from pydantic import model_validator
from pydantic_settings import BaseSettings

from sales_ops.fake_llm import HeuristicFakeLLM
from sales_ops.llm import LLMClient


class ProviderSettings(BaseSettings):
    # "fake" is the canonical runtime (U6). "anthropic" was approved in UD-4; validating it
    # against the real API (AC-2.7) is deferred.
    llm_provider: Literal["fake", "anthropic"]
    llm_model: str | None = None
    llm_timeout_s: float = 60.0

    @model_validator(mode="after")
    def _real_provider_needs_model(self) -> Self:
        if self.llm_provider != "fake" and not self.llm_model:
            raise ValueError("LLM_MODEL is required for a real provider")
        return self


def build_llm_client(settings: ProviderSettings) -> LLMClient:
    if settings.llm_provider == "fake":
        return HeuristicFakeLLM(model=settings.llm_model or "fake-heuristic-v1")
    if settings.llm_provider == "anthropic" and settings.llm_model:
        from sales_ops.anthropic_llm import AnthropicLLM  # only when selected

        return AnthropicLLM(settings.llm_model, timeout_s=settings.llm_timeout_s)
    raise ValueError(f"unsupported LLM_PROVIDER: {settings.llm_provider}")
