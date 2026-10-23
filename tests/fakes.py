"""Test doubles for the model: an in-process LLM and a fake Anthropic SDK client."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from loom.llm import LLMRequest


@dataclass
class FakeLLM:
    """Answers requests with a function of the request; records every call."""

    responder: Callable[[LLMRequest], dict[str, Any]]
    model: str = "fake-model"
    stats: Counter[str] = field(default_factory=Counter)
    calls: list[LLMRequest] = field(default_factory=list)

    def complete(self, request: LLMRequest) -> dict[str, Any]:
        self.calls.append(request)
        self.stats["api_calls"] += 1
        return self.responder(request)

    def labels(self, kind: str | None = None) -> list[str]:
        return [c.label for c in self.calls if kind is None or c.kind == kind]


def message(
    output: Any = None,
    *,
    stop_reason: str = "end_turn",
    text: str | None = None,
    model: str = "claude-sonnet-5-5",
    category: str | None = None,
) -> SimpleNamespace:
    content = [SimpleNamespace(type="thinking", thinking="")]
    if text is not None or output is not None:
        content.append(SimpleNamespace(type="text", text=text or json.dumps(output)))
    return SimpleNamespace(
        stop_reason=stop_reason,
        stop_details=SimpleNamespace(category=category) if category else None,
        content=content,
        model=model,
        usage=SimpleNamespace(
            input_tokens=100,
            output_tokens=20,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=80,
        ),
    )


class FakeSDK:
    """Mimics ``anthropic.Anthropic().beta.messages.stream(...)``.

    ``script`` items are either a message to return or an exception to raise, in order.
    """

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.requests: list[dict[str, Any]] = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))

    @contextmanager
    def _stream(self, **kwargs: Any) -> Iterator[SimpleNamespace]:
        self.requests.append(kwargs)
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        yield SimpleNamespace(get_final_message=lambda: item)
