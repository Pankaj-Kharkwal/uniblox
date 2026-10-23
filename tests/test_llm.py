from __future__ import annotations

import json
from pathlib import Path

import anthropic
import httpx
import pytest
from fakes import FakeSDK, message

from loom.errors import GenerationError
from loom.llm import FALLBACK_BETA, ClaudeClient, LLMRequest, MockMissError, backoff_delay

SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
    "additionalProperties": False,
}


def req(prompt: str = "hello") -> LLMRequest:
    return LLMRequest(
        kind="ambiguity",
        label="does:x",
        system="sys",
        context="ctx",
        prompt=prompt,
        schema=SCHEMA,
        effort="low",
    )


def client(tmp_path: Path, sdk: FakeSDK, **kw: object) -> ClaudeClient:
    sleeps: list[float] = []
    c = ClaudeClient(
        model="claude-sonnet-5-5",
        cache_dir=tmp_path / "cache",
        fixtures_dir=tmp_path / "fixtures",
        client_factory=lambda: sdk,
        sleep=sleeps.append,
        **kw,  # type: ignore[arg-type]
    )
    c.sleeps = sleeps  # type: ignore[attr-defined]
    return c


def status_error(cls: type[anthropic.APIStatusError], code: int) -> anthropic.APIStatusError:
    response = httpx.Response(code, request=httpx.Request("POST", "https://api.anthropic.com"))
    return cls("boom", response=response, body=None)


def test_request_body_uses_structured_output_caching_and_no_temperature() -> None:
    body = req().body("claude-sonnet-5-5")
    assert body["output_config"] == {
        "effort": "low",
        "format": {"type": "json_schema", "schema": SCHEMA},
    }
    assert body["system"][-1]["cache_control"] == {"type": "ephemeral"}
    assert "temperature" not in body and "tool_choice" not in body


def test_key_depends_on_model_and_content_only() -> None:
    assert req().key("m1") == req().key("m1")
    assert req().key("m1") != req().key("m2")
    assert req("a").key("m1") != req("b").key("m1")


def test_live_call_parses_json_and_second_call_hits_disk_cache(tmp_path: Path) -> None:
    sdk = FakeSDK([message({"answer": "42"})])
    c = client(tmp_path, sdk)
    assert c.complete(req()) == {"answer": "42"}
    assert c.complete(req()) == {"answer": "42"}
    assert c.stats["api_calls"] == 1 and c.stats["cache_hits"] == 1
    sent = sdk.requests[0]
    assert sent["betas"] == [FALLBACK_BETA] and sent["fallbacks"] == "default"
    assert sent["model"] == "claude-sonnet-5-5"


def test_fallbacks_can_be_disabled(tmp_path: Path) -> None:
    sdk = FakeSDK([message({"answer": "x"})])
    client(tmp_path, sdk, use_fallbacks=False).complete(req())
    assert "fallbacks" not in sdk.requests[0] and "betas" not in sdk.requests[0]


def test_retries_with_backoff_then_succeeds(tmp_path: Path) -> None:
    sdk = FakeSDK(
        [
            status_error(anthropic.RateLimitError, 429),
            status_error(anthropic.InternalServerError, 500),
            message({"answer": "ok"}),
        ]
    )
    c = client(tmp_path, sdk)
    assert c.complete(req()) == {"answer": "ok"}
    assert c.stats["retries"] == 2 and c.stats["api_calls"] == 3
    assert len(c.sleeps) == 2  # type: ignore[attr-defined]


def test_gives_up_after_max_attempts(tmp_path: Path) -> None:
    sdk = FakeSDK([status_error(anthropic.RateLimitError, 429)] * 3)
    with pytest.raises(GenerationError, match="failed after 3 attempts"):
        client(tmp_path, sdk, max_attempts=3).complete(req())


def test_bad_request_is_not_retried(tmp_path: Path) -> None:
    sdk = FakeSDK([status_error(anthropic.BadRequestError, 400)])
    with pytest.raises(GenerationError, match="LOOM_NO_FALLBACK"):
        client(tmp_path, sdk).complete(req())


@pytest.mark.parametrize(
    ("msg", "error"),
    [
        (message(stop_reason="refusal", category="cyber"), "declined.*cyber"),
        (message({"answer": "x"}, stop_reason="max_tokens"), "cut off"),
        (message(), "no text output"),
        (message(text="not json"), "not valid JSON"),
    ],
)
def test_bad_responses_raise_generation_errors(tmp_path: Path, msg: object, error: str) -> None:
    with pytest.raises(GenerationError, match=error):
        client(tmp_path, FakeSDK([msg])).complete(req())


def test_record_mode_writes_fixture_and_mock_mode_replays_it(tmp_path: Path) -> None:
    rec = client(tmp_path, FakeSDK([message({"answer": "recorded"})]), record=True)
    rec.complete(req())
    key = req().key("claude-sonnet-5-5")
    fixture = json.loads((tmp_path / "fixtures" / f"{key}.json").read_text())
    assert fixture["source"] == "api" and fixture["output"] == {"answer": "recorded"}
    assert fixture["usage"]["cache_creation_input_tokens"] == 80

    sdk = FakeSDK([])  # any network use would fail: the script is empty
    mock = client(tmp_path, sdk, mock=True)
    assert mock.complete(req()) == {"answer": "recorded"}
    assert mock.stats["fixture_hits"] == 1 and mock.stats["api_calls"] == 0


def test_mock_miss_saves_the_request_for_recording(tmp_path: Path) -> None:
    mock = client(tmp_path, FakeSDK([]), mock=True)
    with pytest.raises(MockMissError, match="no recorded ambiguity response for does:x"):
        mock.complete(req("unseen"))
    (pending,) = (tmp_path / "fixtures" / "_pending").glob("*.request.json")
    saved = json.loads(pending.read_text())
    assert saved["request"]["messages"][0]["content"] == "unseen"


def test_from_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LOOM_MODEL", "claude-opus-5-5")
    monkeypatch.setenv("LOOM_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("LOOM_MOCK", "1")
    monkeypatch.setenv("LOOM_NO_FALLBACK", "1")
    c = ClaudeClient.from_env()
    assert c.model == "claude-opus-5-5" and c.cache_dir == tmp_path
    assert c.mock and not c.use_fallbacks
    assert ClaudeClient.from_env("claude-haiku-5-5").model == "claude-haiku-5-5"


def test_backoff_grows_and_is_capped() -> None:
    for attempt in range(1, 10):
        d = backoff_delay(attempt)
        ceiling = min(30.0, 2.0 ** (attempt - 1))
        assert ceiling / 2 <= d <= ceiling
