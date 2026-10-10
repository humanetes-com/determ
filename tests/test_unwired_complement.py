"""The session's write set carries the table complement of its builders.

Run `195091-72b92f1e` (2026-09-21, cluster `stable`): the slice session's
editable set is read off the failing tests' coverage, so it held the producer
that wraps an error and every fn that builds the crate's fn-pointer table, but
not the two table fns a correct producer must install. Each was referenced by
nothing but its own declaration, so no test executed it, so coverage could
never admit it. File-first offered both and never had the producer in its
write list. No reply on any route could write the pair.

Run `92187-f71f12bf` (2026-09-22) found the second half of the same class:
the session held the builder whose literal installs a drop slot, not the slot,
because the failing path never reached it; the model's first reply rewrote the
slot and the splicer refused it. A slot the writable builder's own literal
names is as much the builder's other half as a slot nothing installs.

The shape here is invented: a table `Slots` with two fn-pointer fields, a
registry method that builds it, one free fn already wired into the literal,
one free fn nothing installs, and one free fn whose signature fits no field.
"""
from __future__ import annotations

import ast
from pathlib import Path

from pipeline_core.application.reconstruct.session_write_set import session_write_set
from pipeline_core.domain.installable_fns import table_complement
from pipeline_core.domain.models import FieldRecord, FnLocator, FnRecord, FnSite, TypeRecord
from pipeline_core.domain.slice_session import SessionSlice
from pipeline_core.prompts.slice_session import build_slice_session_prompt, render_slice_file

REL = "src/hooks.rs"

SLOTS = TypeRecord(file=REL, kind="struct", name="Slots", fields=(
    FieldRecord(name="on_open", ty="unsafe fn(Handle<Raw>, Tag) -> Option<Handle<()>>"),
    FieldRecord(name="on_close", ty="unsafe fn(Owned<Raw>)"),
))

BUILDER = FnRecord(
    file=REL, fn_name="register", container="impl Registry",
    sig_text="fn register<T>(item: T) -> Self",
    body_text="let slots = Slots { on_open: open_plain::<T>, on_close: close_plain::<T> };")
WIRED = FnRecord(
    file=REL, fn_name="open_plain", container="",
    sig_text="unsafe fn open_plain<T>(h: Handle<Raw>, tag: Tag) -> Option<Handle<()>>")
UNWIRED = FnRecord(
    file=REL, fn_name="open_nested", container="",
    sig_text="unsafe fn open_nested<T>(h: Handle<Raw>, tag: Tag) -> Option<Handle<()>>")
UNWIRED_CLOSE = FnRecord(
    file=REL, fn_name="close_nested", container="",
    sig_text="unsafe fn close_nested<T>(h: Owned<Raw>)")
OFF_SHAPE = FnRecord(
    file=REL, fn_name="lookup", container="",
    sig_text="fn lookup(h: Handle<Raw>) -> u32")
RECORDS = [BUILDER, WIRED, UNWIRED, UNWIRED_CLOSE, OFF_SHAPE]
COUNTS = {"register": 1, "open_plain": 2, "open_nested": 1,
          "close_nested": 1, "lookup": 3}


def _complement(writable: set[str]) -> list[str]:
    return [i.fn_name for i in table_complement(
        RECORDS, [SLOTS], frozenset(FnLocator(REL, n) for n in writable), REL,
        reference_counts=COUNTS)]


def test_a_writable_table_builder_brings_the_fns_nothing_installs() -> None:
    """The plan's sentence: a write set holding the table-builder carries the
    fns of a field's signature that no table names, unwired first."""
    assert _complement({"register"})[:2] == ["close_nested", "open_nested"]


def test_a_writable_builder_brings_the_slot_its_own_literal_installs() -> None:
    """The second half: `open_plain` is wired — by the writable builder's
    literal — so coverage of a failing path that never reaches it cannot
    admit it, and the builder is writable without it."""
    assert _complement({"register"}) == [
        "close_nested", "open_nested", "open_plain"]


