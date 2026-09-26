"""A per-fn reply may widen its write to other functions of the same file,
and the unit writes, judges and reverts that set as one.

The class, from run `116341-24c76a97` (2026-09-25, cluster `stable`): a
stub 45 of 62 failing tests stopped in was dispatched per-fn in six fills
and reverted every time on the same compile error. The model's reasoning
derived a body that needed two table functions of the same file rewritten
with it, and the per-fn output contract let it write one body only, so it
answered with the call it had shown cannot compile. The file run that held
the stub with those functions was then carved into two pieces by the
per-unit ranking, and neither piece could write the pair.

The plan's sentences, each with its negative control:
- an extra `<FN>` block naming another function of the target's file is
  admitted and written with the target, kept together
  (control: `joint_max=0`, the single-body contract, writes only the target);
- a failed attempt restores the extras with the target;
- a named set is never carved apart by the schedule
  (control: the same ledger without the named set carves it).
Invented shape; no crate names.
"""
from __future__ import annotations

import threading
from dataclasses import replace
from pathlib import Path
from typing import Any

from pipeline_core.application.reconstruct.context import PerFnPassCtx
from pipeline_core.domain.fill_schedule import (
    RouteKind,
    RouteLedgerRow,
    RouteRun,
    RunOutcome,
    UnitEvidence,
    schedule,
)
from pipeline_core.domain.joint_write import admit_joint
from pipeline_core.domain.models import FnLocator, Stub
from pipeline_core.domain.reconstruction import FnLocation, SpliceOutcome
from tests.test_fill_engine_usecases import FakeFill, _ctx, _fill_one, _mk_ws

_RECS = [
    {"file": "src/a.rs", "fn_name": "build", "container": "impl Gauge",
     "sig_text": "pub fn build(v: u8) -> Gauge", "cfg_predicates": []},
    {"file": "src/a.rs", "fn_name": "triple", "container": "",
     "sig_text": "fn triple(x: u8) -> u8", "cfg_predicates": []},
    {"file": "src/a.rs", "fn_name": "fmt", "container": "impl Debug for Gauge",
     "sig_text": "fn fmt(&self, f: &mut Formatter) -> Result", "cfg_predicates": []},
    {"file": "src/a.rs", "fn_name": "fmt", "container": "impl Display for Gauge",
     "sig_text": "fn fmt(&self, f: &mut Formatter) -> Result", "cfg_predicates": []},
    {"file": "src/b.rs", "fn_name": "other", "container": "",
     "sig_text": "fn other()", "cfg_predicates": []},
    {"file": "src/a.rs", "fn_name": "gated", "container": "",
     "sig_text": "fn gated()", "cfg_predicates": ["nightly"]},
]


def _block(name: str, block: str = "", body: str = "x") -> dict[str, Any]:
    return {"fn_name": name, "block": block, "body": body}


def _admit(*blocks: dict[str, Any], cap: int = 4):
    return admit_joint(list(blocks), target_file="src/a.rs",
                       target_name="build", target_container="impl Gauge",
                       records=_RECS, cfg_dead={("src/a.rs", "gated")},
                       cap=cap)


# ---- who may be written ----


def test_a_function_of_the_same_file_is_admitted() -> None:
    adm = _admit(_block("triple", "free fns", "x * 3"))
    assert [u.fn_name for u in adm.admitted] == ["triple"]
    assert adm.admitted[0].body == "x * 3" and not adm.refused


def test_two_homonyms_need_the_block_and_the_block_picks_one() -> None:
    assert not _admit(_block("fmt")).admitted
    adm = _admit(_block("fmt", "impl Display for Gauge"))
    assert [u.container for u in adm.admitted] == ["impl Display for Gauge"]


def test_the_target_another_file_a_compiled_out_fn_and_the_cap_are_refused() -> None:
    adm = _admit(_block("build", "impl Gauge"), _block("other"),
                 _block("gated"), _block("triple"),
                 _block("fmt", "impl Debug for Gauge"), cap=1)
    assert [u.fn_name for u in adm.admitted] == ["triple"]
    why = dict(adm.refused)
    assert "target" in why["build [impl Gauge]"]
    assert "no function of that name" in why["other"]
    assert "compiled out" in why["gated"]
    assert "cap" in why["fmt [impl Debug for Gauge]"]
    assert "`other`" in adm.refusal_note()


