"""The function-execution criterion's pure half (`domain.fn_execution`).

Filed 2026-09-17 from run `301146-2d8a5f06`: augmentation stopped on a line
percentage, and bodies no test entered survived every fill gate to exit 1 at
the end-of-run stub gate. Every fixture here is an invented crate shape; the
assertions are the entry's claims — a function leaves the unexecuted set only
on a measurement (count above 0, or the attempt budget spent with the count
still 0), a reading with no record for a body is unmeasured and never read as
executed, and a stub is excused only when every record that speaks about it
measured it unreached.
"""
from __future__ import annotations

from dataclasses import replace

from pipeline_core.domain.cfg import CfgSet
from pipeline_core.domain.fn_execution import (
    FN_EXECUTION_CRITERION,
    CandidateFn,
    FnCoverageRecord,
    FnTarget,
    FunctionExecution,
    PendingFn,
    SiteIndex,
    UnreachedFn,
    advance_record,
    candidate_records,
    classify_stub_sites,
    executed_by_key,
    flipped_cfg_set,
    function_execution,
    is_test_only,
    mod_chain_active,
    note_attempt,
)
from pipeline_core.domain.models import FnRecord, FnSite, ModRecord

_LIB = """\
pub fn shared() -> u32 {
    let v = [1u32, 2];
    v.iter().map(|x| x + 1).sum()
}

#[cfg(gizmo)]
pub fn gated() -> u32 {
    3
}

#[cfg(not(gizmo))]
pub fn stock_only() -> u32 {
    4
}

pub fn hollow() -> u32 {
    unimplemented!()
}

#[cfg(test)]
fn helper() -> u32 {
    5
}
"""

_EXTRA = """\
pub fn extra() -> u32 {
    6
}
"""


def _rec(text: str, file: str, name: str, *, preds: tuple[str, ...] = (),
         is_test: bool = False, container: str = "") -> FnRecord:
    start = text.index(f"fn {name}(")
    open_brace = text.index("{", start)
    depth, i = 0, open_brace
    while True:
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                break
        i += 1
    return FnRecord(file=file, fn_name=name, sig_byte_start=start,
                    body_byte_start=open_brace + 1, body_byte_end=i,
                    body_text=text[open_brace + 1:i], cfg_predicates=preds,
                    is_test=is_test, container=container)


def _records() -> list[FnRecord]:
    return [
        _rec(_LIB, "src/lib.rs", "shared"),
        _rec(_LIB, "src/lib.rs", "gated", preds=("gizmo",)),
        _rec(_LIB, "src/lib.rs", "stock_only", preds=("not(gizmo)",)),
        _rec(_LIB, "src/lib.rs", "hollow"),
        _rec(_LIB, "src/lib.rs", "helper", preds=("test",)),
        _rec(_EXTRA, "src/extra.rs", "extra"),
        _rec(_EXTRA, "tests/t.rs", "extra", is_test=True),
    ]


_TEXTS = {"src/lib.rs": _LIB, "src/extra.rs": _EXTRA, "tests/t.rs": _EXTRA}
_MODS = [ModRecord(name="extra", file="extra.rs", cfg_predicates=("gizmo",),
                   declared_in="lib.rs")]


def _hollow(rec: FnRecord) -> bool:
    return "unimplemented!()" in rec.body_text


def _candidates(cfg_true: list[str], cfg_false: list[str]) -> list[CandidateFn]:
    return candidate_records(
        _records(), cs=CfgSet.of(cfg_true, cfg_false),
        flipped=flipped_cfg_set(cfg_true, cfg_false), mod_records=_MODS,
        file_texts=_TEXTS, hollow=_hollow)


# --- the join rule ----------------------------------------------------------


def test_a_key_is_unexecuted_only_when_every_body_under_it_counted_zero() -> None:
    # One fn per binary (lib copy at 0, test binary at the real count) and two
    # generic instantiations: any body that ran makes the key executed.
    got = executed_by_key([("f", 0), ("f", 3), ("g", 0), ("g", 0), ("h", 1)])
    assert got == {"f": True, "g": False, "h": True}
    assert "absent" not in got