def test_a_wired_slot_named_only_by_a_builder_outside_the_write_set() -> None:
    """A second builder of the same table is writable and does not name
    `open_plain`; the builder that does is not writable. The route cannot
    change what installs it, so coverage still decides it."""
    other = FnRecord(
        file=REL, fn_name="register_nested", container="impl Registry",
        sig_text="fn register_nested<T>(item: T) -> Self",
        body_text="let slots = Slots { on_open: open_nested::<T>, "
                  "on_close: close_nested::<T> };")
    got = [i.fn_name for i in table_complement(
        [*RECORDS, other], [SLOTS],
        frozenset({FnLocator(REL, "register_nested")}), REL,
        reference_counts={**COUNTS, "open_nested": 2, "close_nested": 2,
                          "register_nested": 1})]
    assert got == ["close_nested", "open_nested"]
    assert "open_plain" not in got


def test_off_shape_fns_stay_out() -> None:
    assert "lookup" not in _complement({"register"})  # fits no field


def test_no_writable_builder_no_complement() -> None:
    """Negative control: the same tree with a write set that builds no table
    (the coverage slice before a producer is in it) admits nothing, so the
    session's write set is the slice alone — the live shape of the run."""
    assert _complement({"lookup", "open_plain"}) == []


def test_an_already_writable_fn_is_not_admitted_twice() -> None:
    assert _complement({"register", "open_nested"}) == [
        "close_nested", "open_plain"]


def test_raw_extractor_dicts_and_typed_records_agree() -> None:
    """The file route holds the extractor's dicts, the session its FnRecords;
    both ask the one relation and must get one answer."""
    dicts = [{"file": r.file, "fn_name": r.fn_name, "container": r.container,
              "sig_text": r.sig_text, "body_text": r.body_text} for r in RECORDS]
    typed = table_complement(RECORDS, [SLOTS], frozenset({FnLocator(REL, "register")}),
                               REL, reference_counts=COUNTS)
    raw = table_complement(dicts, [SLOTS], frozenset({FnLocator(REL, "register")}),
                             REL, reference_counts=COUNTS)
    assert typed == raw


def _site(r: FnRecord) -> FnSite:
    return FnSite(r.file, r.fn_name, r.container)


def test_the_session_header_marks_the_admitted_fn_installable() -> None:
    """The admitted fn is editable and says why; the coverage mark it would
    otherwise get ('NOT executed') would tell the model to leave it alone."""
    text = "\n".join(f"{r.sig_text} {{ {r.body_text} }}" for r in RECORDS)
    head = render_slice_file(
        REL, text, [BUILDER, UNWIRED], RECORDS,
        executed={_site(BUILDER)}, coverage_known=True,
        installable={_site(UNWIRED): "Slots.on_open"}).splitlines()[0]
    assert "open_nested (free fn) [installable: fits Slots.on_open" in head
    assert "open_nested (free fn) [NOT executed" not in head
    assert "register (impl Registry) [executed by the failing tests]" in head


def test_without_the_complement_the_header_cannot_name_it() -> None:
    """Negative control: the pre-change write set (coverage slice only)."""
    text = "\n".join(f"{r.sig_text} {{ {r.body_text} }}" for r in RECORDS)
    head = render_slice_file(
        REL, text, [BUILDER], RECORDS,
        executed={_site(BUILDER)}, coverage_known=True).splitlines()[0]
    assert "open_nested" not in head


def test_turn0_legend_explains_installable_marks_only_when_present() -> None:
    sl = SessionSlice(tests=("t_open",), units=(_site(BUILDER), _site(UNWIRED)))

    def _build(installable: dict[FnSite, str] | None) -> str:
        return build_slice_session_prompt(
            module_name="m", cluster_name="c", slice_=sl,
            records={_site(BUILDER): BUILDER, _site(UNWIRED): UNWIRED},
            all_records=RECORDS, file_texts={REL: "fn x() {}"},
            target_evidence={}, test_blocks=[], types_section="",
            executed=frozenset({_site(BUILDER)}), installable=installable)

    with_ = _build({_site(UNWIRED): "Slots.on_open"})
    without = _build(None)
    assert "A unit marked installable" in with_
    assert "A unit marked installable" not in without