def test_a_unit_named_the_way_the_pipeline_labels_it_is_admitted() -> None:
    """Run `38934-2999cc94` (2026-09-26): the reply wrote `name="fmt [impl Debug
    for MyError]"`, the label every prompt and log line uses for that unit, and
    the whole label was refused as `no function of that name`. Both spellings
    the pipeline prints (`name [container]`, `name (container)`) resolve."""
    for label in ("fmt [impl Display for Gauge]", "fmt (impl Display for Gauge)"):
        adm = _admit(_block(label))
        assert [(u.fn_name, u.container) for u in adm.admitted] == [
            ("fmt", "impl Display for Gauge")], label
        assert not adm.refused
    # A bare name that is a real fn keeps its reading; an explicit block wins
    # over a container spelled into the name; a label that names no impl of
    # the file is refused by the ordinary ambiguity rule, never guessed.
    assert [u.fn_name for u in _admit(_block("triple")).admitted] == ["triple"]
    won = _admit(_block("fmt [impl Debug for Gauge]", "impl Display for Gauge"))
    assert [u.container for u in won.admitted] == ["impl Display for Gauge"]
    unknown = _admit(_block("fmt [impl Debug for Nothing]"))
    assert not unknown.admitted and "ambiguous" in dict(unknown.refused)[
        "fmt [impl Debug for Nothing]"]


def test_negative_control_the_label_read_as_a_bare_name_is_refused(
        monkeypatch: Any) -> None:
    from pipeline_core.domain import joint_write
    monkeypatch.setattr(joint_write, "_split_label",
                        lambda name: (name.strip(), ""))
    adm = _admit(_block("fmt [impl Display for Gauge]"))
    assert not adm.admitted
    assert "no function of that name" in dict(adm.refused)[
        "fmt [impl Display for Gauge]"]


# ---- the unit writes, keeps and reverts the set as one ----


_REPLY = ("<BODY>\nGauge { f: triple, v }\n</BODY>\n"
          '<FN name="triple" block="free fns">\nx * 3\n</FN>')


class _Tracking(FakeFill):
    """Records which function each scoped splice / revert named."""

    def splice_body_scoped(self, *a: Any, **k: Any) -> SpliceOutcome:
        self.s.setdefault("spliced", []).append(a[1])
        return super().splice_body_scoped(*a, **k)

    def revert_body_scoped(self, *a: Any, **k: Any) -> SpliceOutcome:
        self.s.setdefault("reverted", []).append(a[1])
        return super().revert_body_scoped(*a, **k)


def _joint_ctx(ws: Path, joint_max: int) -> PerFnPassCtx:
    recs = [dict(r, file="lib.rs") for r in _RECS if r["file"] == "src/a.rs"]
    return replace(_ctx(ws), fn_records=tuple(recs), joint_max=joint_max,
                   splice_check_lock=threading.Lock())


def _run(tmp_path: Path, *, joint_max: int, run: tuple[int, str]) -> tuple[Any, _Tracking]:
    ws = _mk_ws(tmp_path)
    fake = _Tracking(
        loc_rec=FnLocation(0, 0, 5, "pub fn build(v: u8) -> Gauge", "unimplemented!()"),
        splice=SpliceOutcome(True, "ok", 4), run=run, u2=None,
        body=("Gauge { f: triple, v }", _REPLY))
    res = _fill_one(fake).run(
        1, Stub(file="lib.rs", fn_name="build", container="impl Gauge"),
        ctx=_joint_ctx(ws, joint_max))
    return res, fake


def test_the_extra_is_written_and_kept_with_the_target(tmp_path: Path) -> None:
    res, fake = _run(tmp_path, joint_max=4, run=(0, ""))
    assert res.filled
    assert res.joint == (FnLocator("lib.rs", "triple"),)
    assert res.joint_named == ((FnLocator("lib.rs", "build"),
                                FnLocator("lib.rs", "triple")),)
    assert fake.s["spliced"] == ["triple"]
    assert "phase_c.fn.joint_kept" in fake.events
    # The opening prompt offered the widening clause.
    first = fake.s["turns"][0][0]
    assert "OTHER functions of this same file" in first


