"""Starvation classifier (domain) + miner correlation (application)."""
from __future__ import annotations

from pipeline_core.application.starvation_report import (
    collect_exchanges,
    mine_run,
    render_report,
)
from pipeline_core.domain.starvation import (
    StarvationCause,
    classify_starvation,
    has_word,
)

_PROMPT = ("Fill fn object_ref in src/error.rs.\n"
           "TYPES_IN_SCOPE: struct Error { inner: Own<ErrorImpl> }\n"
           "fn vtable(...) -> &Vtable\n")


def test_has_word_boundaries() -> None:
    assert has_word("let x = vtable();", "vtable")
    assert not has_word("let x = my_vtable();", "vtable")
    assert not has_word("vtables", "vtable")
    assert not has_word("abc", "")


def test_symbol_absent_when_hallucinated_fn_never_in_prompt() -> None:
    err = "error[E0425]: cannot find function `ext_context` in this scope"
    cause, ev = classify_starvation(_PROMPT, "self.ext_context(msg)", err)
    assert cause is StarvationCause.SYMBOL_ABSENT and ev == "ext_context"


def test_type_def_absent_for_unresolved_type() -> None:
    err = "error[E0433]: failed to resolve: use of undeclared type `BoxedError`"
    cause, ev = classify_starvation(_PROMPT, "BoxedError::new(e)", err)
    assert cause is StarvationCause.TYPE_DEF_ABSENT and ev == "BoxedError"


def test_e0560_owner_named_first_does_not_swap_field_and_type() -> None:
    # E0560 names the STRUCT first ("struct `X` has no field named `Y`"), the
    # reverse of E0609. ErrorImpl is shown in the prompt, so inventing
    # `backtrace` on it is a design conflict — and the evidence must read
    # `backtrace` on `ErrorImpl`, not the swapped form.
    err = "error[E0560]: struct `ErrorImpl<E>` has no field named `backtrace`"
    cause, ev = classify_starvation(_PROMPT, "self.backtrace", err)
    assert cause is StarvationCause.DESIGN_CONFLICT
    assert ev == "invented field `backtrace` on `ErrorImpl`"


def test_e0609_field_named_first_still_reads_field_then_owner() -> None:
    # E0609 keeps field-first order; the E0560 fix must not regress it. Both
    # codes now normalise to the same correct (field, owner) evidence.
    err = "error[E0609]: no field `backtrace` on type `ErrorImpl<E>`"
    cause, ev = classify_starvation(_PROMPT, "self.backtrace", err)
    assert cause is StarvationCause.DESIGN_CONFLICT
    assert ev == "invented field `backtrace` on `ErrorImpl`"


def test_symbol_present_in_prompt_is_not_starvation() -> None:
    err = "error[E0425]: cannot find function `vtable` in this scope"
    cause, _ = classify_starvation(_PROMPT, "vtable()", err)
    assert cause is not StarvationCause.SYMBOL_ABSENT


def test_test_absent_when_failing_test_source_not_shown() -> None:
    err = ("running 2 tests\n"
           "test tests::test_downcast_ref ... FAILED\n"
           "failures:\n    tests::test_downcast_ref\n"
           "test result: FAILED. 1 passed; 1 failed\n")
    cause, ev = classify_starvation(_PROMPT, "", err)
    assert cause is StarvationCause.TEST_ABSENT and ev == "test_downcast_ref"


def test_test_shown_in_prompt_falls_through() -> None:
    prompt = _PROMPT + "\n#[test] fn test_downcast_ref() { ... }\n"
    err = "test tests::test_downcast_ref ... FAILED\ntest result: FAILED."
    cause, _ = classify_starvation(prompt, "", err)
    assert cause is not StarvationCause.TEST_ABSENT


def test_design_conflict_on_e0053() -> None:
    err = "error[E0053]: method `fmt` has an incompatible type for trait"
    cause, _ = classify_starvation(_PROMPT, "", err)
    assert cause is StarvationCause.DESIGN_CONFLICT


def test_error_truncated_tail_detected() -> None:
    err = "   |     ^^^^^ expected `&str`, found `String`\n   = note: ..."
    cause, _ = classify_starvation(_PROMPT, "", err)
    assert cause is StarvationCause.ERROR_TRUNCATED


