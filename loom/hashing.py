"""Normalized AST hashing.

A block's ``source_hash`` covers its meaning, not its formatting. It is computed from the AST
with positions dropped, prose whitespace collapsed, and ordinary comments already discarded by
the parser. ``# decided:`` lines are part of the AST, so answering an ambiguity *does* change
the hash.

A block's ``input_hash`` adds everything its generated code depends on: the app header, every
thing, top-level ``exact`` code, and the signatures of the other does blocks.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from loom.nodes import Does, Exact, Program, Thing, to_data


def canonical_json(data: Any) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256(text: str | bytes) -> str:
    raw = text.encode("utf-8") if isinstance(text, str) else text
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def normalized(node: Any) -> Any:
    """AST data with positions dropped and prose whitespace collapsed (exact code kept)."""
    return _collapse(to_data(node, positions=False))


def _collapse(data: Any) -> Any:
    if isinstance(data, dict):
        keep_raw = data.get("node") == "Exact"
        return {k: (v if keep_raw and k == "code" else _collapse(v)) for k, v in data.items()}
    if isinstance(data, list):
        return [_collapse(x) for x in data]
    if isinstance(data, str):
        return " ".join(data.split())
    return data


def node_hash(node: Any) -> str:
    return sha256(canonical_json(normalized(node)))


def block_key(block: Thing | Does) -> str:
    return f"thing:{block.name}" if isinstance(block, Thing) else f"does:{block.slug}"


def source_hashes(program: Program) -> dict[str, str]:
    """``{"app": ..., "thing:Task": ..., "does:complete_task": ..., "exact": ...}``."""
    out = {"app": node_hash(program.app)}
    for t in program.things:
        out[block_key(t)] = node_hash(t)
    for d in program.does:
        out[block_key(d)] = node_hash(d)
    out["exact"] = node_hash(program.exacts)
    return out


def shared_context_hash(program: Program) -> str:
    """Hash of what every does block depends on: app header, things, top-level exact code."""
    parts = {
        "app": node_hash(program.app),
        "things": [node_hash(t) for t in program.things],
        "exact": [node_hash(e) for e in program.exacts],
    }
    return sha256(canonical_json(parts))


def does_input_hash(
    program: Program, does: Does, other_signatures: Mapping[str, str] | None = None
) -> str:
    """Everything that, if changed, should regenerate this does block."""
    parts = {
        "self": node_hash(does),
        "shared": shared_context_hash(program),
        "signatures": dict(sorted((other_signatures or {}).items())),
    }
    return sha256(canonical_json(parts))


def exact_hash(exact: Exact) -> str:
    return sha256(exact.code)
