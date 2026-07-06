"""Application-layer cargo composition (P2 slice 6).

The `Cargo` port runs ONE command. Deriving the
`--message-format=json-diagnostic-rendered-ansi` flag and parsing the JSON
compiler-messages into structured diagnostics is the "higher-level concern"
the port docstring explicitly places in the application layer, not the
adapter. This function composes the port + `diagnostics_from_json` to give a
caller both a rendered-text view (prompts / logs) and structured diagnostics
(error counting / file attribution) — the regex-false-positive-free check.
"""

from __future__ import annotations

import shlex
from collections.abc import Callable
from pathlib import Path

from pipeline_core.application.reconstruct.cargo_errors import diagnostics_from_json
from pipeline_core.domain.models import Diagnostic
from pipeline_core.domain.test_results import (
    TestRunOutcome,
    failure_bodies_for,
    parse_failed_tests,
    parse_test_counts,
)
from pipeline_core.ports import Cargo

# Pre-fill snapshot of the cluster's runtime test state: (pass_count, failed
# names). `None` means the suite could not be MEASURED (crate did not compile →
# no `test result:` lines) — an unusable baseline, so the regression gate is
# skipped rather than guessing.
TestBaseline = "tuple[int, frozenset[str]] | None"

# A "run one cargo command → (rc, combined_output)" operation. The production
# binding is `pipeline._run_cargo` (itself port-composed, slice 8); injecting it
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
    otherwise the stdout+stderr-block combined layout (legacy parity)."""
    p = cargo.run(cmd, cwd, env=env, timeout=timeout)
    if p.returncode == -1:
        return -1, p.stderr
    return p.returncode, _combined(p.stdout, p.stderr)


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
    INCONCLUSIVE — fall back to compile-based acceptance rather than block.
    """
    rc, out = run_cargo_fn(test_cmd, cwd, env, timeout=timeout)
    if rc == -1:
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
) -> tuple[int, frozenset[str]] | None:
    """Run the FULL cluster `test_cmd` and return (pass_count, failing-names),
    or None when the suite could not run (compile failure / timeout → no
    `test result:` lines). Shared by the regression gate for both the pre-fill
    baseline and the post-fill re-measure. Unlike `run_scoped_tests` this keeps
    the pass_count so a drop in the number of tests that EXECUTE (e.g. a fill
    that breaks a test target's compilation) is detectable, not just pass→fail
    flips."""
    rc, out = run_cargo_fn(test_cmd, cwd, env, timeout=timeout)
    if rc == -1:
        return None
    passed, failed_n, _ig = parse_test_counts(out)
    # No test-result lines AND a non-zero rc ⇒ the suite never executed (the
    # crate itself did not compile). Zero tests with rc==0 is a legitimate
    # empty baseline (pass_count 0, no failures).
    if passed + failed_n == 0 and rc != 0:
        return None
    return passed, frozenset(parse_failed_tests(out))


def test_regression_gate(
    run_cargo_fn: RunCargo,
    test_cmd: str | None,
    cwd: Path,
    env: dict[str, str] | None,
    baseline: tuple[int, frozenset[str]] | None,
    *,
    enabled: bool,
    timeout: int,
    guard: Callable[[str, str], None],
) -> tuple[frozenset[str], str] | None:
    """Global non-regression gate (the monotone ratchet). A fill is a REGRESSION
    if it turns any previously-passing test red, or drops the number of passing
    tests. Unlike `u2_test_gate` (which only inspects the filled unit's OWN
    tests) this guards the WHOLE cluster, so a fill that fixes its own tests
    while breaking siblings is rejected — the root cause of the pass-count
    oscillation ceiling.

    Returns:
      - None → ACCEPT (gate off, no test_cmd, no valid baseline, the post-fill
        suite was inconclusive, or nothing regressed — never block on
        uncertainty, mirroring `u2_test_gate`'s fail-safe stance).
      - (newly_red, detail) → REVERT: `newly_red` is the set of previously-green
        tests this fill broke (empty when the regression is a pass-count drop
        with no nameable new failure); `detail` is the prompt-ready explanation.
    """
    if not (enabled and test_cmd and baseline is not None):
        return None
    base_pass, base_failed = baseline
    try:
        after = measure_test_state(run_cargo_fn, test_cmd, cwd, env or {},
                                   timeout=timeout)
    except Exception as e:  # noqa: BLE001 — fail-safe to compile-based accept
        guard("phase-c/regr_skip", f"{type(e).__name__}: {e}")
        return None
    if after is None:
        return None  # crate not compiling now ⇒ can't judge; defer to TDD loop
    cur_pass, cur_failed = after
    newly_red = cur_failed - base_failed
    if not newly_red and cur_pass >= base_pass:
        return None
    if newly_red:
        detail = ("This compiled, but it REGRESSED previously-passing test(s): "
                  f"{sorted(newly_red)} (cluster pass count {base_pass}→"
                  f"{cur_pass}).\nKeep those tests passing while fixing the "
                  "target — do not change shared behavior they rely on.")
    else:
        detail = (f"This dropped the cluster pass count {base_pass}→"
                  f"{cur_pass} (tests that used to run/pass no longer do). "
                  "Revert the change that removed them.")
    return frozenset(newly_red), detail


def u2_test_gate(
    run_cargo_fn: RunCargo,
    fn_tests: set[str],
    test_cmd: str | None,
    target_src_dir: Path,
    cluster_env: dict[str, str] | None,
    *,
    enabled: bool,
    timeout: int,
    err_cap: int,
    guard: Callable[[str, str], None],
) -> tuple[frozenset[str], str] | None:
    """Plan 10 — the SINGLE reusable test-acceptance gate, called by every fill
    loop (per-fn, per-impl, post-merge) at the point a unit compiles.

    Runs the unit's own tests (`fn_tests`) via `run_scoped_tests` and decides:
      - None  → ACCEPT (tests pass, gate off, no tests, or inconclusive run —
        never block on uncertainty).
      - (fail_sig, detail) → DO NOT ACCEPT: compiled but its own tests fail.
        `fail_sig` is the failing-test set (stall signature → loop terminates),
        `detail` is the assertion/panic text for the next prompt.
    Fail-safe: any error → None (fall back to compile-based acceptance).
    """
    if not (enabled and test_cmd and fn_tests):
        return None
    try:
        _passed, _fc, detail, failed = run_scoped_tests(
            run_cargo_fn, test_cmd, target_src_dir, cluster_env or {},
            timeout=timeout, err_cap=err_cap, only=set(fn_tests))
    except Exception as e:  # noqa: BLE001 — fail-safe to compile-based accept
        guard("phase-c/u2_skip", f"{type(e).__name__}: {e}")
        return None
    still = set(fn_tests) & failed
    if not still:
        return None
    msg = (f"This compiled, but these tests still FAIL: {sorted(still)}\n\n"
           "Fix the BEHAVIOR so they pass.\n" + detail)
    return frozenset(still), msg


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
