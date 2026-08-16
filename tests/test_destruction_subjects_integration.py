"""The destruction-subject selector against the REAL extractor.

Why this crosses the seam instead of stopping at the domain. `destruction_path_subjects`
takes extractor records, and everything that can go wrong with it goes wrong
BETWEEN the two: what the records' `file` field looks like (the selector's output
has to match frontier keys, which are `src/…`), and which files the enumeration
reaches (the crate root walk includes `tests/`, whose fns no fill may rewrite).
Neither is visible to a test that hand-writes the record dicts, and both were
real: run by hand over `tmp/anyhow/.skeleton-filled/stable` on 2026-08-16, the
first version of the selector returned EIGHT subjects over 323 fns — the five
real ones plus `tests/drop/mod.rs::drop`, `tests/test_downcast.rs::test_drop`
and `tests/test_repr.rs::test_drop`. A subject naming a test file is the defect
entry 173.1 shipped a frame filter for: no fill writes a test, so the key lands
in the frontier and buys a dispatch that cannot change anything.

The fixture crate below is that shape in miniature — a drop family in `src/`, a
`Drop` impl and a `test_drop` under `tests/` — written into `tmp_path`. It is
NOT the crate under reconstruction, deliberately: that tree changes every run
(that is the pipeline's job), so a gate asserting on its contents would fail
exactly when the loop works. `scripts/probe_destruction_subjects.py` is where
the live tree gets read, on demand, by an operator.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from pipeline_core.domain.crash_site import destruction_path_subjects
from pipeline_core.domain.models import FnLocator

_BIN = (Path(__file__).parent.parent / "tools" / "skeleton_extractor"
        / "target" / "release" / "skeleton-extractor")

pytestmark = pytest.mark.skipif(
    not _BIN.exists(), reason="skeleton-extractor binary not built (bash setup.sh)")

_ERROR_RS = """\
pub struct Own<T> { pub ptr: *mut T }
pub struct ErrorImpl<E> { pub obj: E }

pub unsafe fn object_drop<E>(e: Own<ErrorImpl<E>>) {
    let _ = Box::from_raw(e.ptr);
}

pub unsafe fn object_drop_front<E>(e: Own<ErrorImpl<E>>, target: usize) {
    let _ = target;
    let _ = Box::from_raw(e.ptr);
}

pub unsafe fn context_drop_rest<C, E>(e: Own<ErrorImpl<C>>) {
    let _ = Box::from_raw(e.ptr);
}

pub fn downcast<E>(p: *const E) -> E {
    unsafe { core::ptr::read(p) }
}

pub fn construct<E>(obj: E) -> ErrorImpl<E> {
    ErrorImpl { obj }
}
"""

_DROP_IMPL_RS = """\
pub struct Error;

impl Drop for Error {
    fn drop(&mut self) {
        let _ = self;
    }
}
"""

# The two shapes the live tree has under `tests/`, both matching the drop rule.
_TESTS_MOD_RS = """\
pub struct Flag;

