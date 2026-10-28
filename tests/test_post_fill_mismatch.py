"""A stub the file route put back on purpose is not a silent splice loss.

Run `1804897-1ad432ba` iter 4 (2026-10-20): the ignored-inputs gate declined
seven `src/error.rs` bodies and the next line printed the same seven as
`POST-FILL MISMATCH`. The same run's `src/nightly.rs` reply routed both of
its `provide` bodies to one impl, so the stub's own impl got none: that one
IS the silent loss the re-scan exists for.

The comparison is per body: a stubbed homonym the reply was never asked for
is not a lost answer of the one it was (`BodyKey`, name plus container).
"""
from __future__ import annotations

from pipeline_core.domain.ignored_inputs import body_identity
from pipeline_core.domain.impl_routing import norm_src_rel
from pipeline_core.domain.post_fill_mismatch import (
    BodyKey,
    put_back_bodies,
    still_stubbed_after_accept,
)

_DECLINED = ("context_chain_downcast", "context_chain_drop_rest",
             "context_downcast", "context_drop_rest", "no_backtrace",
             "object_downcast", "object_drop_front")


def _rows(file: str, names: tuple[str, ...], container: str = "") -> list[dict[str, object]]:
    return [{"file": file, "fn_name": n, "container": container} for n in names]


def _keys(names: tuple[str, ...], container: str = "") -> frozenset[BodyKey]:
    return frozenset(BodyKey.of(n, container) for n in names)


def test_declined_bodies_are_not_a_mismatch() -> None:
    rows = _rows("src/error.rs", (*_DECLINED, "erase"))
    asked = _keys((*_DECLINED, "drop"))
    assert still_stubbed_after_accept(
        rows, file_rel="src/error.rs", asked=asked,
        put_back=put_back_bodies(frozenset(), _DECLINED, asked),
        norm=norm_src_rel) == []
    # NEGATIVE CONTROL: the peel-only exclusion names all seven.
    assert still_stubbed_after_accept(
        rows, file_rel="src/error.rs", asked=asked, put_back=frozenset(),
        norm=norm_src_rel) == sorted(_DECLINED)


def test_a_splice_lost_to_routing_is_still_named() -> None:
    rows = _rows("src/nightly.rs", ("provide",)) + _rows("src/error.rs", ("provide",))
    assert still_stubbed_after_accept(
        rows, file_rel="src/nightly.rs", asked=_keys(("provide", "request_ref")),
        put_back=frozenset(), norm=norm_src_rel) == ["provide"]


def test_a_stubbed_homonym_that_was_not_asked_is_not_a_lost_answer() -> None:
    """Two `make` bodies, one per impl block. The reply was asked the first,
    filled it, and the second is still a stub nobody asked for. Matched by
    name, the second read as the first's lost answer. Control: the first
    still stubbed is named, with its container."""
    one, two = "impl Alpha", "impl Beta"
    rows = _rows("src/a.rs", ("make",), two)
    asked = _keys(("make",), one)
    assert still_stubbed_after_accept(
        rows, file_rel="src/a.rs", asked=asked, put_back=frozenset(),
        norm=norm_src_rel) == []
    lost = _rows("src/a.rs", ("make",), one)
    assert still_stubbed_after_accept(
        lost, file_rel="src/a.rs", asked=asked, put_back=frozenset(),
        norm=norm_src_rel) == [BodyKey.of("make", one).render()]


def test_a_put_back_keyed_by_body_spares_only_that_body() -> None:
    """A decline that told homonyms apart puts back one body; the other,
    asked and still a stub, is a loss. A bare name names the one asked
    body of that name. Two or more asked bodies of that name: the refusal
    did not say which, so it names none of them."""
    one, two = "impl Alpha", "impl Beta"
    asked = _keys(("make",), one) | _keys(("make",), two)
    rows = _rows("src/a.rs", ("make",), one) + _rows("src/a.rs", ("make",), two)
    back = put_back_bodies(frozenset(), (body_identity("make", one),), asked)
    assert still_stubbed_after_accept(
        rows, file_rel="src/a.rs", asked=asked, put_back=back,
        norm=norm_src_rel) == [BodyKey.of("make", two).render()]
    assert put_back_bodies(frozenset(), ("make",), asked) == frozenset()
    only = _keys(("make",), one)
    assert put_back_bodies(frozenset(), ("make",), only) == only
