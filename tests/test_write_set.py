"""The write-set model: one answer to "which other bodies may this unit write
in the same transaction", read by the per-fn route and the slice session.

Exposed by runs `38934-2999cc94` (2026-09-26) through `59479-cc3afffd`
(2026-09-27): a three-body recursion across `src/kind.rs` and
`src/error.rs` stayed on the tree while per-fn, file and impl runs each
wrote one member. The recorded fixture is the extractor's records of that
tree (`tests/fixtures/reference_graph/stable_2026-09-27.json`, body text
dropped). The plan's claims, checked here:

- the tree's call graph, resolving only what a spelling decides, holds
  exactly one cycle and it is the recursion (negative control: a name-only
  resolver joins 15 formatting bodies and 13 constructors into cycles);
- a per-fn unit on one member is offered the other two and may write them
  in their own files (negative control: without the coupling the same reply
  lands on the target file's same-named function, or is refused);
- the session's editable set gains the coupled members.

The invented shapes name no crate symbol: the same functions fire on them.
"""
from __future__ import annotations

import json
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from pipeline_core.application.reconstruct.session_write_set import session_write_set
from pipeline_core.domain import write_set
from pipeline_core.domain.attribution import record_site
from pipeline_core.domain.joint_write import admit_joint
from pipeline_core.domain.models import FnRecord, FnRef, FnSite, Stub
from pipeline_core.domain.reconstruction import FnLocation, SpliceOutcome
from pipeline_core.domain.reference_graph import ReferenceGraph, reference_graph
from pipeline_core.domain.slice_session import SessionSlice
from pipeline_core.domain.write_set import Coupling, coupled_units
from tests.test_fill_engine_usecases import FakeFill, _ctx, _fill_one, _mk_ws

_FIXTURE = Path(__file__).parent / "fixtures" / "reference_graph" / "stable_2026-09-27.json"

RING = frozenset({
    FnSite("src/error.rs", "construct_from_boxed", "impl Error"),
    FnSite("src/error.rs", "from_boxed", "impl Error"),
    FnSite("src/kind.rs", "new", "impl Boxed"),
})


@pytest.fixture(autouse=True)
def _no_observed_cycles() -> Any:
    write_set.bind_observed(())
    yield
    write_set.bind_observed(())


def _recorded() -> list[FnRecord]:
    data = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    return [FnRecord.from_dict(r) for r in data["records"]]


def _cycles(g: ReferenceGraph) -> set[frozenset[FnSite]]:
    return {g.cycle_of(s) for s in g.edges if g.cycle_of(s)}


# ---- the call graph ----


def test_the_recorded_tree_has_one_cycle_and_it_is_the_recursion() -> None:
    g = reference_graph(_recorded())
    assert _cycles(g) == {RING}


def test_negative_control_resolving_by_name_alone_joins_unrelated_bodies() -> None:
    recs = _recorded()
    by_name: dict[str, list[FnRecord]] = {}
    for r in recs:
        by_name.setdefault(r.fn_name, []).append(r)
    naive = ReferenceGraph({
        record_site(r): frozenset(record_site(h) for ref in r.refs
                                  for h in by_name.get(ref.name, ()))
        for r in recs})
    sizes = sorted(len(c) for c in _cycles(naive))
    assert sizes[-1] >= 13 and len(_cycles(naive)) > 1


def _rec(file: str, name: str, container: str, *refs: tuple[str, str, str],
         **kw: Any) -> FnRecord:
    return FnRecord(file=file, fn_name=name, container=container,
                    sig_text=f"fn {name}()", body_text="todo",
                    refs=tuple(FnRef(k, q, n) for k, q, n in refs), **kw)