impl Drop for Flag {
    fn drop(&mut self) {
        let _ = self;
    }
}
"""

_TESTS_MAIN_RS = """\
#[test]
fn test_drop() {
    assert!(true);
}
"""


def _crate(root: Path) -> Path:
    (root / "src").mkdir(parents=True)
    (root / "tests" / "drop").mkdir(parents=True)
    (root / "src" / "error.rs").write_text(_ERROR_RS, encoding="utf-8")
    (root / "src" / "lib.rs").write_text(_DROP_IMPL_RS, encoding="utf-8")
    (root / "tests" / "drop" / "mod.rs").write_text(_TESTS_MOD_RS, encoding="utf-8")
    (root / "tests" / "test_downcast.rs").write_text(_TESTS_MAIN_RS, encoding="utf-8")
    return root


def _records(root: Path) -> list[dict]:
    from pipeline_core.adapters.extractor import CliExtractor
    recs = CliExtractor(_BIN).enumerate_fns(root)
    assert recs is not None, "the real extractor returned nothing"
    return [r.to_dict() for r in recs]


def test_the_selector_names_the_drop_family_and_no_test_file(tmp_path: Path) -> None:
    """The measurement that found the leak, as a gate. Enumerated at the
    CLUSTER ROOT — which is what the driver passes — so the walk reaches
    `tests/` exactly as it does live."""
    subs = destruction_path_subjects(_records(_crate(tmp_path)))

    assert subs == frozenset({
        FnLocator("src/error.rs", "object_drop"),
        FnLocator("src/error.rs", "object_drop_front"),
        FnLocator("src/error.rs", "context_drop_rest"),
        FnLocator("src/lib.rs", "drop"),
    })
    assert not [k for k in subs if not str(k.file).startswith("src/")], \
        "a subject in a test file is a dispatch that cannot change anything"
    # `downcast` holds `core::ptr::read` and is deliberately absent: the body
    # rule is the fallback for a crate that does not name its drop family, not
    # a widening of a set the names already resolved.
    assert FnLocator("src/error.rs", "downcast") not in subs


def test_the_paths_the_selector_returns_are_the_frontier_key_form(
        tmp_path: Path) -> None:
    """The half a hand-written record dict cannot check. Frontier keys are
    `src/error.rs`; the extractor emits paths relative to the directory it is
    GIVEN, so enumerating the cluster root yields that form and enumerating
    `…/src` yields `error.rs` — which would match no frontier key and no
    layout component. The authorable-file rule turns that mistake into an
    EMPTY set rather than a set of silent near-misses, which is the difference
    between a wiring bug that shows up in one log line and one that reads as
    "the channel found nothing"."""
    root = _crate(tmp_path)
    at_root = {str(FnLocator(r["file"], r["fn_name"]).file)
               for r in _records(root) if r["fn_name"] == "object_drop"}
    assert at_root == {"src/error.rs"}

    at_src = _records(root / "src")
    assert {r["file"] for r in at_src if r["fn_name"] == "object_drop"} == {
        "error.rs"}, "the extractor is relative to the dir it is handed"
    assert destruction_path_subjects(at_src) == frozenset(), \
        "the wrong root must produce nothing, never near-miss keys"


def test_the_body_rule_fires_when_the_crate_names_no_drop_family(
        tmp_path: Path) -> None:
    """A crate whose teardown path is not called `drop` still has one, and the
    ownership primitives are what name it."""
    root = tmp_path / "other"
    (root / "src").mkdir(parents=True)
    (root / "src" / "raw.rs").write_text(
        "pub fn take<E>(p: *mut E) -> Box<E> { unsafe { Box::from_raw(p) } }\n"
        "pub fn plain() -> u32 { 1 }\n", encoding="utf-8")

    assert destruction_path_subjects(_records(root)) == frozenset({
        FnLocator("src/raw.rs", "take")})


_SENTINEL_RS = """\
pub struct Ref<T> { pub p: *const T }
pub struct Slot { pub p: *mut u8 }

pub unsafe fn object_downcast<E>(target: usize) -> Option<Ref<()>> {
    if target == 0 {
        Some(Ref { p: core::ptr::NonNull::<()>::dangling().as_ptr() })
    } else {
        None
    }
}

pub fn take(s: &mut Slot) -> *mut u8 {
    core::mem::replace(&mut s.p, core::ptr::null_mut())
}
"""

_SENTINEL_TEST_RS = """\
#[test]
fn test_sentinel() {
    let _p: *mut u8 = core::ptr::null_mut();
}
"""

_CALLER_RS = """\
pub fn vtable(x: u8) -> u8 { x }
pub fn helper(x: u8) -> u8 { x }
pub fn unrelated(x: u8) -> u8 { x }

pub fn downcast_ref(t: &Table) -> u8 {
    let v = vtable(1);
    (t.helper)(v)
}

pub struct Table { pub helper: fn(u8) -> u8 }
"""


def test_the_sentinel_selector_separates_handing_out_from_placing_in(
        tmp_path: Path) -> None:
    """The live discrimination, through the real extractor. `object_downcast`
    builds a dangling pointer and RETURNS it; `take` builds a null and writes
    it into a slot in exchange for the value it takes out — the same mark, one
    defect and one idiom, and only what the occurrence is an argument to tells
    them apart. The test-side fn is excluded on `authorable_frames`' rule."""
    from pipeline_core.domain.crash_site import placeholder_pointer_subjects

    root = tmp_path / "sent"
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir(parents=True)
    (root / "src" / "error.rs").write_text(_SENTINEL_RS, encoding="utf-8")
    (root / "tests" / "test_sentinel.rs").write_text(
        _SENTINEL_TEST_RS, encoding="utf-8")

    assert placeholder_pointer_subjects(_records(root)) == frozenset({
        FnLocator("src/error.rs", "object_downcast")})


def test_the_callee_selector_returns_frontier_key_paths(tmp_path: Path) -> None:
    """The half a hand-written record dict cannot check, for the second
    selector: the locators it returns must be the `src/…` form the frontier is
    keyed by, and the fn-pointer call shape — `(t.helper)(v)`, which is how the
    live `(vtbl.object_downcast)(…)` reads — must survive the parse."""
    from pipeline_core.domain.crash_site import callee_subjects

    root = tmp_path / "call"
    (root / "src").mkdir(parents=True)
    (root / "src" / "error.rs").write_text(_CALLER_RS, encoding="utf-8")

    subs = callee_subjects(
        FnLocator("src/error.rs", "downcast_ref"), _records(root))
    assert subs == frozenset({
        FnLocator("src/error.rs", "vtable"),
        FnLocator("src/error.rs", "helper")})
    assert FnLocator("src/error.rs", "unrelated") not in subs
