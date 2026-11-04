"""A reference doc test is met by the re-hosted example that stands for it.

The floor measured the original's doc tests and compared them by name, so on
run `391506-3403a6a7` (2026-10-22) it reported 26 doc tests "absent here"
while each had a re-hosted copy (`tests/doc_*_gen_N.rs`) passing in the
oracle. The emitter now records every fence's origin, the reference keeps the
line rustdoc named each doc test by, and one join pairs them
(`domain.doc_stand_in`). On the pristine anyhow tree (2026-11-02) 24 doc tests
join at record line plus fence offset and the 2 written inside a
`macro_rules!` body join at one shift; `scripts/doc_stand_in_fixture.py`
recomputes that on real records. These fixtures are invented shapes: a crate
`gadgets`, items `Widget` and `make`, a macro body at line 40 expanded at 90.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from pipeline_core.application.doc_example_tests import (
    DocExampleDeps,
    emit_doc_example_tests,
)
from pipeline_core.application.reference_run import _from_json, _to_json
from pipeline_core.domain.doc_example_test import doc_fences, rendered_sha
from pipeline_core.domain.doc_stand_in import (
    ORIGINS_STORE,
    DocCover,
    DocOrigin,
    doc_covers,
    doc_test_file,
    join_origins,
    origins_from_json,
)
from pipeline_core.domain.models import DocItemRecord
from pipeline_core.domain.reference_floor import (
    ClusterReference,
    ReferenceMeasurement,
    judge_against_reference,
)
from pipeline_core.domain.same_failure import FailedReading
from pipeline_core.domain.test_results import join_capture, join_world_captures
from pipeline_core.domain.world_floor import judge_every_world

_D = "Doc-tests gadgets"
_WIDGET = (_D, "src/lib.rs - Widget#0")
_MAKE_0 = (_D, "src/lib.rs - Widget::make#0")
_MAKE_1 = (_D, "src/lib.rs - Widget::make#1")
_MAC_0 = (_D, "src/lib.rs - gadget#0")
_MAC_1 = (_D, "src/lib.rs - gadget#1")


def _o(item: int, offset: int, *, index: int = 0, macro: bool = False,
       kept: str = "", reason: str = "") -> DocOrigin:
    return DocOrigin("src/lib.rs", item, index, offset, macro,
                     kept_as=kept, test_fn=kept.replace("_gen_", "_example_")[:-3] if kept else "",
                     sha="h" if kept else "", reason=reason)


def test_the_file_of_a_doc_test_name_raw_or_normalized() -> None:
    assert doc_test_file("src/lib.rs - Context (line 543)") == "src/lib.rs"
    assert doc_test_file("src/lib.rs - Context#1") == "src/lib.rs"
    assert doc_test_file("src/lib.rs -#0") == "src/lib.rs"
    assert doc_test_file("src/lib.rs - (line 22)") == "src/lib.rs"


def test_fence_offsets_are_the_opening_line_and_skipped_fences_are_kept() -> None:
    doc = " Head.\n\n ```\n let a = 1;\n ```\n ```no_run\n loop {}\n ```\n ```text\n x\n ```\n"
    fences = doc_fences(doc)
    assert [(f.offset, f.runs, f.skip_tag) for f in fences] == [(2, True, ""), (5, False, "no_run")]


def test_a_doc_on_an_item_joins_at_its_line_and_only_there() -> None:
    lines = {_WIDGET: ("src/lib.rs", 12), _MAKE_0: ("src/lib.rs", 31)}
    joined = join_origins(lines, [_o(10, 2), _o(25, 6)])
    assert joined[_WIDGET].item_line == 10 and joined[_MAKE_0].item_line == 25
    # NEGATIVE CONTROL for the restriction: a known-base record whose line is
    # off is not moved onto a free line, though one shift would place it.
    assert join_origins({_MAKE_0: ("src/lib.rs", 33)}, [_o(25, 6)]) == {}


def test_a_doc_in_a_macro_body_joins_at_its_unique_shift() -> None:
    """Measured shape: the record says line 40, rustdoc names its two
    examples from the expansion at 90, offsets kept."""
    lines = {_WIDGET: ("src/lib.rs", 12), _MAC_0: ("src/lib.rs", 106),
             _MAC_1: ("src/lib.rs", 117)}
    origins = [_o(10, 2), _o(40, 16, macro=True), _o(40, 27, index=1, macro=True)]
    joined = join_origins(lines, origins)
    assert joined[_MAC_0].offset == 16 and joined[_MAC_1].offset == 27
    assert joined[_WIDGET].item_line == 10


def test_a_macro_body_record_with_more_than_one_fitting_shift_does_not_join() -> None:
    lines = {_MAC_0: ("src/lib.rs", 106), _MAC_1: ("src/lib.rs", 117)}
    assert join_origins(lines, [_o(40, 16, macro=True)]) == {}


def test_two_macro_records_landing_on_one_line_both_stay_out() -> None:
    lines = {_MAC_0: ("src/lib.rs", 106)}
    origins = [_o(40, 16, macro=True), _o(60, 3, macro=True)]
    assert join_origins(lines, origins) == {}


def test_a_cover_names_why_there_is_no_stand_in() -> None:
    lines = {_WIDGET: ("src/lib.rs", 12), _MAKE_0: ("src/lib.rs", 31),
             _MAKE_1: ("src/lib.rs", 44), (_D, "src/lib.rs - Gone#0"): ("src/lib.rs", 70)}
    origins = [_o(10, 2, kept="doc_lib_gen_1.rs"),
               _o(25, 6, reason="dropped at emission: passes in no world as rendered"),
               _o(25, 19, index=1, kept="doc_lib_gen_2.rs")]
    covers = doc_covers(lines, origins, {"doc_lib_gen_1.rs": "h", "doc_lib_gen_2.rs": "x"})
    assert covers[_WIDGET] == DocCover(stand_in=("tests/doc_lib_gen_1.rs", "doc_lib_example_1"))
    assert covers[_MAKE_0].gap.startswith("dropped at emission")
    assert "changed after emission" in covers[_MAKE_1].gap
    assert covers[(_D, "src/lib.rs - Gone#0")].gap == "no re-hosted example matches its line"


def _ref(covers: dict[tuple[str, str], DocCover]) -> ClusterReference:
    return ClusterReference(cluster="base", passed=frozenset({_WIDGET, _MAKE_0, _MAKE_1}),
                            announced=3, reported=3, doc_covers=covers)


def _reading(failed: frozenset[tuple[str, str]] = frozenset()) -> FailedReading:
    return FailedReading(pairs=failed, signatures={})


_S1 = ("tests/doc_lib_gen_1.rs", "doc_lib_example_1")
_S2 = ("tests/doc_lib_gen_2.rs", "doc_lib_example_2")


def test_a_doc_test_whose_stand_in_passes_is_met_not_reported() -> None:
    covers = {_WIDGET: DocCover(stand_in=_S1), _MAKE_0: DocCover(stand_in=_S2),
              _MAKE_1: DocCover(gap="not re-hostable: rustdoc `no_run`")}
    verdict = judge_against_reference(_ref(covers), frozenset({_S1, _S2}), _reading())
    assert verdict.met
    assert verdict.doc_stand_ins == frozenset({_WIDGET, _MAKE_0})
    assert verdict.advisory_unmet == frozenset({_MAKE_1})
    assert "not re-hostable" in verdict.reason and "Widget#0" not in verdict.reason
    # NEGATIVE CONTROL: the reference without covers (the schema-3 store)
    # reports all three absent while both stand-ins pass.
    old = judge_against_reference(_ref({}), frozenset({_S1, _S2}), _reading())
    assert old.advisory_unmet == frozenset({_WIDGET, _MAKE_0, _MAKE_1})


def test_a_stand_in_that_fails_gates_once_and_one_that_did_not_run_is_named() -> None:
    covers = {_WIDGET: DocCover(stand_in=_S1), _MAKE_0: DocCover(stand_in=_S2),
              _MAKE_1: DocCover(gap="x")}
    verdict = judge_against_reference(_ref(covers), frozenset(), _reading(frozenset({_S1})))
    assert not verdict.met and verdict.unexcused_failures == frozenset({_S1})
    assert _WIDGET not in verdict.advisory_unmet
    assert "did not run in this world" in verdict.advisory_gaps[_MAKE_0]


def test_the_world_union_names_the_world_of_each_gap() -> None:
    covers = {_WIDGET: DocCover(stand_in=_S1)}
    ref = ReferenceMeasurement(key="k", clusters={
        "base": ClusterReference(cluster="base", passed=frozenset({_WIDGET}),
                                 announced=1, reported=1, doc_covers=covers),
        "alt": ClusterReference(cluster="alt", passed=frozenset({_WIDGET}),
                                announced=1, reported=1, doc_covers=covers)})
    def world(test_file: str, test: str) -> str:
        return join_capture(
            f"running 1 test\ntest {test} ... ok\n\n"
            "test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out\n",
            f"     Running {test_file} (target/debug/deps/x)\n")
    # The stand-in runs in `base`; in `alt` it is gated out and another
    # test runs.
    capture = join_world_captures([
        ("base", world("tests/doc_lib_gen_1.rs", "doc_lib_example_1")),
        ("alt", world("tests/other.rs", "other"))])
    verdict = judge_every_world(ref, capture, "base", lambda _w: lambda _t: {})
    assert verdict is not None
    # Met through its stand-in in `base`, absent in `alt`: the union says
    # advisory, with the world that has the gap. NEGATIVE CONTROL: a plain
    # union put the same pair in both sets.
    assert verdict.advisory_unmet == frozenset({_WIDGET})
    assert verdict.doc_stand_ins == frozenset()
    assert verdict.advisory_gaps[_WIDGET].startswith("alt: ")


def test_the_store_round_trips_lines_and_covers() -> None:
    covers = {_WIDGET: DocCover(stand_in=_S1), _MAKE_0: DocCover(gap="g")}
    c = ClusterReference(cluster="base", passed=frozenset({_WIDGET}), announced=1,
                         reported=1, doc_lines={_WIDGET: ("src/lib.rs", 12)},
                         doc_covers=covers)
    m = ReferenceMeasurement(key="k", clusters={"base": c})
    back = _from_json(json.loads(json.dumps(_to_json(m))))
    assert back is not None
    assert back.clusters["base"].doc_lines == {_WIDGET: ("src/lib.rs", 12)}
    assert back.clusters["base"].doc_covers == covers


_BASE = SimpleNamespace(name="base", is_default=True)


def test_the_emitter_records_every_fence_and_the_file_it_became(tmp_path: Path) -> None:
    """Kept, dropped and not re-hostable fences each get an origin; a kept
    one carries the hash of the file as the settle left it."""
    doc = (" ```\n let kept = 1;\n ```\n ```compile_fail\n let x: u8 = \"s\";\n ```\n"
           " ```\n let dropped = 2;\n ```\n")
    records = [DocItemRecord(file="src/lib.rs", line=7, doc=doc),
               DocItemRecord(file="src/lib.rs", line=40, doc=" ```\n let m = 3;\n ```\n",
                             in_macro_body=True)]

    def settle(_p: Path, _m: str, f: Path, block: str, _c: object) -> str | None:
        if "dropped" in block:
            return None
        f.write_text(block)
        return ""

    log: list[str] = []
    emit_doc_example_tests(tmp_path, "gadgets", [_BASE], DocExampleDeps(
        enumerate_doc_items=lambda _p: records, world_gate=lambda _c: "",
        settle=settle, measurable=lambda _c: True, log=log.append))
    raw = json.loads((tmp_path / ORIGINS_STORE).read_text())
    origins = origins_from_json(raw)
    assert origins is not None
    got = [(o.item_line, o.index, o.offset, o.in_macro_body, o.kept_as, o.reason[:12])
           for o in origins]
    assert got == [(7, 0, 0, False, "doc_lib_gen_1.rs", ""),
                   (7, 1, 3, False, "", "not re-hosta"),
                   (7, 2, 6, False, "", "dropped at e"),
                   (40, 0, 0, True, "doc_lib_gen_2.rs", "")]
    kept = tmp_path / "tests" / "doc_lib_gen_1.rs"
    assert origins[0].sha == rendered_sha(kept.read_text())
    assert "4 origin(s) recorded" in log[-1] and "1 written in a macro body" in log[-1]


def test_a_compile_only_doc_test_is_a_doc_test_with_its_line() -> None:
    """rustdoc names a `no_run` example `… (line 8) - compile` and a
    `compile_fail` one `… (line 11) - compile fail`. NEGATIVE CONTROL: the
    parser that wanted the name to end in `)` read neither."""
    from pipeline_core.domain.reference_floor import doc_test_head, doc_test_line
    assert doc_test_line("src/lib.rs - a (line 8) - compile") == 8
    assert doc_test_line("src/lib.rs - a (line 11) - compile fail") == 11
    assert doc_test_head("src/lib.rs - a (line 11) - compile fail") == "src/lib.rs - a"
    assert doc_test_line("src/lib.rs - a - compile") is None


def test_doc_test_ordinals_count_every_verdict_of_the_binary() -> None:
    """One item, a passing example at line 2 and an ignored one at line 5:
    two keys. NEGATIVE CONTROL: ordinals counted per verdict gave both
    `#0`, so one key named two tests and a reading lost one of them."""
    from pipeline_core.domain.reference_floor import binary_pairs
    b = SimpleNamespace(label="Doc-tests gadgets",
                        passed=frozenset({"src/lib.rs - a (line 2)",
                                          "src/lib.rs - a (line 8) - compile"}),
                        failed=frozenset(),
                        ignored=frozenset({"src/lib.rs - a (line 5)"}))
    pairs = {raw: pair[1] for _v, raw, pair in binary_pairs(b)}
    assert pairs == {"src/lib.rs - a (line 2)": "src/lib.rs - a#0",
                     "src/lib.rs - a (line 5)": "src/lib.rs - a#1",
                     "src/lib.rs - a (line 8) - compile": "src/lib.rs - a#2"}


def test_one_macro_example_and_one_free_line_join_at_that_shift() -> None:
    """The rule is one shift that places every fence of the record; with
    one fence and one free line that shift is unique, and the join takes it."""
    joined = join_origins({_MAC_0: ("src/lib.rs", 106)}, [_o(40, 16, macro=True)])
    assert joined[_MAC_0].offset == 16


def test_each_unjoined_doc_test_says_which_absence_it_is() -> None:
    lines = {_WIDGET: ("src/lib.rs", 12), _MAC_0: ("src/lib.rs", 106),
             _MAC_1: ("src/lib.rs", 117), (_D, "src/x.rs - y#0"): ("src/x.rs", 3)}
    assert doc_covers(lines, [], {})[_WIDGET].gap == "no doc-example origins recorded"
    covers = doc_covers(lines, [_o(40, 16, macro=True)], {})
    assert "could not be placed by one shift" in covers[_MAC_0].gap
    assert covers[(_D, "src/x.rs - y#0")].gap == "no origin recorded for src/x.rs"
    plain = doc_covers({_WIDGET: ("src/lib.rs", 12)}, [_o(10, 9)], {})
    assert plain[_WIDGET].gap == "no re-hosted example matches its line"


def _deps_for(records: list[DocItemRecord] | None, settle: object) -> DocExampleDeps:
    return DocExampleDeps(enumerate_doc_items=lambda _p: records,
                          world_gate=lambda _c: "", settle=settle,  # type: ignore[arg-type]
                          measurable=lambda _c: True, log=lambda _m: None)


def _keep(_p: Path, _m: str, f: Path, block: str, _c: object) -> str | None:
    f.write_text(block)
    return ""


_ONE = [DocItemRecord(file="src/lib.rs", line=7, doc=" ```\n let a = 1;\n ```\n"),
        DocItemRecord(file="src/lib.rs", line=20, doc=" ```\n let b = 2;\n ```\n")]


def test_a_re_entry_with_no_records_leaves_the_files_and_their_store(tmp_path: Path) -> None:
    """NEGATIVE CONTROL: the store used to be removed before the empty
    return, so a failed extractor read left the files with no origins and
    the next floor reported every doc test absent."""
    emit_doc_example_tests(tmp_path, "gadgets", [_BASE], _deps_for(_ONE, _keep))
    before = (tmp_path / ORIGINS_STORE).read_text()
    emit_doc_example_tests(tmp_path, "gadgets", [_BASE], _deps_for(None, _keep))
    assert (tmp_path / ORIGINS_STORE).read_text() == before
    assert sorted(p.name for p in (tmp_path / "tests").iterdir()) == [
        "doc_lib_gen_1.rs", "doc_lib_gen_2.rs"]


def test_a_pass_that_raises_leaves_a_store_naming_exactly_the_files_on_disk(
        tmp_path: Path) -> None:
    def settle(p: Path, m: str, f: Path, block: str, c: object) -> str | None:
        if "let b" in block:
            f.write_text(block)
            raise KeyboardInterrupt  # not an Exception: the settle's own guard lets it through
        return _keep(p, m, f, block, c)

    with pytest.raises(KeyboardInterrupt):
        emit_doc_example_tests(tmp_path, "gadgets", [_BASE], _deps_for(_ONE, settle))
    origins = origins_from_json(json.loads((tmp_path / ORIGINS_STORE).read_text()))
    assert origins is not None
    assert [o.kept_as for o in origins] == ["doc_lib_gen_1.rs"]
    assert sorted(p.name for p in (tmp_path / "tests").iterdir()) == ["doc_lib_gen_1.rs"]
