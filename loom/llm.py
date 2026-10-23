"""Thin wrapper around the Anthropic SDK.

Every model call in Loom goes through :class:`ClaudeClient.complete` with an
:class:`LLMRequest`. The client adds:

* structured output: the response is constrained to the request's JSON schema
  (``output_config.format``), so nothing is scraped out of markdown fences;
* prompt caching: the per-kind system prompt and the project context are one cached prefix;
* exponential-backoff retries on rate limits, overload, 5xx and connection errors;
* a disk cache keyed by ``(model, prompt hash)``, so identical requests never hit the API twice;
* mock mode (``LOOM_MOCK=1``): replay recorded responses from ``tests/fixtures/llm/`` and never
  touch the network; record mode (``LOOM_RECORD=1``): call the API and save the responses there.

No ``temperature`` is sent: current models reject it. Determinism comes from the cache and the
lockfile instead.
"""

from __future__ import annotations

import json
import os
import random
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from loom import __version__
from loom.errors import GenerationError, LoomError
from loom.hashing import canonical_json, sha256

DEFAULT_MODEL = "claude-sonnet-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
REPO_FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "llm"


def default_model() -> str:
    return os.environ.get("LOOM_MODEL") or DEFAULT_MODEL


@dataclass(frozen=True)
class LLMRequest:
    kind: str  # ambiguity | codegen | example_tests | rule_tests | repair
    label: str  # what the call is about, e.g. "does:chase_overdue_invoices"
    system: str  # stable instructions for this kind of call (cached)
    context: str  # project context shared by every block (cached)
    prompt: str  # the per-call content
    schema: dict[str, Any]  # JSON schema the output must follow
    effort: str = "medium"
    max_tokens: int = 32000

    def body(self, model: str) -> dict[str, Any]:
        """The request body sent to the Messages API (minus transport-only options)."""
        return {
            "model": model,
            "max_tokens": self.max_tokens,
            "system": [
                {"type": "text", "text": self.system},
                {"type": "text", "text": self.context, "cache_control": {"type": "ephemeral"}},
            ],
            "messages": [{"role": "user", "content": self.prompt}],
            "output_config": {
                "effort": self.effort,
                "format": {"type": "json_schema", "schema": self.schema},
            },
        }

    def key(self, model: str) -> str:
        """Cache / fixture key: a hash of everything that can change the answer."""
        return sha256(canonical_json({"kind": self.kind, "body": self.body(model)})).removeprefix(
            "sha256:"
        )[:32]


class LLM(Protocol):
    """What the compiler needs from a model. Tests substitute an in-process fake."""

    model: str
    stats: Counter[str]

    def complete(self, request: LLMRequest) -> dict[str, Any]: ...


class MockMissError(LoomError):
    """LOOM_MOCK=1 and no recorded response exists for a request."""


