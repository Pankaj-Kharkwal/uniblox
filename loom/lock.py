"""The lockfile: ``<file>.lock``, JSON.

It records, per block, the normalized AST and its hashes, the files generated for it with
their content hashes, the function signature, status and timestamps. On rebuild, only blocks
whose inputs changed (or that are red, or whose files are missing) are regenerated. A generated
file whose content no longer matches the lock was edited by hand: that is reported, not
silently overwritten.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from loom import __version__
from loom.errors import LockError
from loom.hashing import sha256

LOCK_VERSION = 1


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def lock_path(source: Path) -> Path:
    return source.with_name(source.name + ".lock")


def content_hash(text: str) -> str:
    return sha256(text)


@dataclass
class BlockLock:
    kind: str  # "thing" | "does"
    source_hash: str
    input_hash: str
    ast: Any  # normalized AST data (no positions)
    files: dict[str, str] = field(default_factory=dict)  # relpath -> content hash
    tests: dict[str, str] = field(default_factory=dict)  # relpath -> content hash (locked)
    signature: str | None = None
    schedule: dict[str, str] | None = None
    status: str = "green"  # green | red
    iterations: int = 0  # repair rounds this block needed
    generated_at: str = field(default_factory=now_iso)

    def all_files(self) -> dict[str, str]:
        return {**self.files, **self.tests}


@dataclass
class Lock:
    model: str
    source: str  # the .loom file name
    out_dir: str  # relative to the lock file's directory (or absolute)
    app_hash: str = ""
    shared_hash: str = ""  # app header + things + top-level exact
    shared_files: dict[str, str] = field(default_factory=dict)  # scaffold + glue files
    blocks: dict[str, BlockLock] = field(default_factory=dict)
    loom_version: str = __version__
    lock_version: int = LOCK_VERSION
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)

    def does_blocks(self) -> dict[str, BlockLock]:
        return {k: v for k, v in self.blocks.items() if v.kind == "does"}

    def signatures(self) -> dict[str, str]:
        return {
            k.split(":", 1)[1]: b.signature
            for k, b in self.does_blocks().items()
            if b.signature is not None
        }

    def all_files(self) -> dict[str, str]:
        out = dict(self.shared_files)
        for b in self.blocks.values():
            out.update(b.all_files())
        return out

    # --- persistence ---------------------------------------------------------------------

    def to_json(self) -> str:
        data = asdict(self)
        ordered = {
            k: data[k]
            for k in (
                "lock_version",
                "loom_version",
                "model",
                "source",
                "out_dir",
                "created_at",
                "updated_at",
                "app_hash",
                "shared_hash",
                "shared_files",
                "blocks",
            )
        }
        return json.dumps(ordered, indent=2, ensure_ascii=False, sort_keys=False) + "\n"

    @classmethod
    def from_json(cls, text: str) -> Lock:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise LockError(f"the lockfile is not valid JSON: {exc}") from exc
        if not isinstance(data, dict) or data.get("lock_version") != LOCK_VERSION:
            raise LockError(
                f"unsupported lockfile version {data.get('lock_version')!r}"
                if isinstance(data, dict)
                else "the lockfile is not a JSON object"
            )
        try:
            blocks = {k: BlockLock(**v) for k, v in data.pop("blocks", {}).items()}
            return cls(blocks=blocks, **data)
        except TypeError as exc:
            raise LockError(f"the lockfile is malformed: {exc}") from exc

    def save(self, path: Path) -> None:
        self.updated_at = now_iso()
        path.write_text(self.to_json(), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> Lock | None:
        if not path.exists():
            return None
        return cls.from_json(path.read_text(encoding="utf-8"))

    def resolve_out_dir(self, lock_file: Path) -> Path:
        p = Path(self.out_dir)
        return p if p.is_absolute() else (lock_file.parent / p).resolve()


def relative_out_dir(out: Path, lock_file: Path) -> str:
    """Store the output directory relative to the lock when possible (portable checkouts)."""
    try:
        return str(out.resolve().relative_to(lock_file.parent.resolve()))
    except ValueError:
        import os

        try:
            return os.path.relpath(out.resolve(), lock_file.parent.resolve())
        except ValueError:  # pragma: no cover - different drives on Windows
            return str(out.resolve())


def drifted_files(root: Path, expected: dict[str, str]) -> tuple[list[str], list[str]]:
    """Files whose content changed since the lock, and files that are missing."""
    changed, missing = [], []
    for rel, digest in sorted(expected.items()):
        path = root / rel
        if not path.exists():
            missing.append(rel)
        elif content_hash(path.read_text(encoding="utf-8")) != digest:
            changed.append(rel)
    return changed, missing
