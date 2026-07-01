"""Unit tests for the `FnLocator` value object (domain.models).

`FnLocator` names the ubiquitous `(file, fn_name)` fill-unit key. It is a
`NamedTuple` ON PURPOSE: it must stay a *drop-in* for the bare `tuple[str, str]`
it replaced, so every `set` membership, `dict` key, positional unpack and
tuple-equality already in the codebase keeps working. These tests pin that
drop-in contract (so a future refactor to a frozen dataclass would fail loudly)
plus the `.file` / `.fn_name` accessors that are the vocabulary win.
"""
from __future__ import annotations

from pipeline_core.domain.models import FnLocator, Frontier, Stub


def test_accessors_and_positional_order() -> None:
    loc = FnLocator("src/ensure.rs", "check")
    assert loc.file == "src/ensure.rs"
    assert loc.fn_name == "check"
    assert loc[0] == "src/ensure.rs"
    assert loc[1] == "check"


def test_equals_plain_tuple_drop_in() -> None:
    # The whole campaign relies on this: FnLocator interoperates with the bare
    # tuple it replaced (a not-yet-migrated producer still compares equal).
    plain = ("a.rs", "f")
    assert FnLocator("a.rs", "f") == plain
    assert plain == FnLocator("a.rs", "f")


def test_hash_matches_plain_tuple_in_set_and_dict() -> None:
    s: set[FnLocator] = {FnLocator("a.rs", "f")}
    assert ("a.rs", "f") in s              # plain-tuple probe hits the VO key
    d: dict[FnLocator, int] = {FnLocator("a.rs", "f"): 1}
    assert d[("a.rs", "f")] == 1           # type: ignore[index]  # drop-in at runtime


def test_positional_unpack() -> None:
    file, fn = FnLocator("x.rs", "g")
    assert (file, fn) == ("x.rs", "g")


def test_frontier_alias_accepts_fn_locator_key() -> None:
    fr: Frontier = {FnLocator("a.rs", "f"): {"t1"}}
    assert fr[FnLocator("a.rs", "f")] == {"t1"}


def test_stub_key_is_fn_locator() -> None:
    stub = Stub(file="a.rs", fn_name="f", container="", sig_text="fn f()",
                cfg_predicates=())
    assert stub.key == FnLocator("a.rs", "f")
    assert isinstance(stub.key, FnLocator)