_SHAPE = [
    # A cycle through two type-qualified calls in two files.
    _rec("src/p.rs", "make", "impl Pail", ("path", "Quill", "build")),
    _rec("src/q.rs", "build", "impl Quill", ("path", "Self", "shape"),
         ("call", "", "helper")),
    _rec("src/q.rs", "shape", "impl Quill", ("path", "Pail", "make")),
    _rec("src/q.rs", "helper", ""),
    # A homonym of the cycle member outside it.
    _rec("src/r.rs", "make", "impl Rope", ("path", "Rope", "other")),
    _rec("src/r.rs", "other", "impl Rope"),
    # Two formatting impls; the second delegates to a same-named method.
    _rec("src/r.rs", "render", "impl Show for Rope", ("method", "", "render")),
    _rec("src/p.rs", "render", "impl Show for Pail", ("method", "", "render")),
    # A method called on a receiver whose name has two bodies: no edge.
    _rec("src/p.rs", "tick", "impl Pail", ("method", "", "make")),
    # Test files are not nodes.
    _rec("tests/t.rs", "t", "", ("path", "Pail", "make"), is_test=True),
]


def test_a_cycle_is_the_spelled_edges_and_nothing_ambiguous_joins_it() -> None:
    g = reference_graph(_SHAPE)
    cyc = frozenset({FnSite("src/p.rs", "make", "impl Pail"),
                     FnSite("src/q.rs", "build", "impl Quill"),
                     FnSite("src/q.rs", "shape", "impl Quill")})
    assert _cycles(g) == {cyc}
    assert FnSite("src/q.rs", "helper", "") in g.callees(
        FnSite("src/q.rs", "build", "impl Quill"))
    assert not g.callees(FnSite("src/p.rs", "render", "impl Show for Pail"))
    assert not g.callees(FnSite("src/p.rs", "tick", "impl Pail"))
    assert g.unresolved >= 1


# ---- the coupling every route asks ----


def test_a_member_of_the_recorded_recursion_is_coupled_to_the_other_two() -> None:
    target = FnSite("src/error.rs", "construct_from_boxed", "impl Error")
    got = coupled_units((target,), _recorded(), cap=4)
    assert {c.site for c in got} == RING - {target}
    assert {c.coupling for c in got} == {Coupling.CALL_CYCLE}


def test_an_observed_cycle_couples_what_the_source_does_not_spell() -> None:
    a = FnSite("src/p.rs", "helper_a", "")
    b = FnSite("src/q.rs", "helper_b", "")
    recs = [_rec("src/p.rs", "helper_a", ""), _rec("src/q.rs", "helper_b", "")]
    assert coupled_units((a,), recs, cap=4) == ()
    write_set.bind_observed([{a, b}])
    got = coupled_units((a,), recs, cap=4)
    assert [(c.site, c.coupling) for c in got] == [(b, Coupling.OBSERVED_CYCLE)]


def test_the_cap_bounds_the_coupled_set() -> None:
    target = FnSite("src/error.rs", "from_boxed", "impl Error")
    assert len(coupled_units((target,), _recorded(), cap=1)) == 1
    assert coupled_units((target,), _recorded(), cap=0) == ()


# ---- per-fn admission beyond the target's file ----


def _dicts() -> list[dict[str, Any]]:
    return [r.to_dict() for r in _recorded()]


def _admit(*blocks: dict[str, Any], coupled: tuple[FnSite, ...]) -> Any:
    return admit_joint(list(blocks), target_file="src/error.rs",
                       target_name="construct_from_boxed",
                       target_container="impl Error", records=_dicts(),
                       coupled=coupled)


def _coupled_of_target() -> tuple[FnSite, ...]:
    target = FnSite("src/error.rs", "construct_from_boxed", "impl Error")
    return tuple(c.site for c in coupled_units((target,), _recorded(), cap=4))


def test_a_coupled_body_of_another_file_is_admitted_by_its_header() -> None:
    adm = _admit({"fn_name": "new", "block": "impl Boxed", "body": "x"},
                 coupled=_coupled_of_target())
    assert [(u.file, u.fn_name, u.container) for u in adm.admitted] == [
        ("src/kind.rs", "new", "impl Boxed")]


def test_a_coupled_body_is_admitted_by_its_file_spelling() -> None:
    adm = _admit({"fn_name": "src/kind.rs::new", "block": "", "body": "x"},
                 coupled=_coupled_of_target())
    assert [(u.file, u.fn_name) for u in adm.admitted] == [("src/kind.rs", "new")]


