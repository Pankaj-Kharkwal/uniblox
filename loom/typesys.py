"""Forgiving Loom types -> concrete Python types.

| Loom              | Python                          |
|-------------------|---------------------------------|
| text              | str                             |
| email             | EmailStr                        |
| number            | int                             |
| money (CUR)       | Decimal, tagged with currency   |
| date / datetime   | datetime.date / datetime.datetime |
| one of a, b       | a generated StrEnum             |
| T?                | Optional[T] (default None)      |
| = value           | a typed default                 |
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from loom.errors import LoomSyntaxError
from loom.nodes import Field, Program, Thing, TypeRef

BASE_TYPES = ("text", "email", "number", "money", "date", "datetime")

# Names people reach for that Loom spells differently. Used only for "did you mean" hints:
# unknown types are always an error.
_SYNONYMS = {
    "string": "text",
    "str": "text",
    "varchar": "text",
    "int": "number",
    "integer": "number",
    "count": "number",
    "decimal": "money",
    "currency": "money",
    "price": "money",
    "timestamp": "datetime",
    "time": "datetime",
    "enum": "one of a, b",
    "choice": "one of a, b",
    "mail": "email",
}

_PY_BASE = {
    "text": "str",
    "email": "EmailStr",
    "number": "int",
    "money": "Decimal",
    "date": "date",
    "datetime": "datetime",
}

_IMPORTS = {
    "email": "from pydantic import EmailStr",
    "money": "from decimal import Decimal",
    "date": "from datetime import date",
    "datetime": "from datetime import datetime",
}


@dataclass(frozen=True)
class ResolvedType:
    loom: str  # canonical Loom spelling, e.g. "money (INR)"
    kind: str  # text | email | number | money | date | datetime | enum
    py: str  # annotation source, e.g. "Optional[datetime]"
    optional: bool = False
    currency: str | None = None
    options: tuple[str, ...] = ()
    enum_name: str | None = None
    imports: tuple[str, ...] = ()


@dataclass(frozen=True)
class ResolvedField:
    name: str
    type: ResolvedType
    default_py: str | None  # Python source of the default, None if required

    @property
    def required(self) -> bool:
        return self.default_py is None


@dataclass(frozen=True)
class ResolvedThing:
    name: str
    fields: tuple[ResolvedField, ...]


def _err(message: str, ref: TypeRef | Field, hint: str | None = None) -> LoomSyntaxError:
    return LoomSyntaxError(message, ref.pos.line, ref.pos.column, hint=hint)


def resolve_type(ref: TypeRef, *, thing: str = "", field: str = "") -> ResolvedType:
    """Resolve one forgiving type. ``thing``/``field`` name the generated enum class."""
    if ref.base == "one of":
        options = ref.options
        if len(set(options)) != len(options):
            dup = next(o for o in options if options.count(o) > 1)
            raise _err(f"option {dup!r} is listed twice", ref)
        enum_name = f"{thing}{_camel(field)}" if thing else _camel(field) or "Choice"
        py = f"Optional[{enum_name}]" if ref.optional else enum_name
        return ResolvedType(
            loom="one of " + ", ".join(options),
            kind="enum",
            py=py,
            optional=ref.optional,
            options=options,
            enum_name=enum_name,
            imports=("from enum import StrEnum",),
        )

    base = ref.base.lower()
    if base not in BASE_TYPES:
        suggestion = _SYNONYMS.get(base) or next(
            iter(difflib.get_close_matches(base, BASE_TYPES, n=1, cutoff=0.6)), None
        )
        hint = (
            f"did you mean {suggestion!r}?"
            if suggestion
            else "known types: text, email, number, money (CUR), date, datetime, one of a, b"
        )
        raise _err(f"unknown type {ref.base!r}", ref, hint)

    currency: str | None = None
    if base == "money":
        if not ref.arg:
            raise _err("money needs a currency", ref, "e.g. `money (INR)` or `money (USD)`")
        currency = ref.arg.upper()
        if not re.fullmatch(r"[A-Z]{3}", currency):
            raise _err(
                f"{ref.arg!r} is not a currency code", ref, "use a 3-letter ISO code like INR"
            )
    elif ref.arg:
        raise _err(f"type {base!r} takes no argument", ref, f"write just `{base}`")

    py = _PY_BASE[base]
    imports = tuple(i for i in (_IMPORTS.get(base),) if i)
    if ref.optional:
        py = f"Optional[{py}]"
        imports += ("from typing import Optional",)
    loom = f"money ({currency})" if currency else base
    return ResolvedType(
        loom=loom + ("?" if ref.optional else ""),
        kind=base,
        py=py,
        optional=ref.optional,
        currency=currency,
        imports=imports,
    )


def resolve_default(rt: ResolvedType, field: Field) -> str | None:
    """Validate a ``= value`` default and return its Python source."""
    raw = field.default
    if raw is None:
        return "None" if rt.optional else None
    value = raw.strip()
    if value.lower() in ("none", "nothing", "null") and rt.optional:
        return "None"
    if rt.kind == "number":
        if re.fullmatch(r"-?\d+", value):
            return str(int(value))
        raise _err(f"default {value!r} is not a whole number", field)
    if rt.kind == "money":
        try:
            return f'Decimal("{Decimal(value.replace(",", ""))}")'
        except InvalidOperation:
            raise _err(f"default {value!r} is not an amount of money", field) from None
    if rt.kind == "text":
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        return repr(value)
    if rt.kind == "enum":
        if value not in rt.options:
            close = difflib.get_close_matches(value, rt.options, n=1)
            hint = (
                f"did you mean {close[0]!r}?"
                if close
                else f"choose one of: {', '.join(rt.options)}"
            )
            raise _err(f"default {value!r} is not one of the options", field, hint)
        assert rt.enum_name is not None
        return f"{rt.enum_name}.{_member(value)}"
    raise _err(
        f"defaults are not supported for {rt.kind} fields in v0",
        field,
        "set the value in a does block instead",
    )


def resolve_thing(thing: Thing) -> ResolvedThing:
    fields = []
    for f in thing.fields:
        rt = resolve_type(f.type, thing=thing.name, field=f.name)
        fields.append(ResolvedField(name=f.name, type=rt, default_py=resolve_default(rt, f)))
    return ResolvedThing(name=thing.name, fields=tuple(fields))


def resolve_program(program: Program) -> dict[str, ResolvedThing]:
    """Resolve every thing. Raises :class:`LoomSyntaxError` at the first bad type."""
    return {t.name: resolve_thing(t) for t in program.things}


def _camel(name: str) -> str:
    return "".join(part[:1].upper() + part[1:] for part in re.split(r"[^0-9A-Za-z]+", name) if part)


def _member(option: str) -> str:
    """Enum member name for an option: ``paid`` -> ``PAID``, ``in progress`` -> ``IN_PROGRESS``."""
    name = re.sub(r"[^0-9A-Za-z]+", "_", option).strip("_").upper() or "OPTION"
    return f"_{name}" if name[0].isdigit() else name


member_name = _member
