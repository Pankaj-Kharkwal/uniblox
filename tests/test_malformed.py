"""Malformed inputs must fail with a positioned, human-readable message."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from loom.errors import LoomSyntaxError
from loom.frontend import load

MALFORMED = Path(__file__).resolve().parent / "fixtures" / "malformed"
EXPECTED: dict[str, dict[str, object]] = json.loads((MALFORMED / "expected.json").read_text())


def test_every_malformed_file_has_an_expectation() -> None:
    assert sorted(p.stem for p in MALFORMED.glob("*.loom")) == sorted(EXPECTED)
    assert len(EXPECTED) >= 6


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_malformed_input_reports_expected_error(name: str) -> None:
    with pytest.raises(LoomSyntaxError) as info:
        load(MALFORMED / f"{name}.loom")
    err = info.value
    want = EXPECTED[name]
    assert (err.line, err.column) == (want["line"], want["column"])
    assert err.message == want["message"]
    assert err.hint == want["hint"]


def test_rendered_error_shows_location_source_line_caret_and_hint() -> None:
    with pytest.raises(LoomSyntaxError) as info:
        load(MALFORMED / "unknown_type.loom")
    rendered = info.value.render().splitlines()
    assert rendered[0].endswith("unknown_type.loom:4:11: error: unknown type 'monee'")
    assert rendered[1] == "      amount: monee (INR)"
    assert rendered[2] == "              ^"
    assert rendered[3] == "  hint: did you mean 'money'?"