def test_negative_control_the_single_body_contract_writes_only_the_target(
        tmp_path: Path) -> None:
    res, fake = _run(tmp_path, joint_max=0, run=(0, ""))
    assert res.filled and res.joint == () and res.joint_named == ()
    assert "spliced" not in fake.s
    assert "OTHER functions of this same file" not in fake.s["turns"][0][0]


def test_a_failed_joint_attempt_restores_the_extra_with_the_target(
        tmp_path: Path) -> None:
    res, fake = _run(tmp_path, joint_max=4,
                     run=(101, "error[E0277]: the trait bound is not satisfied"))
    assert not res.filled and res.joint == ()
    spliced, reverted = fake.s["spliced"], fake.s["reverted"]
    assert spliced.count("triple") >= 1
    # Every attempt that wrote the extra restored it.
    assert reverted.count("triple") == spliced.count("triple")
    # The named set is still recorded: it is the model's claim, not a keep.
    assert res.joint_named


# ---- the schedule keeps a named set whole ----


OR = "o1"
A, B, C = (FnLocator("src/x.rs", n) for n in ("a", "b", "c"))
_OTHER = RouteRun(kind=RouteKind.FILE, label="file src/q.rs",
                  targets=frozenset({FnLocator("src/q.rs", "e")}),
                  target_tests=frozenset({"t_q"}), bodies_sha="new")


def _file_run() -> RouteRun:
    targets = frozenset({A, B, C})
    return RouteRun(
        kind=RouteKind.FILE, label="file src/x.rs", targets=targets,
        target_tests=frozenset({"t"}), stubs_in_targets=targets,
        files=("src/x.rs",), bodies_sha="new",
        unit_tests=tuple((u, frozenset({"t"})) for u in (A, B, C)))


def _rows(named: bool) -> list[RouteLedgerRow]:
    # The file route's last try wrote `a` without `b` (the live shape: the
    # stub's file-route tries never held the table fns it installs).
    reverted = RouteLedgerRow(
        kind=RouteKind.FILE, label="file src/x.rs",
        targets=(("src/x.rs", "a"), ("src/x.rs", "c")),
        bodies_sha="old", oracle_fp=OR, outcome=RunOutcome.REVERTED,
        run_id="r1", iteration=1, started="2026-09-25T00:00:00",
        reverted=(("src/x.rs", "a"),))
    joint = RouteLedgerRow(
        kind=RouteKind.PER_FN, label="per-fn 1 stub(s)",
        targets=(("src/x.rs", "a"),), bodies_sha="p", oracle_fp=OR,
        outcome=RunOutcome.REVERTED, run_id="r1", iteration=1,
        started="2026-09-25T00:00:01",
        joint_sets=((("src/x.rs", "a"), ("src/x.rs", "b")),))
    return [reverted, joint] if named else [reverted]


def _opened(named: bool) -> set[FnLocator]:
    s = schedule([_file_run(), _OTHER], _rows(named), oracle_fp=OR, hollow={},
                 budget_minutes=100.0, unit_evidence=UnitEvidence())
    return {t for x in s.ordered for t in x.run.targets} - _OTHER.targets


def test_a_named_set_is_not_carved_apart() -> None:
    assert _opened(named=True) == {A, B, C}


def test_a_named_set_already_tried_together_is_still_a_re_buy() -> None:
    """The same named set, when the last file try wrote `a` WITH `b`: that
    write was tried, so `a` is the re-buy it was."""
    rows = _rows(named=True)
    rows[0] = replace(rows[0], targets=(("src/x.rs", "a"), ("src/x.rs", "b"),
                                        ("src/x.rs", "c")))
    s = schedule([_file_run(), _OTHER], rows, oracle_fp=OR, hollow={},
                 budget_minutes=100.0, unit_evidence=UnitEvidence())
    assert A not in {t for x in s.ordered for t in x.run.targets}


def test_negative_control_without_the_named_set_the_unit_is_carved_out() -> None:
    assert _opened(named=False) == {B, C}


def test_the_ledger_row_round_trips_its_named_sets() -> None:
    row = _rows(named=True)[1]
    back = RouteLedgerRow.from_dict(row.to_dict())
    assert back is not None and back.joint_sets == row.joint_sets
