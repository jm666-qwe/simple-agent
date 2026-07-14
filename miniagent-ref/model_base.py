from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Protocol

from pydantic import BaseModel, Field

from miniagent.messages import Message


class ModelRequest(BaseModel):
    messages: list[Message]
    tools: list[dict[str, Any]] = Field(default_factory=list)
    system_prompt: str = ""
    meta: dict[str, Any] = Field(default_factory=dict)


class ModelUsage(BaseModel):
    provider: str
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None


class ModelEvent(BaseModel):
    type: str
    data: dict[str, Any] = Field(default_factory=dict)


class ProviderResponse(BaseModel):
    message: Message
    usage: ModelUsage | None = None


class ModelClient(Protocol):
    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        raise NotImplementedError


class ModelProviderError(RuntimeError):
    def __init__(self, provider: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.provider = provider
        self.retryable = retryable


def normalize_model_error(provider: str, exc: Exception) -> ModelProviderError:
    if isinstance(exc, ModelProviderError):
        return exc
    return ModelProviderError(provider, str(exc), retryable=False)
