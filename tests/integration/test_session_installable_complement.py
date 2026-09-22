"""Integration: a slice session's write set carries the table fns nothing
installs yet, and the slots its writable builder's literal installs, read off
the real extractor's records.

The class, run `195091-72b92f1e` (2026-09-21, cluster `stable`): the session
reads its editable set off the failing tests' coverage. A free fn that fits a
field of a fn-pointer table but that no table literal names is executed by
nothing, so coverage can never admit it — while the producer that should
install it IS in the slice. File-first offered those fns but not the
producer. The joint write the leftovers needed was expressible on no route.

The crate is invented: a `Slots` table with two fn-pointer fields, a
registry method that builds it from two wired free fns, two free fns of the
same signatures that nothing names, and one free fn that fits no field. The
real syn extractor enumerates it (no cargo). The negative control is the
coverage slice alone — the builder and the wired fns — with no complement:
the unwired fns are absent from the write set, the live shape of the run.

The second half, run `92187-f71f12bf` (2026-09-22, cluster `stable`): the
builder was in the session and a slot its literal installs was not, because
the failing path never reached the slot; the model's reply rewrote the slot
and the splicer refused it as outside the editable set. The negative control
is the pre-change relation (unwired only) on the same tree and write set.

Needs only files and the extractor release binary, so it runs in the
default suite; `scripts/run_integration.sh` runs it with the rest.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from pipeline_core.adapters.extractor import CliExtractor
from pipeline_core.domain.installable_fns import table_complement
from pipeline_core.domain.models import FnLocator
from pipeline_core.domain.rust_idents import fn_reference_counts

pytestmark = [pytest.mark.integration]

_REPO = Path(__file__).resolve().parents[2]
_EXTRACTOR = (_REPO / "tools" / "skeleton_extractor" / "target" / "release"
              / "skeleton-extractor")

_HOOKS = """\
pub struct Raw;
pub struct Tag(u32);
pub struct Handle<T>(*const T);
pub struct Owned<T>(*mut T);

pub(crate) struct Slots {
    on_open: unsafe fn(Handle<Raw>, Tag) -> Option<Handle<()>>,
    on_close: unsafe fn(Owned<Raw>),
}

pub struct Registry {
    slots: &'static Slots,
}

impl Registry {
    pub fn register<T: 'static>() -> Self {
        let slots = Box::leak(Box::new(Slots {
            on_open: open_plain::<T>,
            on_close: close_plain::<T>,
        }));
        Registry { slots }
    }
}

unsafe fn open_plain<T>(h: Handle<Raw>, tag: Tag) -> Option<Handle<()>> {
    None
}

unsafe fn close_plain<T>(h: Owned<Raw>) {}

unsafe fn open_nested<T>(h: Handle<Raw>, tag: Tag) -> Option<Handle<()>> {
    None
}

unsafe fn close_nested<T>(h: Owned<Raw>) {}

fn lookup(h: Handle<Raw>) -> u32 {
    0
}
"""


@pytest.fixture(scope="module")
def tree(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if not _EXTRACTOR.exists():
        pytest.skip("extractor release binary not built (bash pre_run.sh builds it)")
    root = tmp_path_factory.mktemp("installable_complement")
    (root / "src").mkdir()
    (root / "src" / "lib.rs").write_text("mod hooks;\n")
    (root / "src" / "hooks.rs").write_text(_HOOKS)
    return root


def _complement(tree: Path, writable: set[str], *,
                unwired_only: bool = False) -> list[str]:
    ex = CliExtractor(_EXTRACTOR)
    fns = ex.enumerate_fns(tree) or []
    types = ex.enumerate_types(tree) or []
    rel = "src/hooks.rs"
    assert any(f.file == rel for f in fns), "extractor rooting changed: no src/ file key"
    return [i.fn_name for i in table_complement(
        fns, types, frozenset(FnLocator(rel, n) for n in writable), rel,
        reference_counts=fn_reference_counts((tree / rel).read_text()))
        if i.unwired or not unwired_only]


def test_premise_the_extractor_reports_the_table_fields_as_fn_pointers(tree: Path) -> None:
    """The relation keys on the extractor printing a fn-pointer field type;
    if it stops doing so the complement is silently empty."""
    types = CliExtractor(_EXTRACTOR).enumerate_types(tree) or []
    slots = next(t for t in types if t.name == "Slots")
    assert {f.name for f in slots.fields} == {"on_open", "on_close"}
    assert all("fn" in f.ty for f in slots.fields)


def test_the_builder_in_the_write_set_admits_the_unwired_fns(tree: Path) -> None:
    assert _complement(tree, {"register", "open_plain", "close_plain"}) == [
        "close_nested", "open_nested"]


def test_negative_control_no_builder_in_the_write_set(tree: Path) -> None:
    """The coverage slice before the producer entered it: nothing admitted."""
    assert _complement(tree, {"open_plain", "close_plain", "lookup"}) == []


def test_the_builder_alone_admits_the_slots_its_literal_installs(tree: Path) -> None:
    """The failing path reached the builder and not its wired slots."""
    assert _complement(tree, {"register"}) == [
        "close_nested", "open_nested", "close_plain", "open_plain"]


def test_negative_control_unwired_only_leaves_the_wired_slots_out(tree: Path) -> None:
    """The pre-change relation on the same write set: the slot the builder
    installs is refused, the live shape of the run."""
    got = _complement(tree, {"register"}, unwired_only=True)
    assert "close_plain" not in got and "open_plain" not in got
