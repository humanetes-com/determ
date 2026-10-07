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

import json
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
    aimed_fn_outcomes,
    candidate_records,
    classify_stub_sites,
    executed_by_key,
    flipped_cfg_set,
    function_execution,
    inherited_attempts,
    is_test_only,
    mod_chain_active,
    note_attempt,
    note_verdicts,
    unambiguous_verdicts,
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
    assert rec.unreached == (UnreachedFn(_G.site, (1, 2, 1), True, ('committed', 'committed', 'committed')),)


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


def test_a_block_that_did_not_compile_spends_the_budget_and_says_why() -> None:
    """The nightly `Mut::clone` shape: three passes aimed, the blocks failed
    to compile. The account reads the aim, so the function is unreached after
    the third, with each pass's reason kept."""
    rec = advance_record(None, FunctionExecution(unexecuted=(_G,)), attempt_budget=3)
    for attempt in (1, 2, 3):
        rec = note_attempt(rec, [_G.site], attempt, outcomes={_G.site: "compile_failed"})
        rec = advance_record(rec, FunctionExecution(unexecuted=(_G,)), attempt_budget=3)
    assert rec.accounted
    assert rec.unreached == (
        UnreachedFn(_G.site, (1, 2, 3), True, ("compile_failed",) * 3),)


def test_aimed_outcomes_name_the_function_a_block_was_written_for() -> None:
    both = aimed_fn_outcomes([_G, _S], aimed_lines={8}, ran_lines=set(), otherwise="compile_failed")
    assert both == {_G.site: "compile_failed"}                    # _S (1-4) was not in a prompt
    ran = aimed_fn_outcomes([_G, _S], aimed_lines={2, 8}, ran_lines={8}, otherwise="compile_failed")
    assert ran == {_S.site: "compile_failed", _G.site: "committed"}
    assert aimed_fn_outcomes([_G], aimed_lines=set(), ran_lines=set(), otherwise="x") == {}


def test_attempts_another_world_spent_shorten_a_shared_budget_but_never_replace_one() -> None:
    other = FnCoverageRecord(measured=True, unreached=(UnreachedFn(_S.site, (1, 2, 3)),))
    inherited = inherited_attempts([other])
    assert inherited == {_S.site: 3}
    reading = FunctionExecution(unexecuted=(_S,))
    first = advance_record(None, reading, attempt_budget=3, inherited=inherited)
    assert first.accounted is False                                 # one own attempt first
    spent = note_attempt(first, [_S.site], 1)
    assert advance_record(spent, reading, attempt_budget=3, inherited=inherited).unreached
    # Negative control: no inherited attempts, the same single attempt leaves it pending.
    assert not advance_record(spent, reading, attempt_budget=3).unreached
    # A body only this world compiles is never shortened by another world's record.
    gated = advance_record(note_attempt(advance_record(None, FunctionExecution(unexecuted=(_G,)),
                                                      attempt_budget=3), [_G.site], 1),
                           FunctionExecution(unexecuted=(_G,)), attempt_budget=3,
                           inherited={_G.site: 3})
    assert gated.pending and not gated.unreached


def test_inherited_attempts_skip_what_the_other_world_owns_and_an_unmeasured_record() -> None:
    other = FnCoverageRecord(measured=True, pending=(PendingFn(_G, (1, 2)), PendingFn(_S, (1,))))
    assert inherited_attempts([other]) == {_S.site: 1}
    assert inherited_attempts([FnCoverageRecord(measured=False)]) == {}


def test_a_retrospective_verdict_attributes_only_to_a_name_that_is_one_site() -> None:
    own = FnTarget(FnSite("src/p.rs", "clone", "impl Clone for Own<T>"), 1, 3, False)
    ref = FnTarget(FnSite("src/p.rs", "clone", "impl Clone for Ref<T>"), 5, 7, False)
    uniq = FnTarget(FnSite("src/p.rs", "provide", ""), 9, 11, False)
    cats = {"NEEDS_INTERNALS": ["clone"], "TESTABLE": ["provide"], "UNREACHABLE": []}
    assert unambiguous_verdicts([own, ref, uniq], cats) == {uniq.site: "TESTABLE"}
    both = {"NEEDS_INTERNALS": ["provide"], "TESTABLE": ["provide"]}
    assert unambiguous_verdicts([uniq], both) == {}


def test_a_stored_ran_outcome_reads_as_committed() -> None:
    """A committed block was aimed at the function: the old word `ran` read as
    if the function executed, which only the next count says."""
    rec = FnCoverageRecord(measured=True, pending=(PendingFn(_S, (1,), ("committed",)),))
    legacy = rec.to_json().replace('"committed"', '"ran"')
    assert FnCoverageRecord.from_json(legacy) == rec


def test_a_verdict_is_evidence_on_the_record_and_changes_no_budget() -> None:
    rec = FnCoverageRecord(measured=True, pending=(PendingFn(_S, (1,), ("committed",)),))
    said = note_verdicts(rec, {_S.site: "NEEDS_INTERNALS"})
    assert said.pending[0].verdict == "NEEDS_INTERNALS"
    assert said.pending[0].attempts == (1,) and said.accounted is False
    again = advance_record(said, FunctionExecution(unexecuted=(_S,)), attempt_budget=3)
    assert again.pending[0].verdict == "NEEDS_INTERNALS"
    assert FnCoverageRecord.from_json(said.to_json()) == said



# --- unreachable by construction (domain.fn_reachability feeds these) --------

_PS = _S.site


def _reading_with(*targets: FnTarget, executed: tuple[FnSite, ...] = ()) -> FunctionExecution:
    return FunctionExecution(executed=frozenset(executed), unexecuted=tuple(targets))

