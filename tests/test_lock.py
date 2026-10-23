from __future__ import annotations

from pathlib import Path

import pytest

from loom.errors import LockError
from loom.lock import BlockLock, Lock, content_hash, drifted_files, lock_path, relative_out_dir


def sample() -> Lock:
    lock = Lock(model="claude-sonnet-5-5", source="todo.loom", out_dir="out/todo_list")
    lock.app_hash = "sha256:a"
    lock.shared_hash = "sha256:s"
    lock.shared_files = {"pyproject.toml": "sha256:p"}
    lock.blocks["thing:Task"] = BlockLock(
        kind="thing", source_hash="sha256:t", input_hash="sha256:t", ast={"node": "Thing"}
    )
    lock.blocks["does:complete_task"] = BlockLock(
        kind="does",
        source_hash="sha256:d",
        input_hash="sha256:i",
        ast={"node": "Does", "description": "complete task"},
        files={"src/todo_list/complete_task.py": "sha256:m"},
        tests={"tests/test_complete_task_examples.py": "sha256:e"},
        signature="def complete_task(tasks: list[Task], title: str) -> Task",
        schedule={"cron": "0 10 * * mon-fri", "timezone": "Asia/Kolkata"},
        status="red",
        iterations=3,
    )
    return lock


def test_round_trip(tmp_path: Path) -> None:
    lock = sample()
    path = tmp_path / "todo.loom.lock"
    lock.save(path)
    again = Lock.load(path)
    assert again == lock
    assert again is not None and again.to_json() == lock.to_json()
    assert again.signatures() == {
        "complete_task": "def complete_task(tasks: list[Task], title: str) -> Task"
    }
    assert set(again.does_blocks()) == {"does:complete_task"}
    assert again.all_files() == {
        "pyproject.toml": "sha256:p",
        "src/todo_list/complete_task.py": "sha256:m",
        "tests/test_complete_task_examples.py": "sha256:e",
    }
    assert list(__import__("json").loads(path.read_text()))[:3] == [
        "lock_version",
        "loom_version",
        "model",
    ]


def test_missing_lock_is_none(tmp_path: Path) -> None:
    assert Lock.load(tmp_path / "nope.lock") is None


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("{not json", "not valid JSON"),
        ("[]", "not a JSON object"),
        ('{"lock_version": 99}', "unsupported lockfile version 99"),
        ('{"lock_version": 1, "model": "m"}', "malformed"),
    ],
)
def test_bad_locks(text: str, message: str) -> None:
    with pytest.raises(LockError, match=message):
        Lock.from_json(text)


def test_paths(tmp_path: Path) -> None:
    src = tmp_path / "examples" / "todo.loom"
    assert lock_path(src) == tmp_path / "examples" / "todo.loom.lock"
    lf = lock_path(src)
    assert relative_out_dir(tmp_path / "examples" / "out" / "x", lf) == "out/x"
    assert relative_out_dir(tmp_path / "out" / "x", lf) == "../out/x"
    lock = sample()
    lock.out_dir = "../out/x"
    assert lock.resolve_out_dir(lf) == (tmp_path / "out" / "x").resolve()
    lock.out_dir = str(tmp_path / "abs")
    assert lock.resolve_out_dir(lf) == tmp_path / "abs"


def test_drifted_files(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("same")
    (tmp_path / "b.py").write_text("edited")
    changed, missing = drifted_files(
        tmp_path,
        {"a.py": content_hash("same"), "b.py": content_hash("original"), "c.py": "sha256:x"},
    )
    assert changed == ["b.py"] and missing == ["c.py"]
