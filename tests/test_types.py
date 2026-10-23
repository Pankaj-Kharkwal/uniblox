from __future__ import annotations

import pytest

from loom.errors import LoomSyntaxError
from loom.frontend import load_text
from loom.nodes import Field, TypeRef
from loom.typesys import member_name, resolve_default, resolve_type

H = "app X\ntarget: python 3.12\n"


@pytest.mark.parametrize(
    ("loom_type", "kind", "py", "currency"),
    [
        ("text", "text", "str", None),
        ("email", "email", "EmailStr", None),
        ("number", "number", "int", None),
        ("money (INR)", "money", "Decimal", "INR"),
        ("money (usd)", "money", "Decimal", "USD"),
        ("date", "date", "date", None),
        ("datetime", "datetime", "datetime", None),
        ("datetime?", "datetime", "Optional[datetime]", None),
        ("text?", "text", "Optional[str]", None),
        ("one of draft, sent, paid", "enum", "TStatus", None),
    ],
)
def test_type_table(loom_type: str, kind: str, py: str, currency: str | None) -> None:
    fe = load_text(H + f"thing T\n  status: {loom_type}\n")
    (field,) = fe.things["T"].fields
    assert field.type.kind == kind
    assert field.type.py == py
    assert field.type.currency == currency


def test_one_of_becomes_named_enum_with_options() -> None:
    fe = load_text(H + "thing Invoice\n  status: one of draft, sent, paid\n")
    rt = fe.things["Invoice"].fields[0].type
    assert rt.enum_name == "InvoiceStatus"
    assert rt.options == ("draft", "sent", "paid")
    assert "from enum import StrEnum" in rt.imports


@pytest.mark.parametrize(
    ("decl", "default_py"),
    [
        ("number = 0", "0"),
        ("number = -3", "-3"),
        ("money (INR) = 1,250.50", 'Decimal("1250.50")'),
        ("text = hello", "'hello'"),
        ('text = "two words"', "'two words'"),
        ("one of low, normal, high = normal", "TF.NORMAL"),
        ("datetime?", "None"),
        ("text? = none", "None"),
        ("number", None),
    ],
)
def test_defaults(decl: str, default_py: str | None) -> None:
    fe = load_text(H + f"thing T\n  f: {decl}\n")
    assert fe.things["T"].fields[0].default_py == default_py
    assert fe.things["T"].fields[0].required is (default_py is None)


@pytest.mark.parametrize(
    ("decl", "message", "hint"),
    [
        ("cash", "unknown type 'cash'", "known types"),
        ("string", "unknown type 'string'", "did you mean 'text'?"),
        ("integer", "unknown type 'integer'", "did you mean 'number'?"),
        ("datetme", "unknown type 'datetme'", "did you mean 'datetime'?"),
        ("money", "money needs a currency", "money (INR)"),
        ("money (RUPEES)", "'RUPEES' is not a currency code", "3-letter"),
        ("text (x)", "type 'text' takes no argument", "write just `text`"),
        ("one of a, b, a", "option 'a' is listed twice", None),
        ("number = lots", "default 'lots' is not a whole number", None),
        ("money (INR) = free", "default 'free' is not an amount of money", None),
        ("one of a, b = c", "default 'c' is not one of the options", "choose one of: a, b"),
        ("one of apple, banana = aple", "default 'aple' is not one of the options", "'apple'"),
        ("date = today", "defaults are not supported for date fields in v0", "does block"),
    ],
)
def test_type_errors_have_messages_and_suggestions(
    decl: str, message: str, hint: str | None
) -> None:
    with pytest.raises(LoomSyntaxError) as info:
        load_text(H + f"thing T\n  f: {decl}\n", "t.loom")
    assert info.value.message == message
    if hint:
        assert info.value.hint is not None and hint in info.value.hint
    assert info.value.line == 4
    assert info.value.source is not None


def test_resolve_without_thing_context_and_member_names() -> None:
    rt = resolve_type(TypeRef("one of", options=("in progress", "2nd")), field="state")
    assert rt.enum_name == "State"
    assert member_name("in progress") == "IN_PROGRESS"
    assert member_name("2nd") == "_2ND"
    assert member_name("!!") == "OPTION"
    assert resolve_default(rt, Field("state", TypeRef("one of"), "2nd")) == "State._2ND"