@dataclass
class ClaudeClient:
    model: str = field(default_factory=default_model)
    cache_dir: Path | None = None
    fixtures_dir: Path | None = None
    mock: bool = False
    record: bool = False
    use_fallbacks: bool = True
    max_attempts: int = 5
    sleep: Callable[[float], None] = time.sleep
    client_factory: Callable[[], Any] | None = None
    stats: Counter[str] = field(default_factory=Counter)
    _client: Any = None

    @classmethod
    def from_env(cls, model: str | None = None) -> ClaudeClient:
        cache = os.environ.get("LOOM_CACHE_DIR")
        fixtures = os.environ.get("LOOM_FIXTURES_DIR")
        return cls(
            model=model or default_model(),
            cache_dir=Path(cache) if cache else Path.home() / ".cache" / "loom",
            fixtures_dir=Path(fixtures) if fixtures else REPO_FIXTURES,
            mock=os.environ.get("LOOM_MOCK") == "1",
            record=os.environ.get("LOOM_RECORD") == "1",
            use_fallbacks=os.environ.get("LOOM_NO_FALLBACK") != "1",
        )

    # --- public --------------------------------------------------------------------------

    def complete(self, request: LLMRequest) -> dict[str, Any]:
        key = request.key(self.model)
        if self.mock:
            return self._replay(key, request)
        if not self.record:
            cached = self._read(self._cache_path(key))
            if cached is not None:
                self.stats["cache_hits"] += 1
                return dict(cached["output"])
        entry = self._call_api(key, request)
        self._write(self._cache_path(key), entry)
        if self.record:
            self._write(self._fixture_path(key), entry)
        return dict(entry["output"])

    # --- storage -------------------------------------------------------------------------

    def _cache_path(self, key: str) -> Path | None:
        return self.cache_dir / f"{key}.json" if self.cache_dir else None

    def _fixture_path(self, key: str) -> Path | None:
        return self.fixtures_dir / f"{key}.json" if self.fixtures_dir else None

    @staticmethod
    def _read(path: Path | None) -> dict[str, Any] | None:
        if path is None or not path.exists():
            return None
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return data

    @staticmethod
    def _write(path: Path | None, entry: dict[str, Any]) -> None:
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(entry, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    def _replay(self, key: str, request: LLMRequest) -> dict[str, Any]:
        entry = self._read(self._fixture_path(key))
        if entry is None:
            pending = None
            if self.fixtures_dir is not None:
                pending = self.fixtures_dir / "_pending" / f"{key}.request.json"
                self._write(
                    pending,
                    {
                        "key": key,
                        "kind": request.kind,
                        "label": request.label,
                        "request": request.body(self.model),
                    },
                )
            raise MockMissError(
                f"LOOM_MOCK=1 but no recorded {request.kind} response for {request.label} "
                f"(key {key}). Record it with LOOM_RECORD=1 and an ANTHROPIC_API_KEY"
                + (f"; the request was saved to {pending}" if pending else "")
            )
        self.stats["fixture_hits"] += 1
        return dict(entry["output"])

    # --- network -------------------------------------------------------------------------

    def _sdk(self) -> Any:
        if self._client is None:
            if self.client_factory is not None:
                self._client = self.client_factory()
            else:
                import anthropic

                # Retries are ours (below), so the SDK must not retry on its own as well.
                self._client = anthropic.Anthropic(max_retries=0)
        return self._client

    def _call_api(self, key: str, request: LLMRequest) -> dict[str, Any]:
        import anthropic

        body = request.body(self.model)
        extra: dict[str, Any] = {}
        if self.use_fallbacks:
            extra = {"betas": [FALLBACK_BETA], "fallbacks": "default"}
        retryable = (
            anthropic.RateLimitError,
            anthropic.OverloadedError,
            anthropic.ServiceUnavailableError,
            anthropic.InternalServerError,
            anthropic.APIConnectionError,
        )
        attempt = 0
        while True:
            attempt += 1
            try:
                self.stats["api_calls"] += 1
                with self._sdk().beta.messages.stream(**body, **extra) as stream:
                    message = stream.get_final_message()
                break
            except anthropic.BadRequestError as exc:
                hint = " (set LOOM_NO_FALLBACK=1 to call without fallbacks)" if extra else ""
                raise GenerationError(
                    f"the API rejected the {request.kind} request: {exc}{hint}"
                ) from exc
            except retryable as exc:
                if attempt >= self.max_attempts:
                    raise GenerationError(
                        f"{request.kind} for {request.label} failed after {attempt} attempts: {exc}"
                    ) from exc
                self.stats["retries"] += 1
                self.sleep(backoff_delay(attempt))
        return self._entry(key, request, message)

    def _entry(self, key: str, request: LLMRequest, message: Any) -> dict[str, Any]:
        if message.stop_reason == "refusal":
            details = getattr(message, "stop_details", None)
            category = getattr(details, "category", None)
            raise GenerationError(
                f"the model declined the {request.kind} request for {request.label}"
                + (f" (category: {category})" if category else "")
            )
        if message.stop_reason == "max_tokens":
            raise GenerationError(
                f"the {request.kind} output for {request.label} was cut off at "
                f"{request.max_tokens} tokens"
            )
        text = next((b.text for b in message.content if b.type == "text"), None)
        if text is None:
            raise GenerationError(f"no text output in the {request.kind} response")
        try:
            output = json.loads(text)
        except json.JSONDecodeError as exc:
            raise GenerationError(f"the {request.kind} output is not valid JSON: {exc}") from exc
        usage = message.usage
        return {
            "key": key,
            "kind": request.kind,
            "label": request.label,
            "source": "api",
            "model": self.model,
            "served_model": message.model,
            "loom_version": __version__,
            "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "usage": {
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "cache_read_input_tokens": usage.cache_read_input_tokens,
                "cache_creation_input_tokens": usage.cache_creation_input_tokens,
            },
            "request": request.body(self.model),
            "output": output,
        }


def backoff_delay(attempt: int, *, base: float = 1.0, cap: float = 30.0) -> float:
    """Exponential backoff with full jitter: attempt 1 -> [0.5, 1)s, 2 -> [1, 2)s, ..."""
    ceiling: float = min(cap, base * 2.0 ** (attempt - 1))
    return ceiling / 2 + random.random() * ceiling / 2