def test_u2_gate_wrapper_not_truncated_when_evidence_intact() -> None:
    # cargo_runner.py's u2_test_gate prepends its own verdict line before the
    # real libtest evidence — that framing must not itself be read as a
    # cut-off diagnostic when the body beneath it is intact.
    prompt = _PROMPT + "test_clone\n"
    err = ("This compiled, but these tests still FAIL: ['test_clone']\n\n"
           "Fix the BEHAVIOR so they pass.\n"
           "---- test_clone stdout ----\n"
           "thread 'test_clone' panicked at src/chain.rs:35:5:\n"
           "assertion `left == right` failed")
    cause, _ = classify_starvation(prompt, "", err)
    assert cause is not StarvationCause.ERROR_TRUNCATED


def test_regression_gate_message_is_unknown_not_truncated() -> None:
    # the regression gate's message carries no diagnostic beneath its own
    # commentary at all — it should short-circuit to UNKNOWN with its own
    # line as evidence, not be misread as a cut-off excerpt.
    err = ("This dropped the cluster pass count 40→38 (tests that used to "
           "run/pass no longer do). Revert the change that removed them.")
    cause, ev = classify_starvation(_PROMPT, "", err)
    assert cause is StarvationCause.UNKNOWN
    assert ev.startswith("This dropped the cluster pass count")


def test_e0277_names_type_absent_from_prompt() -> None:
    err = "error[E0277]: the trait bound `Quoted: Write` is not satisfied"
    cause, ev = classify_starvation(_PROMPT, "", err)
    assert cause is StarvationCause.TYPE_DEF_ABSENT and ev == "Quoted"


def test_unknown_for_clean_head_without_signals() -> None:
    err = "error: linking with `cc` failed: exit status: 1"
    cause, _ = classify_starvation(_PROMPT + " cc ", "", err)
    assert cause is StarvationCause.UNKNOWN


# --- application: correlation over a scripted event stream -------------------

_RID = "1-a"


def _ev(kind: str, ts: str, tid: int, **data):
    return {"event": kind, "ts": f"2026-07-04T{ts}+00:00", "run_id": _RID,
            "tid": tid, **data}


def _stream():
    return [
        _ev("llm.request", "10:00:00", 7,
            data={"prompt": {"sha256": "p1"}}),
        _ev("llm.response", "10:00:30", 7,
            data={"response": {"sha256": "r1"}}),
        # other thread's exchange inside the window — must not correlate
        _ev("llm.request", "10:00:40", 9,
            data={"prompt": {"sha256": "px"}}),
        _ev("llm.response", "10:00:50", 9,
            data={"response": {"sha256": "rx"}}),
        _ev("phase_c.fn.fill.fail", "10:01:00", 7, duration_ms=60_000,
            data={"file": "src/error.rs", "fn": "object_ref", "cluster": "stable",
                  "fix_attempts": 2,
                  "error_codes": ["E0425"],
                  "last_error_preview":
                      "error[E0425]: cannot find function `ext_context` "
                      "in this scope"}),
        {"event": "llm.request", "ts": "2026-07-04T10:02:00+00:00",
         "run_id": "other-run", "tid": 7,
         "data": {"prompt": {"sha256": "p-other"}}},
    ]


_BLOBS = {
    "p1": "Fill fn object_ref in src/error.rs. TYPES: Error, Own",
    "r1": "self.ext_context(msg)",
    "px": "Fill fn context in src/context.rs",
    "rx": "other body",
}


def test_collect_exchanges_pairs_by_tid_and_filters_run() -> None:
    exchanges, fails = collect_exchanges(_stream(), _RID)
    assert [(x.tid, x.prompt_sha, x.response_sha) for x in exchanges] == [
        (7, "p1", "r1"), (9, "px", "rx")]
    assert len(fails) == 1


def test_mine_run_correlates_and_classifies() -> None:
    pms = mine_run(_stream(), _RID, _BLOBS.get)
    assert len(pms) == 1
    pm = pms[0]
    assert pm.unit == "src/error.rs::object_ref"
    assert pm.cause is StarvationCause.SYMBOL_ABSENT
    assert pm.evidence == "ext_context"
    assert pm.attempts == 2


def test_mine_run_unknown_when_no_exchange_matches() -> None:
    stream = [e for e in _stream() if e.get("tid") != 7
              or e["event"].startswith("phase_c")]
    pms = mine_run(stream, _RID, _BLOBS.get)
    assert pms[0].cause is StarvationCause.UNKNOWN
    assert pms[0].evidence == "no exchange correlated"


