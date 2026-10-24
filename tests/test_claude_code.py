"""The Claude Code backend: `claude -p` as the model, so a subscription can power Loom."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from loom.errors import GenerationError, LoomError
from loom.llm import ClaudeClient, ClaudeCodeClient, LLMRequest, claude_code_env, make_client

SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
    "additionalProperties": False,
}
REQ = LLMRequest(
    kind="codegen", label="does:x", system="SYS", context="CTX", prompt="PROMPT", schema=SCHEMA
)


def result(**data: Any) -> str:
    base = {
        "type": "result",
        "is_error": False,
        "result": "",
        "usage": {"input_tokens": 10, "output_tokens": 5},
        "modelUsage": {"claude-sonnet-5-5": {}},
    }
    return json.dumps({**base, **data})


class FakeCLI:
    def __init__(self, *responses: tuple[int, str]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        system_file = Path(cmd[cmd.index("--system-prompt-file") + 1])
        self.calls.append({"cmd": cmd, "system": system_file.read_text(), **kw})
        code, out = self.responses.pop(0)
        return subprocess.CompletedProcess(cmd, code, out, "")


def client(tmp_path: Path, cli: FakeCLI, **kw: Any) -> ClaudeCodeClient:
    return ClaudeCodeClient(
        model="claude-sonnet-5-5",
        cache_dir=tmp_path / "cache",
        fixtures_dir=tmp_path / "fixtures",
        run=cli,
        sleep=lambda s: None,
        **kw,
    )


def test_runs_claude_print_mode_with_schema_no_tools_and_stdin_prompt(tmp_path: Path) -> None:
    cli = FakeCLI((0, result(structured_output={"answer": "42"})))
    c = client(tmp_path, cli)
    assert c.complete(REQ) == {"answer": "42"}
    call = cli.calls[0]
    cmd = call["cmd"]
    assert cmd[:2] == ["claude", "-p"]
    assert cmd[cmd.index("--model") + 1] == "claude-sonnet-5-5"
    assert json.loads(cmd[cmd.index("--json-schema") + 1]) == SCHEMA
    assert cmd[cmd.index("--tools") + 1] == ""  # a plain model call: no tools
    assert cmd[cmd.index("--effort") + 1] == "medium"
    assert "--no-session-persistence" in cmd
    assert call["input"] == "PROMPT" and call["system"] == "SYS\n\nCTX"
    # Same cache keys as the API backend: a second identical request is a cache hit.
    assert c.complete(REQ) == {"answer": "42"}
    assert c.stats["api_calls"] == 1 and c.stats["cache_hits"] == 1
    assert REQ.key("claude-sonnet-5-5") == REQ.key(c.model)


def test_falls_back_to_json_in_result_and_records_fixtures(tmp_path: Path) -> None:
    cli = FakeCLI((0, result(result='{"answer": "from text"}')))
    c = client(tmp_path, cli, record=True)
    assert c.complete(REQ) == {"answer": "from text"}
    (fixture,) = (tmp_path / "fixtures").glob("*.json")
    entry = json.loads(fixture.read_text())
    assert entry["source"] == "claude-code" and entry["served_model"] == "claude-sonnet-5-5"
    assert entry["usage"]["output_tokens"] == 5


def test_not_logged_in_explains_how_to_authenticate(tmp_path: Path) -> None:
    cli = FakeCLI((1, result(is_error=True, result="Not logged in · Please run /login")))
    with pytest.raises(GenerationError, match="claude setup-token"):
        client(tmp_path, cli).complete(REQ)


def test_transient_errors_are_retried(tmp_path: Path) -> None:
    cli = FakeCLI(
        (1, result(is_error=True, result="API Error: 529 overloaded")),
        (0, result(structured_output={"answer": "ok"})),
    )
    c = client(tmp_path, cli)
    assert c.complete(REQ) == {"answer": "ok"}
    assert c.stats["retries"] == 1


@pytest.mark.parametrize(
    ("response", "message"),
    [
        ((2, "not json at all"), "not json at all"),
        ((0, "not json"), "unreadable output"),
        ((0, result(result="no json here")), "no structured output"),
    ],
)
def test_bad_output(tmp_path: Path, response: tuple[int, str], message: str) -> None:
    with pytest.raises(GenerationError, match=message):
        client(tmp_path, FakeCLI(response)).complete(REQ)


def test_missing_cli(tmp_path: Path) -> None:
    def missing(*a: Any, **k: Any) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("claude")

    c = ClaudeCodeClient(cache_dir=None, fixtures_dir=None, run=missing)
    with pytest.raises(GenerationError, match="not installed"):
        c.complete(REQ)


def test_child_environment_drops_the_surrounding_session() -> None:
    env = claude_code_env(
        {
            "PATH": "/bin",
            "CLAUDECODE": "1",
            "CLAUDE_CODE_SESSION_ID": "s",
            "CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST": "1",
            "ANTHROPIC_BASE_URL": "http://host-proxy",
            "CLAUDE_CODE_OAUTH_TOKEN": "tok",
            "ANTHROPIC_API_KEY": "key",
        }
    )
    assert env == {"PATH": "/bin", "CLAUDE_CODE_OAUTH_TOKEN": "tok", "ANTHROPIC_API_KEY": "key"}
    assert "ANTHROPIC_BASE_URL" in claude_code_env({"ANTHROPIC_BASE_URL": "https://gw"})


def test_backend_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LOOM_BACKEND", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    assert type(make_client()) is ClaudeClient
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    monkeypatch.setattr("loom.llm.shutil.which", lambda name: "/usr/bin/claude")
    assert type(make_client()) is ClaudeCodeClient
    monkeypatch.setattr("loom.llm.shutil.which", lambda name: None)
    assert type(make_client()) is ClaudeClient
    monkeypatch.setenv("LOOM_BACKEND", "claude-code")
    assert type(make_client(model="claude-opus-5-5")) is ClaudeCodeClient
    assert make_client(model="claude-opus-5-5").model == "claude-opus-5-5"
    assert type(make_client(backend="api")) is ClaudeClient
    with pytest.raises(LoomError, match="unknown backend"):
        make_client(backend="gpt")