def test_a_closure_record_resolves_to_the_fn_around_it() -> None:
    recs = _records()
    index = SiteIndex(recs, _TEXTS)
    closure_line = _LIB.count("\n", 0, _LIB.index("|x|")) + 1
    assert index.site_at("src/lib.rs", closure_line) == FnSite("src/lib.rs", "shared", "")
    assert index.site_at("src/lib.rs", 999) is None


# --- the candidate set --------------------------------------------------------


def test_the_candidates_are_the_compiled_non_test_non_hollow_src_fns() -> None:
    stock = {c.site.fn_name for c in _candidates([], ["gizmo"])}
    gizmo = {c.site.fn_name for c in _candidates(["gizmo"], [])}
    # Test fns, cfg(test) helpers, hollow bodies and test-dir records never.
    assert stock == {"shared", "stock_only"}
    # Under `gizmo` the gated fn and the gated FILE (mod chain) join; the
    # `not(gizmo)` arm leaves.
    assert gizmo == {"shared", "gated", "extra"}


def test_cluster_gated_is_what_the_clusters_cfg_identity_compiles() -> None:
    gizmo = {c.site.fn_name: c.cluster_gated for c in _candidates(["gizmo"], [])}
    stock = {c.site.fn_name: c.cluster_gated for c in _candidates([], ["gizmo"])}
    assert gizmo == {"shared": False, "gated": True, "extra": True}
    assert stock == {"shared": False, "stock_only": True}


def test_test_only_and_mod_chain_helpers() -> None:
    assert is_test_only(["test"]) and is_test_only(["all(test, feature = \"x\")"])
    assert not is_test_only(["not(test)"]) and not is_test_only(["gizmo"])
    assert mod_chain_active("extra.rs", _MODS, CfgSet.of(["gizmo"], []))
    assert not mod_chain_active("extra.rs", _MODS, CfgSet.of([], ["gizmo"]))
    assert mod_chain_active("lib.rs", _MODS, CfgSet.of([], ["gizmo"]))


# --- the reading --------------------------------------------------------------


def _line(text: str, needle: str) -> int:
    return text.count("\n", 0, text.index(needle)) + 1


def test_the_reading_splits_executed_unexecuted_and_unmeasured() -> None:
    cands = _candidates(["gizmo"], [])
    counts = {
        ("src/lib.rs", _line(_LIB, "fn shared")): 0,          # lib copy
        ("src/lib.rs", _line(_LIB, "|x|")): 4,                 # its closure ran
        ("src/lib.rs", _line(_LIB, "fn gated")): 0,
        # `extra` has no record at all: the reading cannot see it.
    }
    reading = function_execution(cands, SiteIndex(_records(), _TEXTS), counts, _TEXTS)
    assert reading.executed == {FnSite("src/lib.rs", "shared", "")}
    assert [t.site.fn_name for t in reading.unexecuted] == ["gated"]
    assert reading.unexecuted[0].cluster_gated is True
    assert reading.unexecuted[0].first_line == _line(_LIB, "fn gated")
    assert reading.unmeasured == (FnSite("src/extra.rs", "extra", ""),)
    assert reading.candidates == 3


# --- the stop rule --------------------------------------------------------------


_G = FnTarget(FnSite("src/lib.rs", "gated", ""), 7, 9, True)
_S = FnTarget(FnSite("src/lib.rs", "shared", ""), 1, 4, False)


def test_attempts_below_the_budget_keep_the_function_pending_and_the_stop_open() -> None:
    rec = advance_record(None, FunctionExecution(unexecuted=(_G,)), attempt_budget=3)
    assert not rec.accounted and rec.targets() == (_G,)
    for attempt in (1, 2):
        rec = note_attempt(rec, [_G.site], attempt)
        rec = advance_record(rec, FunctionExecution(unexecuted=(_G,)), attempt_budget=3)
        assert not rec.accounted
    rec = note_attempt(rec, [_G.site], 1)          # a later pass restarts the numbering
    assert rec.pending[0].attempts == (1, 2, 1)
    rec = advance_record(rec, FunctionExecution(unexecuted=(_G,)), attempt_budget=3)
    assert rec.accounted
    assert rec.unreached == (UnreachedFn(_G.site, (1, 2, 1), True),)