def test_render_report_ranks_causes() -> None:
    pms = mine_run(_stream(), _RID, _BLOBS.get)
    out = render_report(pms, _RID)
    assert "symbol_absent    1" in out
    assert "stable:src/error.rs::object_ref" in out


def test_error_codes_flow_from_event_payload() -> None:
    pms = mine_run(_stream(), _RID, _BLOBS.get)
    assert pms[0].error_codes == ("E0425",)
    out = render_report(pms, _RID)
    assert "codes: E0425×1" in out
    assert "['E0425']" in out


def test_error_codes_surface_even_without_correlation() -> None:
    # No matching exchange → cause UNKNOWN, but the codes still come straight
    # from the event payload (the whole point of threading them onto the event).
    stream = [e for e in _stream() if e.get("tid") != 7
              or e["event"].startswith("phase_c")]
    pms = mine_run(stream, _RID, _BLOBS.get)
    assert pms[0].cause is StarvationCause.UNKNOWN
    assert pms[0].error_codes == ("E0425",)


def test_response_unused_when_no_error_captured() -> None:
    cause, ev = classify_starvation(_PROMPT, "<FN name=\"x\">1</FN>", "")
    assert cause is StarvationCause.RESPONSE_UNUSED
    assert "nothing spliced" in ev


def test_unknown_when_neither_error_nor_response() -> None:
    cause, _ = classify_starvation(_PROMPT, "", "  ")
    assert cause is StarvationCause.UNKNOWN


def test_focused_error_header_is_a_diagnostic_head() -> None:
    err = "# Errors in target file:\nsomething uninformative\n"
    cause, _ = classify_starvation(_PROMPT, "body", err)
    assert cause is StarvationCause.UNKNOWN  # header present ⇒ not truncated


def test_frozen_bound_mismatch_classifies_as_design_conflict() -> None:
    container = ("impl<T, E> Context<T, E> for Result<T, E> "
                 "where E: ext::StdError + Send + Sync + 'static")
    err = ("# Errors in target file:\n"
           "error[E0277]: the trait bound `E: core::error::Error` "
           "is not satisfied\n")
    cause, ev = classify_starvation(_PROMPT, "", err, container=container)
    assert cause is StarvationCause.DESIGN_CONFLICT
    assert "core::error::Error" in ev and ev.startswith("E: frozen")


def test_e0609_invented_field_on_known_type_is_design_conflict() -> None:
    err = "error[E0609]: no field `vtable` on type `ptr::Own<ErrorImpl>`"
    cause, ev = classify_starvation(_PROMPT, "", err)   # `Own` IS in prompt
    assert cause is StarvationCause.DESIGN_CONFLICT
    assert ev == "invented field `vtable` on `Own`"


def test_e0609_field_on_unknown_type_is_type_def_absent() -> None:
    err = "error[E0609]: no field `buf` on type `Indented<'_, T>`"
    cause, ev = classify_starvation(_PROMPT, "", err)
    assert cause is StarvationCause.TYPE_DEF_ABSENT and ev == "Indented"

def test_foreign_error_when_every_error_is_downstream() -> None:
    # A sibling's leftover broken body fails the whole-crate compile; THIS
    # unit's excerpt then opens with the elsewhere header and no target-file
    # section. That is not starvation and not this unit's defect — the old
    # ERROR_TRUNCATED label sent the investigation to the excerpt builder.
    err = ("# Errors elsewhere in the crate (likely downstream of the target):\n"
           "error[E0308]: mismatched types\n"
           "  --> src/ensure.rs:73:67\n")
    cause, ev = classify_starvation(_PROMPT, "self.count()", err)
    assert cause is StarvationCause.FOREIGN_ERROR
    assert "src/ensure.rs:73" in ev
    # With a target-file section present the errors ARE (also) ours — the
    # foreign shortcut must not swallow that case.
    both = ("# Errors in target file:\nerror[E0308]: mismatched types\n"
            "# Errors elsewhere in the crate (likely downstream of the target):\n"
            "error[E0308]: mismatched types\n")
    cause2, _ = classify_starvation(_PROMPT, "self.count()", both)
    assert cause2 is not StarvationCause.FOREIGN_ERROR
