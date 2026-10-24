"""Test doubles for the model: an in-process LLM and a fake Anthropic SDK client."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
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


SYNTHETIC = Path(__file__).parent / "fixtures" / "synthetic"


def synthetic_outputs() -> dict[tuple[str, str], dict[str, Any]]:
    """Hand-written model outputs by (kind, label), from tests/fixtures/synthetic.

    They exercise Loom's mechanics with controlled answers; they are not model recordings.
    For ambiguity, the entry with questions is kept (the empty re-scan ones are implied).
    """
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for path in sorted(SYNTHETIC.glob("*.json")):
        entry = json.loads(path.read_text())
        key = (entry["kind"], entry["label"])
        if entry["kind"] == "ambiguity" and not entry["output"]["ambiguities"]:
            continue
        out[key] = entry["output"]
    return out


def synthetic_llm(
    overrides: dict[tuple[str, str], Any] | None = None, *, ask: bool = False
) -> FakeLLM:
    """A FakeLLM answering from the synthetic outputs by (kind, label).

    Ambiguity scans return no questions unless ``ask`` is set; then a block gets its
    synthetic questions until its prompt shows a ``# decided:`` line.
    ``overrides`` maps (kind, label) to an output dict, or to a list of outputs used in turn.
    """
    outputs = synthetic_outputs()
    queues = {k: (list(v) if isinstance(v, list) else v) for k, v in (overrides or {}).items()}

    def respond(request: LLMRequest) -> dict[str, Any]:
        key = (request.kind, request.label)
        if key in queues:
            value = queues[key]
            if isinstance(value, list):
                return value.pop(0) if len(value) > 1 else value[0]
            return value
        if request.kind == "ambiguity":
            if ask and "# decided:" not in request.prompt and key in outputs:
                return outputs[key]
            return {"ambiguities": []}
        return outputs[key]

    return FakeLLM(respond)
