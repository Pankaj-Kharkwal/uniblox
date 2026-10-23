"""Compiler errors with source positions and human-readable rendering."""

from __future__ import annotations


class LoomError(Exception):
    """Base class for every error Loom reports to the user."""

    def render(self) -> str:
        return str(self)


class LoomSyntaxError(LoomError):
    """A parse or semantic error anchored at a line and column of a .loom file."""

    def __init__(
        self,
        message: str,
        line: int,
        column: int,
        *,
        hint: str | None = None,
        source: str | None = None,
        path: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.line = line
        self.column = column
        self.hint = hint
        self.source = source
        self.path = path

    def __str__(self) -> str:
        return f"{self.path or '<input>'}:{self.line}:{self.column}: error: {self.message}"

    def render(self) -> str:
        """Render like a compiler diagnostic: location, message, source line, caret, hint."""
        out = [str(self)]
        if self.source is not None:
            lines = self.source.splitlines()
            if 1 <= self.line <= len(lines):
                text = lines[self.line - 1]
                out.append(f"    {text}")
                out.append("    " + " " * max(self.column - 1, 0) + "^")
        if self.hint:
            out.append(f"  hint: {self.hint}")
        return "\n".join(out)


class UnresolvedAmbiguityError(LoomError):
    """Raised when a does block still has open ambiguities and no answer was given."""


class LockError(LoomError):
    """Raised for unreadable lockfiles and for hand-edited (drifted) generated files."""


class GenerationError(LoomError):
    """Raised when the model's output fails structural validation or the API refuses."""


class TestTamperError(LoomError):
    """Raised when a locked test file changes during the green loop."""

    __test__ = False  # keep pytest from collecting this as a test class
