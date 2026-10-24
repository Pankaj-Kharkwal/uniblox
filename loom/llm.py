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
import shutil
import subprocess
import tempfile
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


# --- Claude Code backend --------------------------------------------------------------------

# Environment variables of a surrounding Claude Code session that must not leak into a child
# `claude -p` process (it would try to join that session instead of running on its own).
_KEEP_CLAUDE_VARS = {"CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR"}
_TRANSIENT = ("overloaded", "rate limit", "rate_limit", "529", "503", "timed out", "timeout")


def claude_code_env(base: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if base is None else base)
    hosted = "CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST" in env
    for name in list(env):
        if name in _KEEP_CLAUDE_VARS:
            continue
        if name == "CLAUDECODE" or name.startswith("CLAUDE_"):
            del env[name]
    if hosted:
        env.pop("ANTHROPIC_BASE_URL", None)  # the host's proxy only serves the host session
    return env


@dataclass
class ClaudeCodeClient(ClaudeClient):
    """Runs each request through the Claude Code CLI (`claude -p`), so a Claude subscription
    (Pro/Max) can power Loom without an API key.

    Same cache, mock and record behavior as the API client, and the same cache keys, so
    responses recorded through either backend are interchangeable. Authentication is Claude
    Code's own: `claude` then `/login`, or a `CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token`.
    """

    executable: str = "claude"
    timeout: int = 900
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run

    def command(self, request: LLMRequest, system_file: Path) -> list[str]:
        return [
            self.executable,
            "-p",
            "--output-format",
            "json",
            "--model",
            self.model,
            "--system-prompt-file",
            str(system_file),
            "--json-schema",
            json.dumps(request.schema, separators=(",", ":")),
            "--tools",
            "",
            "--effort",
            request.effort,
            "--no-session-persistence",
            "--strict-mcp-config",
        ]

    def _call_api(self, key: str, request: LLMRequest) -> dict[str, Any]:
        attempt = 0
        while True:
            attempt += 1
            self.stats["api_calls"] += 1
            with tempfile.TemporaryDirectory() as tmp:
                system_file = Path(tmp) / "system.md"
                system_file.write_text(request.system + "\n\n" + request.context, encoding="utf-8")
                try:
                    proc = self.run(
                        self.command(request, system_file),
                        input=request.prompt,
                        capture_output=True,
                        text=True,
                        timeout=self.timeout,
                        env=claude_code_env(),
                    )
                except FileNotFoundError as exc:
                    raise GenerationError(
                        f"the Claude Code CLI ({self.executable!r}) is not installed: "
                        "see https://code.claude.com, or use an API key instead"
                    ) from exc
                except subprocess.TimeoutExpired as exc:
                    proc = subprocess.CompletedProcess(exc.cmd, 124, "", "timed out")
            data = _claude_json(proc.stdout)
            error = _claude_error(proc, data)
            if error is None:
                break
            if attempt < self.max_attempts and any(t in error.lower() for t in _TRANSIENT):
                self.stats["retries"] += 1
                self.sleep(backoff_delay(attempt))
                continue
            hint = ""
            if "not logged in" in error.lower() or "/login" in error:
                hint = (
                    ". Log in once with `claude` (then /login) using your Claude subscription, or "
                    "run `claude setup-token` and export CLAUDE_CODE_OAUTH_TOKEN"
                )
            raise GenerationError(
                f"Claude Code failed on {request.kind} for {request.label}: {error}{hint}"
            )
        assert data is not None
        output = data.get("structured_output")
        if output is None:
            try:
                output = json.loads(str(data.get("result", "")))
            except json.JSONDecodeError as exc:
                raise GenerationError(
                    f"Claude Code returned no structured output for {request.kind}: {exc}"
                ) from exc
        usage = data.get("usage") or {}
        models = list((data.get("modelUsage") or {}).keys())
        return {
            "key": key,
            "kind": request.kind,
            "label": request.label,
            "source": "claude-code",
            "model": self.model,
            "served_model": models[0] if models else None,
            "loom_version": __version__,
            "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "usage": {
                k: usage.get(k)
                for k in (
                    "input_tokens",
                    "output_tokens",
                    "cache_read_input_tokens",
                    "cache_creation_input_tokens",
                )
            },
            "request": request.body(self.model),
            "output": output,
        }


def _claude_json(stdout: str) -> dict[str, Any] | None:
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _claude_error(
    proc: subprocess.CompletedProcess[str], data: dict[str, Any] | None
) -> str | None:
    if data is None:
        if proc.returncode == 0:
            return "unreadable output from Claude Code"
        return (proc.stderr or proc.stdout or f"exit code {proc.returncode}").strip()[:500]
    if data.get("is_error") or proc.returncode != 0:
        return str(data.get("result") or data.get("terminal_reason") or "unknown error")[:500]
    return None


BACKENDS = ("auto", "api", "claude-code")


def make_client(model: str | None = None, backend: str | None = None) -> ClaudeClient:
    """Pick the model backend: the Anthropic API, or the Claude Code CLI (subscription).

    ``auto`` (the default, or ``$LOOM_BACKEND``) uses the API when ANTHROPIC_API_KEY is set,
    otherwise Claude Code when the ``claude`` CLI is installed, otherwise the API (which then
    explains how to authenticate).
    """
    choice = (backend or os.environ.get("LOOM_BACKEND") or "auto").lower()
    if choice not in BACKENDS:
        raise LoomError(f"unknown backend {choice!r}: use one of {', '.join(BACKENDS)}")
    if choice == "auto":
        if os.environ.get("ANTHROPIC_API_KEY"):
            choice = "api"
        elif shutil.which("claude"):
            choice = "claude-code"
        else:
            choice = "api"
    cls = ClaudeCodeClient if choice == "claude-code" else ClaudeClient
    return cls.from_env(model)