def test_a_proof_moves_an_unexecuted_function_out_of_what_the_tests_are_asked() -> None:
    from pipeline_core.domain.fn_execution import apply_proofs
    other = FnTarget(FnSite("src/e.rs", "other", ""), 9, 11, False)
    out = apply_proofs(_reading_with(_S, other), {_PS: "private and never used"})
    assert [t.site for t in out.unexecuted] == [other.site]
    assert out.by_construction == ((_PS, "private and never used"),)
    assert out.candidates == 2 and out.contradicted == ()


def test_a_proved_function_a_test_executed_is_a_counterexample_and_stays_executed() -> None:
    from pipeline_core.domain.fn_execution import apply_proofs
    out = apply_proofs(_reading_with(executed=(_PS,)), {_PS: "wrongly proved"})
    assert out.contradicted == (_PS,) and out.by_construction == () and _PS in out.executed


def test_the_record_keeps_a_proved_function_without_spending_a_budget_on_it() -> None:
    from pipeline_core.domain.fn_execution import apply_proofs
    reading = apply_proofs(_reading_with(_S), {_PS: "private and never used"})
    rec = advance_record(None, reading, attempt_budget=3)
    assert rec.pending == () and rec.accounted
    assert [(p.site, p.proof) for p in rec.by_construction] == [(_PS, "private and never used")]
    assert "unreachable by construction 1" in rec.summary()
    assert FnCoverageRecord.from_json(rec.to_json()) == rec
    # A record written before this field existed still reads, with none.
    old = json.loads(rec.to_json())
    del old["by_construction"]
    assert FnCoverageRecord.from_json(json.dumps(old)).by_construction == ()


def test_the_end_gate_reads_a_proved_function_as_unreached_with_its_proof() -> None:
    rec = advance_record(None, FunctionExecution(
        by_construction=((_PS, "private and never used"),)), attempt_budget=3)
    assert rec.classify_stub(_PS) == "unreached" and rec.speaks_about(_PS)
    entry = rec.unreached_entry(_PS)
    assert entry is not None and entry.proof == "private and never used"
    assert entry.outcomes == ("by_construction",) and entry.attempts == ()
    # Negative control: a site the record does not list is not accounted.
    assert rec.classify_stub(FnSite("src/e.rs", "elsewhere", "")) is None


def test_another_worlds_proof_shortens_the_budget_but_not_the_first_attempt() -> None:
    """313's second gap: a site stable proved unreachable gives nightly the
    whole budget as spent — through the shared rule, so nightly still spends
    one attempt of its own. Controls: a gated proof gives nothing; no budget
    passed gives nothing (the pre-change call)."""
    from pipeline_core.domain.fn_execution import ProvenFn

    shared = ProvenFn(_S.site, "no caller in the public API", cluster_gated=False)
    gated = ProvenFn(_S.site, "cfg-only", cluster_gated=True)
    proved = FnCoverageRecord(measured=True, by_construction=(shared,))
    assert inherited_attempts([proved], budget=3) == {_S.site: 3}
    assert inherited_attempts([proved]) == {}
    assert inherited_attempts(
        [FnCoverageRecord(measured=True, by_construction=(gated,))], budget=3) == {}
    round_trip = FnCoverageRecord.from_json(proved.to_json())
    assert round_trip is not None and round_trip.by_construction == (shared,)


def test_an_uncommitted_block_is_labelled_by_the_gate_that_refused_it() -> None:
    """313's first gap: an admission-rejected block (it compiled, its own
    tests failed) read `compile_failed`. The label is now the gate's."""
    from pipeline_core.domain.fn_execution import (
        COMPILE_FAILED,
        CONTRACT,
        NO_WORLD,
        TESTS_FAILED,
        block_failure,
    )

    assert block_failure(runtime=True, contract=False, sigs=frozenset({"t_x"})) == TESTS_FAILED
    assert block_failure(runtime=False, contract=True, sigs=frozenset()) == CONTRACT
    assert block_failure(runtime=True, contract=False,
                         sigs=frozenset({"no_world"})) == NO_WORLD
    assert block_failure(runtime=False, contract=False,
                         sigs=frozenset({"E0277"})) == COMPILE_FAILED
    t = _S
    got = aimed_fn_outcomes([t], {t.first_line}, set(), COMPILE_FAILED,
                            blocks=[(frozenset({t.first_line}), TESTS_FAILED)])
    assert got == {t.site: TESTS_FAILED}
    # Control: no block recorded — the caller's default, as before.
    assert aimed_fn_outcomes([t], {t.first_line}, set(), COMPILE_FAILED) == {
        t.site: COMPILE_FAILED}


def test_committed_files_survive_a_relaunch_on_the_record() -> None:
    """The review remainder: `committed_for` lived only in the process, so a
    resumed one had the outcome on record and no file names. The record now
    keeps them on the pending entry, merged across readings."""
    from pipeline_core.domain.fn_execution import FunctionExecution, advance_record

    reading = FunctionExecution(unexecuted=(_S,))
    first = advance_record(None, reading, attempt_budget=3,
                           committed={_S.label: ["tests/a_gen_1.rs"]})
    assert first.pending[0].committed_in == ("tests/a_gen_1.rs",)
    again = FnCoverageRecord.from_json(first.to_json())
    assert again is not None and again.pending[0].committed_in == ("tests/a_gen_1.rs",)
    second = advance_record(again, reading, attempt_budget=3,
                            committed={_S.label: ["tests/a_gen_2.rs"]})
    assert second.pending[0].committed_in == ("tests/a_gen_1.rs", "tests/a_gen_2.rs")
    # Control: nothing committed, nothing recorded.
    assert advance_record(None, reading, attempt_budget=3).pending[0].committed_in == ()
