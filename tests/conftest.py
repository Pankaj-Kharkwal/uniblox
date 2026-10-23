from __future__ import annotations

import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"
FIXTURES = Path(__file__).resolve().parent / "fixtures"

# The compiler test suite never talks to the network unless explicitly asked to.
if os.environ.get("LOOM_LIVE") != "1":
    os.environ["LOOM_MOCK"] = "1"


@pytest.fixture
def examples_dir() -> Path:
    return EXAMPLES


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES
