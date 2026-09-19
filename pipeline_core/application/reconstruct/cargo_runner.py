"""Application-layer cargo composition.

The `Cargo` port runs ONE command. Deriving the
`--message-format=json-diagnostic-rendered-ansi` flag and parsing the JSON
compiler-messages into structured diagnostics is the "higher-level concern"
the port docstring explicitly places in the application layer, not the
adapter. This function composes the port + `diagnostics_from_json` to give a
caller both a rendered-text view (prompts / logs) and structured diagnostics
(error counting / file attribution) — the regex-false-positive-free check.
"""

from __future__ import annotations

import os
import shlex
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from pipeline_core.application.reconstruct.attribution import make_fn_site_locator
from pipeline_core.application.reconstruct.cargo_errors import diagnostics_from_json
from pipeline_core.application.reconstruct.tdd_ratchet import (
    hollow_count,
    snapshot_src_tree,
)
from pipeline_core.domain.enums import ProcessFailure
from pipeline_core.domain.models import Diagnostic, FnSite
from pipeline_core.domain.test_command import derive_scoped_test_cmd
from pipeline_core.domain.test_isolation import (
    IsolatedTest,
    is_isolatable_test_run,
    listed_tests,
    listing_args,
    read_single_test_run,
    render_isolated_section,
    run_args,
    splice_isolated_sections,
)
from pipeline_core.domain.test_report import (
    BlameVerdict,
    CrashPanics,
    FailureExcerpt,
    FailureKind,
    TestFailure,
    TestRunReport,
    U2Decision,
    U2Verdict,
    crash_panics,
    external_stub_sites_of,
    parse_test_output,
    partition_blame,
    regressed_test_outputs,
    self_aborted_tests,
    stub_only_crash,
    stub_sites_outside,
)
from pipeline_core.domain.test_results import (
    RegressionComparison,
    TestRunOutcome,
    TestStateReading,
    build_broke,
    compare_readings,
    failure_bodies_for,
    hung_sections,
    parse_binary_results,
    parse_failed_tests,
    render_regression_detail,
)
from pipeline_core.ports import Cargo

# Pre-fill snapshot of the cluster's runtime test state, per test binary.
# `None` means the suite could not be MEASURED (crate did not compile → no
# `test result:` lines) — an unusable baseline, so the regression gate is
# skipped rather than guessing.
TestBaseline = "TestStateReading | None"

# A "run one cargo command → (rc, combined_output)" operation. The production
# binding is `pipeline._run_cargo` (itself port-composed); injecting it
# as a callable — rather than calling `run_cargo` directly — keeps the e2e/smoke
# harness's `monkeypatch pipeline._run_cargo` seam intact.
RunCargo = Callable[..., "tuple[int, str]"]

_MSG_FMT = "--message-format=json-diagnostic-rendered-ansi"
_KNOWN_SUBS = {"check", "test", "build", "run", "doc", "clippy"}


def _combined(stdout: str, stderr: str) -> str:
    """stdout + stderr in the legacy `_run_cargo` layout — built from the
    port's plain stdout/stderr (keeps the port a pure result record)."""
    return stdout + ("\n--- stderr ---\n" + stderr if stderr else "")


def run_cargo(
    cargo: Cargo,
    cmd: str,
    cwd: Path,
    env: dict[str, str],
    timeout: int = 180,
) -> tuple[int, str]:
    """Run one cargo command via the port; return (rc, combined_output).
    rc=-1 (timeout / binary-not-found) keeps the bare message (in stderr);
    otherwise the stdout+stderr-block combined layout (legacy parity).

    A command that runs test binaries has every binary whose section never
    closed re-run one test per process and its section rebuilt
    (`isolate_aborted_binaries`), so no reader of the capture ever sees one
    test's abort erase another test's verdict. `PHASE_C_TEST_ISOLATION=0`
    restores the shared-process capture (the negative control)."""
    p = cargo.run(cmd, cwd, env=env, timeout=timeout)
    if p.returncode == -1:
        # Keep the pre-kill partial stdout (names the hung test).
        return -1, (p.stdout + "\n" if p.stdout else "") + p.stderr
    stdout, stderr = isolate_aborted_binaries(
        cargo, cmd, cwd, env, p.stdout, p.stderr, timeout=timeout)
    return p.returncode, _combined(stdout, stderr)


def isolation_enabled() -> bool:
    """`PHASE_C_TEST_ISOLATION` (default on). Read per call: it decides how
    a capture is measured, and a reading must never mix two rules inside
    one process only because an env var was read once at a different
    boundary than the run that uses it."""
    return os.environ.get("PHASE_C_TEST_ISOLATION", "1").strip() != "0"


def isolate_aborted_binaries(
    cargo: Cargo,
    cmd: str,
    cwd: Path,
    env: dict[str, str],
    stdout: str,
    stderr: str,
    *,
    timeout: int,
) -> tuple[str, str]:
    """Re-run each aborted binary one test per process and splice its
    rebuilt section into the capture (see `domain.test_isolation`).

    A binary is re-run when its section opened and never printed a trailer
    and cargo named its executable. Its tests are LISTED with the original
    filters, then each runs alone under `--exact` on one thread. The whole
    step gets one deadline equal to the caller's `timeout`; a binary the
    deadline cuts, one whose executable is gone, or a capture whose shape
    will not splice unambiguously keeps its original section — the
    shared-process reading, never a guessed one. The environment is the
    run's own plus `CARGO_MANIFEST_DIR` (cargo sets it when it runs a test
    binary, and a harness may read it at runtime)."""
    if not (isolation_enabled() and is_isolatable_test_run(cmd)):
        return stdout, stderr
    combined = parse_binary_results(_combined(stdout, stderr))
    if len(combined) != len(parse_binary_results(stdout)):
        return stdout, stderr
    targets = [(i, b) for i, b in enumerate(combined)
               if not b.completed and not b.hung and b.exe_path]
    if not targets:
        return stdout, stderr
    deadline = time.monotonic() + max(1, timeout)
    run_env = dict(env)
    run_env.setdefault("CARGO_MANIFEST_DIR", str(cwd))
    rebuilt: dict[int, str] = {}
    for idx, b in targets:
        exe = Path(str(b.exe_path))
        if not exe.is_absolute():
            exe = cwd / exe
        if not exe.exists():
            continue
        left = deadline - time.monotonic()
        if left <= 0:
            break
        listing = cargo.run(shlex.join([str(exe), *listing_args(cmd)]), cwd,
                            env=run_env, timeout=int(max(1, left)))
        if listing.returncode != 0:
            continue
        names = listed_tests(listing.stdout)
        if not names:
            continue
        runs: list[IsolatedTest] = []
        for name in names:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            one = cargo.run(shlex.join([str(exe), *run_args(cmd, name)]), cwd,
                            env=run_env, timeout=int(max(1, min(left, timeout))))
            timed_out = (one.returncode == -1
                         and getattr(one, "failure", None) is not ProcessFailure.BINARY_MISSING)
            runs.append(read_single_test_run(
                name, one.returncode, _combined(one.stdout, one.stderr),
                timed_out=timed_out, timeout=min(left, timeout)))
        if len(runs) != len(names):
            continue
        rebuilt[idx] = render_isolated_section(runs)
    spliced = splice_isolated_sections(stdout, stderr, rebuilt)
    return spliced if spliced is not None else (stdout, stderr)


def run_scoped_tests(
    run_cargo_fn: RunCargo,
    test_cmd: str,
    cwd: Path,
    env: dict[str, str],
    *,
    timeout: int = 120,
    err_cap: int,
    only: set[str] | None = None,
) -> TestRunOutcome:
    """Plan 10 U1 — the reusable test-validation step (runtime analog of a
    compile check). Runs `test_cmd` via the injected `run_cargo_fn` and returns
    a :class:`TestRunOutcome` (passed, fail_count, detail, failed_names).

    `passed` is True only when cargo reports rc==0 with zero failing tests.
    `detail` is the failing test(s)' own `---- name stdout ----` body/bodies —
    `test_cmd` runs the WHOLE cluster's tests, so a blind tail-slice of the
    combined output frequently lands on an unrelated binary's summary banner
    instead of the actual panic/assertion text; extracting the specific
    section is what fixes that. `only`, when given, restricts which failing tests'
    bodies are included (the caller's own tests, not every unrelated failure
    in the cluster) — falls back to ALL failing tests' bodies when omitted,
    and to the raw tail-slice only if no section body could be found at all.
    On timeout the caller gets (False, -1, …, set()) and should treat it as
    INCONCLUSIVE — fall back to compile-based acceptance — ONLY when the
    timeout cut a build (no test section had opened). A timeout that cut
    the run INSIDE a test is a test that never returned: the outcome names
    it as a failure (`detail` says HUNG), because a body that makes the
    suite hang is the one answer no gate may accept on "no evidence".
    """
    rc, out = run_cargo_fn(test_cmd, cwd, env, timeout=timeout)
    if rc == -1:
        hung = hung_sections(out or "")
        if hung and hung[-1].dangling:
            name = hung[-1].dangling
            return TestRunOutcome(
                False, 1, render_hang_detail(name, hung[-1].label, timeout),
                {name})
        return TestRunOutcome(False, -1, "scoped test run timed out", set())
    failed = parse_failed_tests(out)
    passed = rc == 0 and len(failed) == 0
    if passed:
        detail = ""
    else:
        wanted = (failed & only) if only is not None else failed
        bodies = failure_bodies_for(out, wanted)
        detail = ("\n\n".join(f"---- {name} stdout ----\n{body}"
                               for name, body in bodies.items())[-err_cap:]
                   if bodies else out[-err_cap:])
    return TestRunOutcome(passed, len(failed), detail, failed)


def measure_test_state(
    run_cargo_fn: RunCargo,
    test_cmd: str,
    cwd: Path,
    env: dict[str, str],
    *,
    timeout: int = 120,
    raw_sink: list[str] | None = None,
) -> TestStateReading | None:
    """Run the FULL cluster `test_cmd` and return a per-binary
    :class:`TestStateReading`, or None when the suite could not run (compile
    failure / timeout → no `test result:` lines). Shared by the regression
    gate for both the pre-fill baseline and the post-fill re-measure. The
    per-binary shape lets the gate scope its comparison to binaries that
    COMPLETED in both readings — a binary that crashes mid-run emits a
    nondeterministic subset of verdicts, so whole-run counts are draws from a
    distribution, not measurements.

    `raw_sink`, when given, receives the raw combined capture: the READING
    deliberately drops panic text, but crash attribution (was the abort a
    still-hollow stub's panic?) is decided from exactly that text."""
    rc, out = run_cargo_fn(test_cmd, cwd, env, timeout=timeout)
    if raw_sink is not None:
        raw_sink.append(out)
    if rc == -1:
        # The caller's timeout cut the run. A capture with an open test
        # section is a READING: that binary hung (marked so, its in-flight
        # test named) and the binaries after it never ran. A capture with no
        # section is a build that did not finish — inconclusive, as before.
        hung = TestStateReading(parse_binary_results(out or "", timed_out=True))
        if hung.binaries:
            return hung
        return None
    reading = TestStateReading(parse_binary_results(out))
    # No test-result sections AND a non-zero rc ⇒ the suite never executed
    # (the crate itself did not compile). Zero tests with rc==0 is a
    # legitimate empty baseline (pass_count 0, no failures).
    if not reading.binaries and rc != 0:
        return None
    return reading


def test_regression_gate(
    run_cargo_fn: RunCargo,
    test_cmd: str | None,
    cwd: Path,
    env: dict[str, str] | None,
    baseline: TestStateReading | None,
    *,
    enabled: bool,
    timeout: int,
    guard: Callable[[str, str], None],
    stub_free: bool = False,
    introduced_stubs: bool = False,
    observe: Callable[[RegressionComparison], None] | None = None,
    hollow_before: int | None = None,
) -> tuple[RegressionComparison, str] | None:
    """Global non-regression gate (the monotone ratchet). A fill is a REGRESSION
    if it turns a previously-passing test red, crashes a previously-completing
    test binary, or drops the passing/executed counts — all judged ONLY over
    binaries that COMPLETED in both readings. A binary that was already
    crashing at baseline emits a nondeterministic verdict subset (which tests
    print before the SIGSEGV varies run-to-run), so its tests can neither
    convict nor exonerate a fill; comparing them produced systematic false
    reverts (trivial fills "blamed" for pre-existing segfault noise, attempts
    burned, blocks falsely exhausted — the pass=14 plateau engine).

    Returns:
      - None → ACCEPT (gate off, no test_cmd, no valid baseline, the post-fill
        suite was inconclusive, or nothing regressed within the measured
        scope — never block on uncertainty, mirroring `u2_test_gate`).
      - (comparison, detail) → REVERT: the typed
        :class:`RegressionComparison` (which regression fired, over which
        measured scope — callers read `.summary` to log it and `.signature` to
        key the stall on it, rather than inferring a cause from an empty
        `newly_red`); `detail` is the prompt-ready explanation, which names the
        measured scope and lists pre-existing crashed binaries as excluded.

    ``stub_free`` — the caller's statement that the cluster-active tree held
    no hollow body when the pass began. It decides the crash-only branch
    below: the keep there rests on a still-hollow sibling being able to
    speak, and in a stub-free tree none can.

    ``introduced_stubs`` — the caller just put hollow bodies back (file-first
    peel). The stub-panic keep below would then exonerate the peel's own
    restored stubs: run `1160135-7b10cdfa` (2026-08-29) peeled
    `construct_from_boxed` and `context` back to `unimplemented!()`, kept
    ten rewrites, and those two panics withheld 60 vanished passes. A caller
    that introduced the panicking stubs must revert, not keep.

    ``hollow_before`` — how many hollow bodies the cluster tree held when
    the caller took its baseline. It is the one input of the ONLY keep this
    gate still grants a crash-only reading: the sweep's own closure
    exemption (`tdd_ratchet.closure_withheld`) — every lost test vanished
    with a crashed binary, the capture's panics are exclusively stub panics,
    and the hollow set strictly SHRANK since the baseline. Under exactly that
    evidence the batch ratchet withholds the breach one sweep later, so the
    fill and the sweep agree. Every other crash-only reading is a REVERT
    here, not a keep "with cost withheld": run `959654-1e96b403`
    (2026-09-06) kept a `src/error.rs` file-first that crashed five binaries
    (six baseline passes lost, no panic located), the next sweep counted the
    vanished tests as failing and the ratchet restored the snapshot — the
    same cycle the night before at iters 9→10 of `334902-2774708c`. Two
    judges, two rules, one iteration lost per cycle. None (a caller that did
    not measure) means the exemption cannot apply.
    """
    if not (enabled and test_cmd and baseline is not None):
        return None
    _raw: list[str] = []
    try:
        after = measure_test_state(run_cargo_fn, test_cmd, cwd, env or {},
                                   timeout=timeout, raw_sink=_raw)
    except Exception as e:  # noqa: BLE001 — fail-safe to compile-based accept
        guard("phase-c/regr_skip", f"{type(e).__name__}: {e}")
        return None
    if after is None:
        return None  # crate not compiling now ⇒ can't judge; defer to TDD loop
    cmp = compare_readings(baseline, after)
    # Attach what the reddened tests printed, from the same capture the
    # comparison was read from — the detail used to name the tests and drop
    # their `left` / `right` (`regressed_test_outputs` carries the instance).
    if cmp.newly_red:
        cmp = replace(cmp, newly_red_output=regressed_test_outputs(
            _raw[-1] if _raw else "", cmp.newly_red))
    # PUBLISH THE READING. This full-suite measurement runs on every splice and
    # its numbers were discarded on every path — the gate reduced them to a
    # yes/no, so nothing in the event store carried a score between one
    # iteration's sweep and the next, and the only way to read one was to kill
    # the run. Fired here, at the measurement, so it cannot alter the decision
    # below; `application/reconstruct/fill_reading.py` documents why the
    # OUTCOME is deliberately not threaded through the six exits that follow.
    if observe is not None:
        observe(cmp)
    if cmp.excluded_crashed:
        guard("phase-c/u2_missing_verdict",
              f"binaries {list(cmp.excluded_crashed)} were already crashed at "
              "baseline — their tests are excluded from the regression verdict")
    if cmp.crashed_all_red:
        guard("phase-c/crash_all_red",
              f"binaries {list(cmp.crashed_all_red)} crashed after the fill "
              "but were all-red at baseline — nothing that worked was lost; "
              "not charged as regression (failure mode moved, panic → abort)")
    if cmp.newly_hung:
        # Charged before every other exit, the net-gain forgiveness included:
        # the binaries after the hung one never ran, so no net can be read,
        # and every later sweep of this tree costs the whole timeout. The
        # crash-only branch below asks what PANICKED in the dead binary; a
        # hung binary printed nothing and did not die.
        guard("phase-c/hang_reverted",
              f"binar{'ies' if len(cmp.newly_hung) > 1 else 'y'} "
              f"{list(cmp.newly_hung)} HUNG after the fill (timed out inside "
              f"{list(cmp.hung_tests)}; {len(cmp.not_run)} later binar"
              f"{'ies' if len(cmp.not_run) != 1 else 'y'} never ran) — a test "
              "that never returns is not an inconclusive reading; REVERTED "
              f"(cost: {cmp.crash_lost_pass} baseline pass(es) in the hung "
              "binary, plus every binary after it)")
        return cmp, render_regression_detail(cmp)
    if not cmp.regressed:
        if cmp.newly_crashed or cmp.newly_red:
            guard("phase-c/net_gain_accept",
                  f"kept despite cost — net {cmp.net_pass_delta:+d} test(s): "
                  f"crashed {list(cmp.newly_crashed)} (was passing "
                  f"{cmp.crash_lost_pass}), newly red {sorted(cmp.newly_red)}. "
                  "The oracle score is the objective; a strict net gain is "
                  "not refused over an individual loss")
        return None
    # Reached only for a binary the capture could NOT rebuild one test per
    # process (`isolate_aborted_binaries`: no executable path, the deadline,
    # an ambiguous splice, or `PHASE_C_TEST_ISOLATION=0`). An isolated binary
    # always completes, its aborted tests are per-test failures, and the
    # per-test comparison above already judged them; the branches below are
    # the fallback reading of a shared-process abort, not a second rule.
    # A crash-only verdict is asked one more question before it may revert:
    # WHAT panicked, in the binaries that COST something. When every
    # completed binary held (no newly-red, no measured-scope count drop) and
    # the `newly_crashed` binaries' own sections hold no crate panic but
    # `not implemented` in a stub, the crash is a still-hollow sibling
    # speaking — typically a correct fill letting Error::drop reach hollow
    # vtable fns for the first time. Reverting there erases correct work and
    # makes the component closable only by one sweep that fills every
    # drop-reachable stub at once: `construct_from_adhoc` was reverted five
    # times this way (run of 2026-08-09 00:37) and `context.rs`'s file-first
    # fill lost a compiling fill to a crash `src/error.rs` caused
    # (run `1048052-2feafd52`, iter 4). Keep the fill; the tests it cannot
    # move stay red/vanished, so nothing is over-reported, and once the last
    # stub fills the same gate judges everything again with real verdicts.
    # Judged over the `newly_crashed` SECTIONS, not the whole capture: an
    # assertion panic in a `crashed_all_red` binary (not charged above) or
    # in a completed one (a verdict, read as `newly_red` when new) is not
    # this crash's evidence. Read over the whole text, the runtime's own
    # `panic in a destructor during cleanup` line and `error_gen_3`'s
    # assertion vetoed every `ptr.rs` keep of run `2585062-494e630e`.
    # NOTE for future call sites: every current caller judges a FILL (hollow →
    # body). A caller judging the reverse direction — deliberately
    # INTRODUCING a stub — must pass ``introduced_stubs=True``, because a
    # stub restore's own panic would otherwise exonerate itself. File-first
    # peel is that caller.
    if (cmp.newly_crashed and not cmp.newly_red
            and cmp.cur_pass >= cmp.base_pass
            and cmp.cur_executed >= cmp.base_executed):
        # A crash-only reading: every COMPLETED binary held and the only
        # loss is a binary that produced no verdicts. The sweep judges this
        # shape by ONE rule — the oracle counts the vanished tests as failing
        # and the batch ratchet restores the snapshot — with ONE exemption,
        # `closure_withheld`: stub-only panics while the hollow set shrank.
        # This gate applies the same rule and the same exemption, so a fill
        # keep is never a sweep breach one iteration later. It used to keep
        # "with cost withheld" here (a still-hollow sibling might be the one
        # speaking; a crash implicates no unit by itself) and let the sweep
        # claw the keep back: a full iteration per cycle, and every later
        # unit of the batch judged with the crashed binaries excluded.
        _stub_sites = stub_only_crash(_raw[-1] if _raw else "",
                                      crashed=cmp.newly_crashed)
        # Carried out on every exit of this branch: the retro writer stamps
        # the record it earns from a stub-caused crash as the tree's, not
        # the body's (`crash_stub_sites`), and the failed-attempt comment
        # names the stubs instead of "did not compile".
        cmp = replace(cmp, crash_stub_sites=_stub_sites)
        _plural = 'ies' if len(cmp.newly_crashed) > 1 else 'y'
        if _stub_sites:
            _locs = sorted(f"{f}:{ln}" for f, ln in _stub_sites)
            if introduced_stubs:
                guard("phase-c/crash_introduced_stub_reverted",
                      f"binar{_plural} {list(cmp.newly_crashed)} crashed, and "
                      f"the capture's only panic(s) are stub panics at {_locs} "
                      "— this fill put those stub(s) back, so the panic is "
                      "its own; REVERTED rather than kept (cost: "
                      f"{cmp.crash_lost_pass} baseline pass(es))")
                return cmp, render_regression_detail(cmp)
            hollow_now = hollow_count(snapshot_src_tree(cwd))
            if hollow_before is not None and hollow_now < hollow_before:
                guard("phase-c/crash_closure_kept",
                      f"binar{_plural} {list(cmp.newly_crashed)} crashed, the "
                      f"capture's only panic(s) are stub panics at {_locs}, "
                      f"and the hollow set shrank {hollow_before} → "
                      f"{hollow_now} — a coupled component closing (the "
                      "sweep's own closure exemption); fill KEPT (cost: "
                      f"{cmp.crash_lost_pass} baseline pass(es) until the "
                      "stub(s) fill)")
                return None
            guard("phase-c/crash_stub_panic_reverted",
                  f"binar{_plural} {list(cmp.newly_crashed)} crashed on stub "
                  f"panic(s) at {_locs} with the hollow set not shrinking "
                  f"({hollow_before} → {hollow_now}) — not a closure; "
                  "REVERTED, as the sweep would charge it (cost: "
                  f"{cmp.crash_lost_pass} baseline pass(es))")
            return cmp, render_regression_detail(cmp)
        if stub_free:
            guard("phase-c/crash_stub_free_reverted",
                  f"binar{_plural} {list(cmp.newly_crashed)} crashed after the "
                  "fill and the tree holds no stub — no hollow sibling can be "
                  "the one speaking, so the crash is this fill's; REVERTED "
                  "now rather than kept for the next sweep's ratchet, so the "
                  "rest of the batch is judged against a live baseline "
                  f"(cost: {cmp.crash_lost_pass} baseline pass(es))")
            return cmp, render_regression_detail(cmp)
        guard("phase-c/crash_only_reverted",
              f"binar{_plural} {list(cmp.newly_crashed)} crashed with no "
              "newly-red verdict in any completed binary and no stub-only "
              "panic evidence in the crashed binar" + _plural + "'s own "
              "output — the sweep would count the vanished tests as failing "
              "and the ratchet would restore the snapshot; REVERTED here "
              "instead, so the rest of the fill is judged against a live "
              f"baseline (cost: {cmp.crash_lost_pass} baseline pass(es))")
        return cmp, render_regression_detail(cmp)
    return cmp, render_regression_detail(cmp)


def render_hang_detail(test: str, binary: str, timeout: int) -> str:
    """Prompt feedback for a body under which a test never RETURNED.

    Stated as the primary finding: the test produced no verdict because it
    never finished, not because an assertion failed, so the body's control
    flow — not its values — is what the next attempt has to change. The
    usual shape is a body that reaches itself: a Display or Debug impl that
    formats the value it is formatting, an accessor that forwards to a
    wrapper that forwards back. The test is named because it is the one
    locator the model has; the timeout is named so the model does not read
    the silence as a slow test."""
    return (
        f"Your body made the test `{test}` (binary `{binary}`) HANG: it did "
        f"not return within {timeout}s, printed no verdict, and every test "
        "after it in that binary never ran. This is not an assertion "
        "failure — the process was still running when it was killed. A hang "
        "is an infinite loop or unbounded recursion that this body reaches: "
        "a formatter that formats the value being formatted (`{self}` or "
        "`{:?}` of `self` inside its own impl, or through a wrapper that "
        "forwards back to it), an accessor that calls itself through a "
        "trait method with the same name, or a loop whose exit condition "
        "never changes. Write a body that terminates: format the inner "
        "fields, delegate to a DIFFERENT impl, and make every loop advance.")


def _render_u2_abort(aborted: frozenset[str], scoped: TestRunReport,
                     err_cap: int, panics: CrashPanics) -> str:
    """Prompt feedback for a body under which the test process ABORTED.

    Stated as the primary finding, not a footnote: these tests are not failing
    assertions to be argued with, they produced no output because the process
    died. The named tests come first and uncapped — which test was in flight is
    the one locator the model has — and any assertion that did print follows,
    explicitly demoted so the model does not spend the attempt on it.

    ``panics`` is what the aborted sections printed, and it decides WHICH
    abort is described. The capture distinguishes the two ways a Rust
    process aborts — the runtime announces a double panic on its own
    `panicked at` line — and the gate reaches this renderer only when that
    evidence did not exonerate the body: a segfault (no panic located: the
    old undefined-behaviour text, still right), or a crate panic of the
    unit's own that a still-stubbed `Drop`-path fn then turned into an
    abort while unwinding. For the second the fault is the FIRST panic, and
    telling the model to re-derive the body against memory faults was a
    wrong-body steer on a right body (the live `ptr.rs::new` comment of run
    `2585062-494e630e` carries exactly that text over the textbook body)."""
    names = ", ".join(sorted(aborted))
    head = (
        "Your body ABORTED the test process. These test(s) produced a verdict "
        "BEFORE your change and produced no output at all after it:\n"
        f"{names}\n\n"
        "This is not an assertion failure — the process died, so every later "
        "test in the same binary was never run. A normal `panic!` does not do "
        "this: libtest catches it and prints a failure. ")
    if panics.crate_panics:
        own = "; ".join(f"{p.file}:{p.line}: {p.message}"
                        for p in panics.crate_panics[:4])
        stubs = panics.rendered_stub_sites
        body = (
            "The capture shows a DOUBLE panic. Your test's own panic came "
            f"first: {own}. "
            + (f"While it unwound, a still-stubbed fn on the drop path panicked "
               f"too (`not implemented` at {stubs}), "
               if stubs else
               "While it unwound, a second panic fired in a destructor, ")
            + "and the runtime aborted the process"
            + (" (`panic in a destructor during cleanup`)"
               if panics.runtime_aborts else "")
            + ". The abort is the tree's; the first panic is yours. Fix what "
            "the first panic reports and leave the drop path alone — the "
            "still-stubbed fn(s) are separate work.")
    elif panics.stub_sites:
        body = (
            "The only panics the aborted binar"
            f"{'ies' if len(panics.sections) > 1 else 'y'} printed are "
            f"`not implemented` at {panics.rendered_stub_sites}, and at least "
            "one of them resolves into THIS unit (or to no fn at all): the "
            "body still reaches a stub of its own. Replace every "
            "`unimplemented!()` / `todo!()` this body can reach with a real "
            "body.")
    else:
        body = (
            "No panic was located in the aborted binar"
            f"{'ies' if len(panics.sections) > 1 else 'y'}, so this is not a "
            "panic in a destructor: it is undefined behaviour — an unsafe "
            "block reading/writing memory that does not hold what it "
            "claims.\n\n"
            "Re-derive the body from the types. Do not re-send the previous "
            "body with a different helper call — the fault is what the body "
            "does to memory, not which API it names.")
    out = head + body
    if scoped.failures:
        excerpt = FailureExcerpt(
            names=tuple(sorted(f.test_name for f in scoped.failures)),
            bodies=tuple((f.test_name, f.body) for f in scoped.failures))
        out += ("\n\n(Secondary — only after the abort is gone: these also "
                "failed.)\n" + excerpt.render(err_cap // 2))
    return out


def _render_u2_reject(blame: BlameVerdict, err_cap: int) -> str:
    """Prompt feedback for a REJECT: the unit's OWN failing tests + their
    bodies (via the shared :class:`FailureExcerpt` rule — names never truncated,
    bodies capped at line boundaries), plus a single summary line for any tests
    blocked by another unit's stub (so the model never chases a failure that
    isn't its fault)."""
    excerpt = FailureExcerpt(
        names=tuple(sorted(f.test_name for f in blame.self_failures)),
        bodies=tuple((f.test_name, f.body) for f in blame.self_failures))
    out = excerpt.render(err_cap)
    if blame.external:
        blocking = sorted({f"{loc.file}::{loc.fn_name}"
                           for _f, loc in blame.external})
        out += (f"\n\n(Ignore: {len(blame.external)} other failing "
                f"test(s) are blocked by still-stubbed fn(s) {blocking} "
                "— not this unit.)")
    if blame.no_verdict:
        out += (f"\n\n(Ignore: {len(blame.no_verdict)} other test(s) produced "
                "no output because their test process aborted — not judged.)")
    return out


def u2_test_gate(
    run_cargo_fn: RunCargo,
    fn_tests: set[str],
    unit_sites: frozenset[FnSite],
    test_cmd: str | None,
    target_src_dir: Path,
    cluster_env: dict[str, str] | None,
    *,
    structure: Any,
    enabled: bool,
    timeout: int,
    err_cap: int,
    guard: Callable[[str, str], None],
    baseline: TestStateReading | None = None,
) -> U2Verdict:
    """Plan 10 / F3 — the SINGLE reusable test-acceptance gate, called by every
    fill loop (per-fn, per-impl, post-merge) at the point a unit compiles.

    Runs the unit's own tests (`fn_tests`), parses the structured report, and
    PARTITIONS the failures: a test that panics inside a DIFFERENT unit's
    still-`unimplemented!()` stub is not this unit's fault. Returns a typed
    :class:`U2Verdict`:
      - ACCEPT               → tests pass / gate off / no tests / inconclusive.
      - ACCEPT_EXTERNAL_ONLY → compiled; no failing test convicts this unit —
        each is either blocked by another unit's stub or carries no verdict at
        all (named FAILED with no captured output, i.e. the test process
        aborted). Caller KEEPS the fill, writes no retro, does not exhaust,
        does not immediate-retry (the outer TDD loop re-runs once the blocking
        producers fill).
      - REJECT               → ≥1 real self failure. `self_failures` is the
        stall signature; `detail` feeds the next prompt (external failures
        summarized, never chased).
    Fail-safe: any error / timeout → ACCEPT (compile-based acceptance).

    `baseline` is the reading taken against the PRE-SPLICE tree (the same one
    the regression gate uses). Without it the gate cannot tell a test that
    never had a verdict from one whose verdict this body destroyed, so an
    aborting body reads as "no failures" and is accepted — see
    :func:`self_aborted_tests`.

    `unit_sites` is what this unit owns as container-qualified :class:`FnSite`
    identities (per-fn: that one fn; per-impl: the block's stub members;
    file-first: the file's fns) — the partition subtracts only stub-panics that
    resolve OUTSIDE it, matched on (file, name, canonical container) so a
    same-named sibling's stub is never counted as this unit's own failure.

    The `cargo test` invocation is scoped to `fn_tests` via libtest's own
    `-- name1 name2 ...` OR filter — unlike `test_regression_gate`, which stays
    unscoped BY DESIGN.
    """
    if not (enabled and test_cmd and fn_tests):
        return U2Verdict.accept()
    scoped_cmd = derive_scoped_test_cmd(test_cmd, fn_tests)
    try:
        rc, out = run_cargo_fn(scoped_cmd, target_src_dir, cluster_env or {},
                               timeout=timeout)
    except Exception as e:  # noqa: BLE001 — fail-safe to compile-based accept
        guard("phase-c/u2_skip", f"{type(e).__name__}: {e}")
        return U2Verdict.accept()
    if rc == -1:
        # The timeout cut the scoped run. Inside a test section it is a
        # test of THIS unit's own that never returned (the scoped command
        # runs `fn_tests` and nothing else): a self failure with the hang
        # as its evidence. Before any section opened it is a build that did
        # not finish — inconclusive, never block, as before.
        hung = hung_sections(out or "")
        if hung and hung[-1].dangling:
            name = hung[-1].dangling
            guard("phase-c/u2_hang",
                  f"scoped run timed out inside `{name}` ({hung[-1].label}) — "
                  "a test that never returns convicts the body under test")
            return U2Verdict(
                U2Decision.REJECT, self_failures=frozenset({name}),
                detail=render_hang_detail(name, hung[-1].label, timeout))
        return U2Verdict.accept()  # build did not finish → INCONCLUSIVE
    report = parse_test_output(out)
    scoped = TestRunReport(
        report.passed,
        tuple(f for f in report.failures if f.test_name in fn_tests))
    # Expected-vs-reported for OUR OWN tests: a crashed/uncompilable scoped
    # binary takes its tests away without a FAILED line, so rc!=0 with no
    # parsed failures used to read as "no failures" here. The verdict stays
    # fail-safe ACCEPT (the whole-cluster regression gate owns executed-count
    # drops, with a baseline this gate lacks) — but the deficit is surfaced,
    # never silently invisible.
    _reported = report.passed | {f.test_name for f in report.failures}
    _missing = fn_tests - _reported
    # Cargo's verdict from the build phase of the scoped capture, the same
    # reading the cluster sweep takes — a test that runs a compiler itself
    # prints the same words later in the stream.
    _compile_failed = build_broke(out or "")
    if rc != 0 and _missing:
        guard("phase-c/u2_missing_verdict",
              f"{sorted(_missing)} reported no verdict in the scoped run "
              + ("(compile failed — no test binaries ran)"
                 if _compile_failed else "(binary crashed)"))
    # A verdict this body DESTROYED outranks every other signal: the binary
    # judged these tests before the splice and died before reaching them
    # after it. Charged before the `not scoped.failures` early-accept below,
    # because the worst case prints nothing at all — a body that aborts the
    # process instantly leaves an empty failure list, which that early return
    # reads as "no failures" and ACCEPTS.
    # rc == 0 means the binary ran to completion, so a name absent from the
    # output was never executed (libtest's OR filter did not match it), not
    # killed mid-run — charging that would convict a body of a filter miss.
    _aborted = (self_aborted_tests(frozenset(_missing), baseline.judged_names)
                if baseline is not None and rc != 0 and not _compile_failed
                else frozenset())
    if _aborted:
        # Destroyed verdicts convict the body — unless the capture proves the
        # aborting panic fired inside ANOTHER unit's `unimplemented!()` stub
        # (every crate panic in the ABORTED sections a stub panic, none
        # resolving into this unit; the runtime's own double-panic line is
        # the announcement of that stub panic in `Drop`, not a veto). Then
        # the abort is the hollow tree speaking: a correct body finally made
        # a still-stubbed sibling reachable (drop-path fns, typically), and
        # rejecting it burns attempts toward a conviction the unit never
        # earned — `from_boxed` spent attempts 2/4/5 exactly here in run
        # `1048052-2feafd52` while `src/error.rs` still held its hollow
        # drop machinery, and `ptr.rs::new` two attempts and two retro
        # rounds in run `2585062-494e630e` with the destructor line as the
        # veto. Keep the fill, name the blockers, defer to the outer loop's
        # producer re-admission.
        _locate = make_fn_site_locator(structure, target_src_dir)
        _panics = crash_panics(out)
        _ext = external_stub_sites_of(_panics, frozenset(unit_sites), _locate)
        if _ext:
            _names = sorted(f"{s.file}::{s.fn_name}" for s in _ext)
            guard("phase-c/u2_abort_external",
                  f"{sorted(_aborted)} lost their verdict to an abort whose "
                  f"only panic(s) are stub panics in still-stubbed {_names} — "
                  "external, fill kept and deferred (not this unit's fault)")
            _sites = sorted(
                _ext, key=lambda s: (s.file, s.fn_name, s.container))
            _pairs = tuple(
                (TestFailure(test_name=n, kind=FailureKind.STUB_PANIC,
                             panic_file=s.file, panic_line=None,
                             panic_col=None, body=""), s)
                for n, s in zip(sorted(_aborted),
                                _sites * len(_aborted), strict=False))
            return U2Verdict.accept_external_only(_pairs, frozenset(_aborted))
        return U2Verdict.reject(
            frozenset(_aborted) | {f.test_name for f in scoped.failures},
            _render_u2_abort(_aborted, scoped, err_cap, _panics),
            sites=scoped.failures, aborted=frozenset(_aborted), crash=_panics,
            external_stub_sites=stub_sites_outside(
                _panics.stub_sites, frozenset(unit_sites), _locate))
    if not scoped.failures:
        # The one ACCEPT with POSITIVE evidence: these tests ran and passed.
        # Carrying the names is what lets the caller's verdict row say so —
        # the other accepts above (gate off, no tests, timeout, exception) have
        # nothing to carry and must not be recorded as if they had.
        return U2Verdict.accept(frozenset(report.passed & fn_tests))
    locate = make_fn_site_locator(structure, target_src_dir)
    blame = partition_blame(scoped, frozenset(unit_sites), locate)
    unjudged = frozenset(f.test_name for f in blame.no_verdict)
    if unjudged:
        guard("phase-c/u2_no_verdict",
              f"{sorted(unjudged)} were named FAILED but printed no output "
              "(process aborted) — not charged to this unit")
    if not blame.self_failures:
        # Includes the all-inconclusive case: nothing convicts, so the
        # compiling fill stands rather than burning attempts on a verdict the
        # unit cannot influence.
        return U2Verdict.accept_external_only(blame.external, unjudged)
    detail = _render_u2_reject(blame, err_cap)
    return U2Verdict.reject(
        frozenset(f.test_name for f in blame.self_failures), detail, unjudged,
        self_passed=frozenset(report.passed & fn_tests),
        sites=blame.self_failures)


def run_check_diag(
    cargo: Cargo,
    cmd: str,
    cwd: Path,
    env: dict[str, str],
    timeout: int = 180,
) -> tuple[int, str, list[Diagnostic]]:
    """Run a cargo check/build/test with json-diagnostic output via `cargo`.

    Returns (rc, rendered_text, diagnostics). The flag is inserted after the
    cargo SUBCOMMAND when the command doesn't already carry `--message-format`
    (the cmd may be `cargo +nightly check`). rendered_text is reconstructed
    from each diagnostic's `rendered` field plus the non-JSON lines, so it
    mirrors a plain `cargo check`. rc=-1 (timeout / binary-not-found) carries
    the bare message as the text, [] diagnostics (legacy parity).
    """
    argv = shlex.split(cmd)
    if not any(a.startswith("--message-format") for a in argv):
        insert_at = len(argv)  # fallback: append at end
        for i, a in enumerate(argv):
            if a in _KNOWN_SUBS:
                insert_at = i + 1
                break
        argv.insert(insert_at, _MSG_FMT)
    proc = cargo.run(shlex.join(argv), cwd, env=env, timeout=timeout)
    if proc.returncode == -1:
        # timeout / binary-not-found: stderr carries the bare message
        return -1, proc.stderr, []
    raw = _combined(proc.stdout, proc.stderr)
    diagnostics = diagnostics_from_json(raw) or []
    rendered_parts: list[str] = []
    for ln in raw.splitlines():
        if ln.strip().startswith("{"):
            continue
        rendered_parts.append(ln)
    for d in diagnostics:
        if d.rendered:
            rendered_parts.append(d.rendered)
    return proc.returncode, "\n".join(rendered_parts), diagnostics


@dataclass(frozen=True, slots=True)
class PortBoundCargoRunner:
    """Production `ports.cargo_runner.CargoRunner`: the module's composition
    fns bound to one raw `Cargo` port. Built once in `build_app`."""

    cargo: Cargo

    def run(self, cmd: str, cwd: Path, env: dict[str, str],
            timeout: int = 180) -> tuple[int, str]:
        return run_cargo(self.cargo, cmd, cwd, env, timeout)

    def check_diag(self, cmd: str, cwd: Path, env: dict[str, str],
                   timeout: int = 180) -> tuple[int, str, list[Diagnostic]]:
        return run_check_diag(self.cargo, cmd, cwd, env, timeout)
