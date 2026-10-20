"""A stub the file route put back on purpose is not a silent splice loss.

Run `1804897-1ad432ba` iter 4 (2026-10-20): the ignored-inputs gate declined
seven `src/error.rs` bodies and the next line printed the same seven as
`POST-FILL MISMATCH`. The same run's `src/nightly.rs` reply routed both of
its `provide` bodies to one impl, so the stub's own impl got none: that one
IS the silent loss the re-scan exists for.
"""
from __future__ import annotations

from pipeline_core.domain.impl_routing import norm_src_rel
from pipeline_core.domain.post_fill_mismatch import still_stubbed_after_accept

_DECLINED = ("context_chain_downcast", "context_chain_drop_rest",
             "context_downcast", "context_drop_rest", "no_backtrace",
             "object_downcast", "object_drop_front")


def _rows(file: str, names: tuple[str, ...]) -> list[dict[str, object]]:
    return [{"file": file, "fn_name": n} for n in names]


def test_declined_bodies_are_not_a_mismatch() -> None:
    rows = _rows("src/error.rs", (*_DECLINED, "erase"))
    asked = {*_DECLINED, "drop"}
    assert still_stubbed_after_accept(
        rows, file_rel="src/error.rs", asked=asked, put_back=set(_DECLINED),
        norm=norm_src_rel) == []
    # NEGATIVE CONTROL: the peel-only exclusion names all seven.
    assert still_stubbed_after_accept(
        rows, file_rel="src/error.rs", asked=asked, put_back=set(),
        norm=norm_src_rel) == sorted(_DECLINED)


def test_a_splice_lost_to_routing_is_still_named() -> None:
    rows = _rows("src/nightly.rs", ("provide",)) + _rows("src/error.rs", ("provide",))
    assert still_stubbed_after_accept(
        rows, file_rel="src/nightly.rs", asked={"provide", "request_ref"},
        put_back=set(), norm=norm_src_rel) == ["provide"]
