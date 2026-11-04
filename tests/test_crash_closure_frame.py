"""A crash frame inside a closure names the function that holds the closure.

Run `19493-36a4e056` (2026-11-04, cluster `stable`, iter 1): gdb traced a
SIGSEGV's faulting address to a frame whose demangled name ended in
`{closure#0}`. `bare_fn_name` took the last path segment, so the crash
culprit, the owner map's owner of the crashed test and the head of the
dispatch batch was `src/error.rs::{closure#0}`: a name no extractor record
carries and no route can write (`HEALTH ‼ SCHEDULE_INVARIANT …
owner_unrouted` on both runs that saw it). `frame_site`, which resolves a
frame by its line, refused the same frame, because the record at that line
is named for the closure's creator.

Claims (invented shapes, no crate names):
- a synthesized segment (`{closure#0}`, `{{closure}}`) never names the
  frame's unit; the item that holds it does;
- the closure frame's culprit, blame and crash-owner locators name the
  creator, and `frame_site` resolves the frame to the creator's record;
- an inlined frame whose line lies in another record still resolves to
  nothing (the guard `frame_site` exists for).
Negative control: the pre-fix frame name (`{closure#0}`) is in no record
and `frame_site` resolves it to nothing.
"""
from __future__ import annotations

from pipeline_core.domain.crash_site import (
    CrashFrame,
    bare_fn_name,
    faulting_stack_records,
    frame_site,
    site_from_frames,
)
from pipeline_core.domain.models import FnLocator, FnRecord, FnSite

_SRC = """pub struct Item { pub tag: u32 }

pub fn read(p: *const Item) -> u32 {
    unsafe { (*p).tag }
}

pub fn outer() -> u32 {
    let f = || {
        let p = 8 as *const Item;
        read(p)
    };
    f()
}

pub fn other() -> u32 {
    1
}
"""
_FILE = "src/a.rs"


def _records() -> list[FnRecord]:
    raw = _SRC.encode()
    out = []
    for name in ("read", "outer", "other"):
        sig = raw.index(f"pub fn {name}(".encode()) + len(b"pub ")
        brace = raw.index(b"{", sig)
        depth, i = 0, brace
        while True:
            if raw[i:i + 1] == b"{":
                depth += 1
            elif raw[i:i + 1] == b"}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        out.append(FnRecord(file=_FILE, fn_name=name, sig_byte_start=sig,
                            body_byte_start=brace + 1, body_byte_end=i,
                            container=""))
    return out


def _text(rel: str) -> str | None:
    return _SRC if rel == _FILE else None


#: The closure's body line (`read(p)`), inside `outer`'s span.
_CLOSURE_LINE = 10
#: `other`'s body line.
_OTHER_LINE = 16


def _raw(func: str, line: int, *ptrs: tuple[int, int]) -> dict[str, object]:
    return {"func": func, "file": _FILE, "line": line,
            "ptr_args": [list(p) for p in ptrs]}


def test_a_synthesized_segment_names_the_item_that_holds_it() -> None:
    assert bare_fn_name("k::m::outer::{closure#0}") == "outer"
    assert bare_fn_name("k::m::outer::{{closure}}") == "outer"
    assert bare_fn_name("k::m::outer::{closure#0}::{closure#1}") == "outer"
    assert bare_fn_name("<k::A as k::T>::method::{closure#0}") == "method"
    assert bare_fn_name("k::{impl#3}::method::{closure#0}") == "method"
    # Unchanged forms.
    assert bare_fn_name("k::{impl#6}::drop") == "drop"
    assert bare_fn_name("<k::E>::build::<k::X>") == "build"
    # Nothing but synthesized segments: the last one, as before.
    assert bare_fn_name("{closure#0}") == "{closure#0}"


def _closure_site():
    # The reader faulted on 0x8; the closure frame handed it that address.
    frames = [_raw("k::read", 4, (0x8, 4)),
              _raw("k::outer::{closure#0}", _CLOSURE_LINE, (0x5555_0000, 1)),
              _raw("k::outer", 12)]
    return site_from_frames("tests/t.rs", "t", "SIGSEGV", frames,
                            fault_addr=0x8)


def test_the_closure_frame_blames_and_owns_through_its_creator() -> None:
    site = _closure_site()
    assert site.origin_traced
    assert site.blamed is not None and site.blamed.fn_name == "outer"
    assert site.culprit_locators == frozenset({FnLocator(_FILE, "outer")})
    owners = faulting_stack_records((site,), {}, {"t"})["t"]
    assert FnLocator(_FILE, "outer") in owners
    assert all(not loc.fn_name.startswith("{") for loc in owners)
    # Every locator names a declared record.
    declared = {r.fn_name for r in _records()}
    assert {loc.fn_name for loc in owners} <= declared


def test_frame_site_resolves_the_closure_frame_to_its_creator() -> None:
    site = _closure_site()
    closure = next(f for f in site.all_frames if f.line == _CLOSURE_LINE)
    assert frame_site(closure, _records(), _text) == FnSite(_FILE, "outer", "")


def test_negative_control_the_tail_name_resolves_to_nothing() -> None:
    pre_fix = CrashFrame(file_rel=_FILE, line=_CLOSURE_LINE,
                         fn_name="{closure#0}",
                         symbol="k::outer::{closure#0}")
    assert frame_site(pre_fix, _records(), _text) is None
    assert "{closure#0}" not in {r.fn_name for r in _records()}


def test_an_inlined_frame_labelled_from_elsewhere_still_resolves_to_nothing(
        ) -> None:
    liar = CrashFrame(file_rel=_FILE, line=_OTHER_LINE, fn_name="outer",
                      symbol="k::outer::{closure#0}")
    assert frame_site(liar, _records(), _text) is None
