"""Turn a pending mock request into a recorded fixture, with a hand-written answer.

Used only when no ANTHROPIC_API_KEY is available. The result is marked
``"source": "hand-authored"`` so it is never mistaken for a real model response; re-record with
``LOOM_RECORD=1`` to replace it.

    python scripts/answer_fixture.py <key-prefix> answer.json
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "llm"


def main(prefix: str, answer_path: str) -> None:
    matches = sorted((FIXTURES / "_pending").glob(f"{prefix}*.request.json"))
    if len(matches) != 1:
        sys.exit(f"expected exactly one pending request matching {prefix!r}, found {len(matches)}")
    pending = json.loads(matches[0].read_text())
    output = json.loads(Path(answer_path).read_text())
    entry = {
        "key": pending["key"],
        "kind": pending["kind"],
        "label": pending["label"],
        "source": "hand-authored",
        "model": pending["request"]["model"],
        "served_model": None,
        "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "usage": None,
        "request": pending["request"],
        "output": output,
    }
    out = FIXTURES / f"{pending['key']}.json"
    out.write_text(json.dumps(entry, indent=2, ensure_ascii=False) + "\n")
    matches[0].unlink()
    where = out.relative_to(FIXTURES.parent.parent.parent)
    print(f"wrote {where} ({pending['kind']} {pending['label']})")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
