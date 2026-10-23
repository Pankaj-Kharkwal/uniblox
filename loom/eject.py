"""``loom eject``: leave a plain Python project with no trace of Loom.

Strips the generated-file headers, restores write permission on the locked tests, removes
caches, and deletes the lockfile. The project keeps working on its own
(``pip install -e . && pytest``).
"""

from __future__ import annotations

import re
import shutil
import stat
from dataclasses import dataclass, field
from pathlib import Path

from loom.errors import LoomError
from loom.scaffold import HEADER_PREFIX

CACHE_DIRS = ("__pycache__", ".pytest_cache", ".hypothesis", ".mypy_cache", ".ruff_cache")
_LOOM = re.compile(r"loom", re.IGNORECASE)


@dataclass
class EjectReport:
    project: Path
    stripped: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    lock_deleted: bool = False
    leftovers: list[str] = field(default_factory=list)  # files still mentioning Loom


def loom_references(root: Path) -> list[str]:
    """Files under ``root`` whose name or text mentions Loom."""
    hits = []
    for path in sorted(root.rglob("*")):
        rel = str(path.relative_to(root))
        if _LOOM.search(rel):
            hits.append(rel)
        elif path.is_file():
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            if _LOOM.search(text):
                hits.append(rel)
    return hits


def eject(project: Path, lock_file: Path | None) -> EjectReport:
    if not (project / "pyproject.toml").exists():
        raise LoomError(f"no generated project at {project}; nothing to eject")
    report = EjectReport(project=project)
    for path in sorted(project.rglob("*")):
        if path.is_dir() and path.name in CACHE_DIRS:
            shutil.rmtree(path)
            report.removed.append(str(path.relative_to(project)))
    for path in sorted(project.rglob("*")):
        if not path.is_file():
            continue
        path.chmod(path.stat().st_mode | stat.S_IWUSR)
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if text.startswith(HEADER_PREFIX):
            path.write_text(text.split("\n", 1)[1] if "\n" in text else "", encoding="utf-8")
            report.stripped.append(str(path.relative_to(project)))
    if lock_file is not None and lock_file.exists():
        lock_file.unlink()
        report.lock_deleted = True
    report.leftovers = loom_references(project)
    return report