def test_a_count_above_zero_removes_the_function_whatever_it_was() -> None:
    rec = FnCoverageRecord(measured=True, unreached=(UnreachedFn(_G.site, (1, 2, 3)),),
                           pending=(PendingFn(_S, (1,)),))
    rec = advance_record(rec, FunctionExecution(executed=frozenset({_G.site, _S.site})),
                         attempt_budget=3)
    assert rec.accounted and rec.unreached == () and rec.pending == ()
    assert set(rec.executed) == {_G.site, _S.site}


def test_an_attempt_charges_only_the_pending_functions_it_was_generated_for() -> None:
    rec = FnCoverageRecord(measured=True, pending=(PendingFn(_G), PendingFn(_S)))
    rec = note_attempt(rec, [_S.site, FnSite("src/other.rs", "x", "")], 4)
    assert [p.attempts for p in rec.pending] == [(), (4,)]


def test_targets_put_cluster_gated_functions_first() -> None:
    rec = FnCoverageRecord(measured=True, pending=(PendingFn(_S), PendingFn(_G)))
    assert rec.targets() == (_G, _S)


def test_the_record_roundtrips_and_another_criterion_reads_as_none() -> None:
    rec = FnCoverageRecord(
        measured=True, candidates=5, executed=(FnSite("src/a.rs", "f", "impl A"),),
        pending=(PendingFn(_S, (2,)),), unreached=(UnreachedFn(_G.site, (1, 2, 3), True),),
        unmeasured=(FnSite("src/b.rs", "m", ""),))
    assert FnCoverageRecord.from_json(rec.to_json()) == rec
    assert rec.criterion == FN_EXECUTION_CRITERION
    assert FnCoverageRecord.from_json('{"criterion": "fn-execution/0"}') is None
    assert FnCoverageRecord.from_json("not json") is None


# --- the gates' reading of a stub ---------------------------------------------------


_P1 = FnSite("src/w.rs", "provide", "impl Error for Boxed")
_P2 = FnSite("src/w.rs", "provide", "impl Error for Plain")


def test_a_stub_is_unreached_only_when_every_record_speaking_about_it_says_so() -> None:
    unreached = FnCoverageRecord(measured=True, unreached=(UnreachedFn(_P1, (1, 2, 3)),))
    executed_elsewhere = FnCoverageRecord(measured=True, executed=(_P1,))
    pending_elsewhere = FnCoverageRecord(measured=True, pending=(PendingFn(
        FnTarget(_P1, 3, 5, False), (1,)),))
    silent = FnCoverageRecord(measured=True, executed=(FnSite("src/x.rs", "y", ""),))
    unmeasured = replace(unreached, measured=False)

    assert [s for s, _ in classify_stub_sites([_P1], [unreached, silent]).unreached] == [_P1]
    assert classify_stub_sites([_P1], [unreached, executed_elsewhere]).executed == (_P1,)
    assert classify_stub_sites([_P1], [unreached, pending_elsewhere]).unaccounted == (_P1,)
    assert classify_stub_sites([_P1], [unmeasured]).unaccounted == (_P1,)
    assert classify_stub_sites([_P1], []).unaccounted == (_P1,)


def test_a_stub_is_matched_by_exact_site_never_by_locator() -> None:
    # Refuted fallback: the locator's other sites decided a stub's status, and
    # a probe-only trait-impl `provide` read "executed" because the free
    # `provide` in the same file ran (run 301146-2d8a5f06's merged tree).
    free_ran = FnCoverageRecord(measured=True, executed=(FnSite("src/w.rs", "provide", ""),))
    probe_impl = FnSite("src/w.rs", "provide", "impl Error for Probe")
    acct = classify_stub_sites([probe_impl], [free_ran])
    assert acct.executed == () and acct.unaccounted == (probe_impl,)
    # Token spacing in a container is the same site (canonical form).
    spaced = FnSite("src/w.rs", "provide", "impl  Error  for  Boxed")
    one = FnCoverageRecord(measured=True, unreached=(UnreachedFn(_P1, (1, 2, 3)),))
    assert [s for s, _ in classify_stub_sites([spaced], [one]).unreached] == [spaced]
    # A renamed container is not accounted — it can keep a run failing, never excuse.
    renamed = FnSite("src/w.rs", "provide", "impl StdError for Boxed")
    assert classify_stub_sites([renamed], [one]).unaccounted == (renamed,)