def _slice_file_text() -> str:
    return "\n".join(f"{r.sig_text} {{ {r.body_text} }}" for r in RECORDS)


def test_the_session_open_admits_the_complement_into_units_and_header() -> None:
    """The consumer the class goes through: the write set the session opens
    with (records, units, marks) and the turn-0 prompt built from it. The
    prompt's editable header reads `slice_.units`, so a complement computed
    but not added to the units would fail here."""
    planned = SessionSlice(tests=("t_open",), units=(_site(BUILDER),), file_chars=9000)
    ws = session_write_set(planned, {_site(BUILDER): BUILDER}, RECORDS, [SLOTS],
                           lambda f: _slice_file_text() if f == REL else None)
    assert set(ws.slice_.units) == {_site(BUILDER), _site(UNWIRED),
                                    _site(UNWIRED_CLOSE), _site(WIRED)}
    assert set(ws.records) == set(ws.slice_.units)
    assert ws.installable == {_site(UNWIRED): "Slots.on_open",
                              _site(UNWIRED_CLOSE): "Slots.on_close",
                              _site(WIRED): "Slots.on_open"}
    prompt = build_slice_session_prompt(
        module_name="m", cluster_name="c", slice_=ws.slice_, records=ws.records,
        all_records=RECORDS, file_texts={REL: _slice_file_text()},
        target_evidence={}, test_blocks=[], types_section="",
        executed=frozenset({_site(BUILDER)}), installable=ws.installable)
    header = next(ln for ln in prompt.splitlines() if ln.startswith("<FILE "))
    assert "open_nested (free fn) [installable: fits Slots.on_open" in header
    assert "close_nested (free fn) [installable: fits Slots.on_close" in header
    # The slot the builder's own literal installs, which the failing path
    # did not reach (run `92187-f71f12bf`): editable beside its builder.
    assert "open_plain (free fn) [installable: fits Slots.on_open" in header


def test_the_session_open_keeps_the_planner_rendering_threshold() -> None:
    """A slice the planner fitted by condensing its files opens at that
    threshold, whether or not anything was admitted. Negative control: the
    field-by-field rebuild the open used before, which dropped it to 0 and so
    to the widest default."""
    planned = SessionSlice(tests=("t_open",), units=(_site(WIRED),), file_chars=9000)
    ws = session_write_set(planned, {_site(WIRED): WIRED}, RECORDS, [SLOTS],
                           lambda f: _slice_file_text())
    assert ws.installable == {}
    assert ws.slice_.file_chars == 9000
    old = type(planned)(tests=planned.tests, units=planned.units)
    assert old.file_chars == 0


def test_an_unreadable_file_admits_nothing() -> None:
    planned = SessionSlice(tests=("t_open",), units=(_site(BUILDER),))
    ws = session_write_set(planned, {_site(BUILDER): BUILDER}, RECORDS, [SLOTS],
                           lambda f: None)
    assert ws.slice_.units == (_site(BUILDER),)
    assert ws.installable == {}


def test_the_live_session_opens_through_the_write_set() -> None:
    """Belt: the live turn-0 build passes `installable=` and the session run
    builds its write set with `session_write_set`, not an inline rebuild."""
    src = (Path(__file__).resolve().parents[1] / "pipeline_core"
           / "application" / "reconstruct" / "impl_fill.py").read_text()
    tree = ast.parse(src)
    live = [c for c in ast.walk(tree)
            if isinstance(c, ast.Call)
            and getattr(c.func, "id", "") == "build_slice_session_prompt"
            and any(k.arg == "executed" for k in c.keywords)]
    assert live, "no live session prompt build found"
    for c in live:
        assert any(k.arg == "installable" for k in c.keywords)
        assert any(k.arg == "unit_notes" for k in c.keywords)
    assert "session_write_set(" in src
    assert "type(_sl)(tests=_sl.tests, units=" not in src