def test_negative_control_without_the_coupling_the_reply_lands_on_the_wrong_body() -> None:
    by_header = _admit({"fn_name": "new", "block": "impl Boxed", "body": "x"},
                       coupled=())
    # The target's file has one `new`: the old rule wrote the reply there.
    assert [(u.file, u.container) for u in by_header.admitted] == [
        ("src/error.rs", "impl Error")]
    by_file = _admit({"fn_name": "src/kind.rs::new", "block": "", "body": "x"},
                     coupled=())
    assert not by_file.admitted


def test_a_body_of_another_file_that_is_not_coupled_is_refused() -> None:
    adm = _admit({"fn_name": "src/chain.rs::next", "block": "", "body": "x"},
                 coupled=_coupled_of_target())
    assert not adm.admitted and adm.refused


# ---- the per-fn route writes each extra in its own file ----


_CYCLE_RECS = [
    {"file": "lib.rs", "fn_name": "build", "container": "impl Gauge",
     "sig_text": "pub fn build(v: u8) -> Gauge", "cfg_predicates": [],
     "refs": [{"kind": "path", "qualifier": "Wand", "name": "make"}]},
    {"file": "wand.rs", "fn_name": "make", "container": "impl Wand",
     "sig_text": "pub fn make(self) -> Gauge", "cfg_predicates": [],
     "body_text": "Gauge::build(1)",
     "refs": [{"kind": "path", "qualifier": "Gauge", "name": "build"}]},
]

_REPLY = ("<BODY>\nGauge { v }\n</BODY>\n"
          '<FN name="wand.rs::make" block="impl Wand">\nGauge { v: 1 }\n</FN>')


class _Paths(FakeFill):
    def splice_body_scoped(self, *a: Any, **k: Any) -> SpliceOutcome:
        self.s.setdefault("spliced_in", []).append((Path(a[0]).name, a[1]))
        return super().splice_body_scoped(*a, **k)


def _run_cycle(tmp_path: Path, recs: list[dict[str, Any]]) -> tuple[Any, _Paths]:
    ws = _mk_ws(tmp_path)
    fake = _Paths(
        loc_rec=FnLocation(0, 0, 5, "pub fn build(v: u8) -> Gauge", "unimplemented!()"),
        splice=SpliceOutcome(True, "ok", 4), run=(0, ""), u2=None,
        body=("Gauge { v }", _REPLY))
    ctx = replace(_ctx(ws), fn_records=tuple(recs), joint_max=4,
                  splice_check_lock=threading.Lock())
    res = _fill_one(fake).run(
        1, Stub(file="lib.rs", fn_name="build", container="impl Gauge"), ctx=ctx)
    return res, fake


def test_the_per_fn_route_offers_and_writes_the_coupled_body_in_its_file(
        tmp_path: Path) -> None:
    res, fake = _run_cycle(tmp_path, _CYCLE_RECS)
    assert res.filled
    assert fake.s["spliced_in"] == [("wand.rs", "make")]
    first = fake.s["turns"][0][0]
    assert "<COUPLED_BODIES>" in first and "wand.rs — impl Wand" in first


def test_negative_control_without_references_nothing_is_offered_or_written(
        tmp_path: Path) -> None:
    flat = [{k: v for k, v in r.items() if k != "refs"} for r in _CYCLE_RECS]
    res, fake = _run_cycle(tmp_path, flat)
    assert res.filled
    assert "spliced_in" not in fake.s
    assert "<COUPLED_BODIES>" not in fake.s["turns"][0][0]


# ---- the session's editable set ----


def test_the_session_editable_set_gains_the_coupled_members() -> None:
    recs = _recorded()
    member = FnSite("src/error.rs", "construct_from_boxed", "impl Error")
    by = {record_site(r): r for r in recs}
    ws = session_write_set(SessionSlice(("t",), (member,)), {member: by[member]},
                           recs, [], lambda f: None)
    assert set(ws.slice_.units) == RING
    assert set(ws.coupled) == RING - {member}
    # Negative control: a cap of zero is the pre-coupling write set.
    bare = session_write_set(SessionSlice(("t",), (member,)), {member: by[member]},
                             recs, [], lambda f: None, coupled_cap=0)
    assert bare.slice_.units == (member,)
