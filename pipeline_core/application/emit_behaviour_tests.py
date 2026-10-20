"""Captured behaviours -> real `#[test]`s in `tests/`, VERIFIED before they land.

WHY THIS EXISTS. Until this module, a captured behaviour could not reach the
oracle at all. The suite is read as PROMPT TEXT — `render_behaviour_list` for
t0c, `_fmt_behaviours` for t1.5 — and by the exercises writer; nothing turned a
`BehaviourSpec` into Rust test source, so an observable that no authored test
asserted stayed a note about the crate rather than a check on it. Verified
across `pipeline_core/` before this was written: every consumer of `spec.then`
rendered prose or compared captured output.

WHY IT IS VERIFIED HERE AND NOT BY `judge_block_contract`. t0c's admission
exists to catch a model GUESSING a value: it refuses `contains(...)`, refuses a
block that pins no listed behaviour, and the runtime gate refuses a wrong
literal. A capture's expected value is what the crate printed, so the pin
contract's premise (this value is a guess) is false here and re-judging it can
only refuse something already proven. What is NOT false is the other half of
`_engine_runtime_gate`'s reason for existing, quoted from its docstring:
"a compiling block whose assertion is simply wrong enters the reconstruction
oracle as an entry no fill can ever satisfy, and the fns it covers are
re-attempted for the life of the project". Emission without execution re-opens
exactly that. Measured 2026-08-27 against a pristine copy of the original, two
ways for a capture to land red with nothing to catch it:

  * a value carrying per-line trailing whitespace was stored through
    `normalize_stdout` and asserted against the raw `format!` — the emitted
    `#[test]` failed on the very tree it was captured from (fixed at the root
    by `printed_value`, and this gate is what would have caught it);
  * `err.backtrace().to_string()` captured `"disabled backtrace"`, passed under
    the capture env and FAILED under `RUST_BACKTRACE=1` — run-twice cannot see
    this, because the value is perfectly stable at any fixed env.

So every emitted file is compiled and run twice: once under the capture env,
once under a PERTURBED one. A capture that fails the capture-env run is not a
specification and is dropped. A capture whose value MOVES under the perturb
is recaptured in that world and emitted as two tests (one per env), each
carrying `required_env` so the reconstruction sweep is in the same world.
A mover that cannot be recaptured is still withdrawn. There is no third
state where an env-moving capture quietly stays as a single Then.

WHY A PER-CLUSTER STEM, AND WHY THE FILE IS CFG-GATED. `tests/` is symlinked
into every cluster's filled tree (`task2_generator`, `[skeleton-C]`), so an
ungated file written while harvesting one cluster is ACTIVE in the others. The
deterministic cluster-leak guard next to that symlink only inspects diagnostics
whose path contains `.gen_tests_`, which an integration test does not, so
nothing downstream would catch it. Each cluster writes its own
`tests/behaviour_<cluster>_gen_1.rs` carrying `cluster_cfg_attr`'s predicate as
a crate-level `#![cfg(...)]`, and the file is rewritten in place on a re-entry
rather than accumulating a second copy of the same tests under a new number.

WHY NOT COEXISTENCE BY FILENAME. `read_all_gen_tests` is stem-scoped, so a
capture filed under any stem is invisible to another file's `already_pinned`
and t0c would still demand the pin. The split lives at t0c's listing site
instead; the dedicated stem only keeps the two writers off each other's
`_gen_<N>` numbering, since `next_gen_tests_n` is stem-scoped too.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import suppress
from pathlib import Path

from pipeline_core.domain.behaviour import BehaviourSpec
from pipeline_core.domain.behaviour_probe import (
    capture_moved,
    is_probe_spec,
    narrow_then_to_agreement,
    observed_values,
    render_replay_test,
    split_env_worlds,
)
from pipeline_core.domain.generation import (
    as_integration_test,
    gen_tests_integration_rel,
    is_claimed_pipeline_gen_test,
    parse_gen_tests_integration_filename,
)
from pipeline_core.domain.test_results import parse_binary_results
from pipeline_core.log import emit, log

BEHAVIOUR_TEST_STEM = "behaviour"
# (test binary stem, extra env) -> (process ok, combined stdout+stderr).
Verify = Callable[[str, Mapping[str, str]], tuple[bool, str]]
# (test binary stem, the names to run) -> the names that fail alone.
FailingAlone = Callable[[str, Sequence[str]], Sequence[str]]
# (the emitted file, its body) -> the gate now on it, or None when it does
# not pass in its own world (`application.test_worlds.settle_test_gate`).
Settle = Callable[[Path, str], str | None]
# Recapture Then under an extra env: raw stdout, or None if the program
# failed to run. Injected so the emitter does not own cargo.
Recapture = Callable[[BehaviourSpec, Mapping[str, str]], str | None]
# How many times the file may be re-rendered after dropping what failed. Three
# is a termination bound, not a budget: each round drops at least one capture,
# so the loop ends either green or empty.
MAX_VERIFY_ROUNDS = 3


def behaviour_test_stem(cfg_cluster: str) -> str:
    """`behaviour_<cluster>` — the per-cluster stem. Two clusters harvesting
    into one `tests/` must not write the same file, and must not each write a
    numbered copy of the other's tests either."""
    safe = "".join(c if (c.isalnum() or c == "_") else "_" for c in cfg_cluster)
    return f"{BEHAVIOUR_TEST_STEM}_{safe}" if safe else BEHAVIOUR_TEST_STEM


def replay_fn_name(spec: BehaviourSpec) -> str:
    """A stable, unique Rust fn name for this spec's replay test.

    Derived from the behaviour id so re-emitting the same suite produces the
    same file: an emitter whose output churned would make every sweep's verdict
    vector incomparable with the last."""
    safe = "".join(c if (c.isalnum() or c == "_") else "_" for c in spec.behaviour_id)
    while "__" in safe:
        safe = safe.replace("__", "_")
    return f"behaviour_{safe.strip('_').lower()}"


def replayable(specs: Iterable[BehaviourSpec]) -> list[BehaviourSpec]:
    """The captures that can become a test: a probe spec with a Then that is a
    value rather than a stdout blob. A legacy free-form capture is skipped —
    `render_replay_test` returns "" for it, because there is no single
    expression to assert on."""
    out = []
    for s in specs:
        if not is_probe_spec(s):
            continue
        if render_replay_test(s, fn_name="probe"):
            out.append(s)
    return out


def render_behaviour_test_file(
    specs: Sequence[BehaviourSpec],
    *,
    crate: str,
    cfg_attr: str = "",
) -> str:
    """One integration-test file asserting every replayable capture, gated to
    the cluster it was captured under when `cfg_attr` names a predicate.

    The crate glob is written HERE rather than left to `as_integration_test`,
    whose insertion is guarded by `"use {crate}::" not in body` — a substring
    test over the whole file. One capture whose `given` opens with
    `use anyhow::Context;` (a `use` inside a fn body, perfectly legal) therefore
    suppressed the module-level import for EVERY test in the file, and the file
    stopped compiling. Measured 2026-08-27 on a pristine copy: three captures
    that each ran green as an example, one `use` between them, and the emitted
    binary did not build. Writing the glob first makes that guard a no-op
    instead of a coin flip."""
    bodies = [render_replay_test(s, fn_name=replay_fn_name(s)) for s in specs]
    block = f"#[allow(unused_imports)]\nuse {crate}::*;\n\n" + "\n".join(b for b in bodies if b)
    body = as_integration_test(block, crate=crate)
    return f"#![cfg({cfg_attr})]\n{body}" if cfg_attr else body


def _clear_previous(project_dir: Path, stem: str) -> None:
    """Delete this cluster's previously emitted files so a re-entry rewrites
    rather than accumulates. Only files carrying the writer's origin marker are
    touched — an authored `tests/<stem>_gen_<N>.rs` is the crate's oracle and
    is never ours to remove."""
    tests = Path(project_dir) / "tests"
    if not tests.is_dir():
        return
    for f in sorted(tests.iterdir()):
        parsed = parse_gen_tests_integration_filename(f.name)
        if not parsed or parsed[0] != stem:
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except OSError:
            continue
        if is_claimed_pipeline_gen_test(f.name, text):
            with suppress(OSError):
                f.unlink()


def _free_dest(project_dir: Path, stem: str) -> Path:
    """Where this cluster's file goes: `_gen_1` normally, the first free number
    when something we did not write already sits there.

    `_clear_previous` has removed every file of this stem CARRYING THE ORIGIN
    MARKER, so anything still occupying the name is authored — the crate's own
    oracle — and writing over it would delete a test the reconstruction is
    measured by. The number is otherwise always 1, which is what keeps a
    re-entry rewriting one file instead of stacking a second copy of the same
    tests beside it."""
    n = 1
    while (dest := Path(project_dir) / gen_tests_integration_rel(stem, n)).exists():
        n += 1
    return dest


def _narrow_world_pinned(
    keep: list[BehaviourSpec], failed: frozenset[str], output: str,
) -> tuple[list[BehaviourSpec], list[BehaviourSpec]]:
    """(`keep` with the narrowable specs re-pinned, the ones narrowed).

    A capture that pins its own world REPORTS what it rendered when it fails,
    so a disagreement between the probe binary's render and the test binary's
    can be cut back to the text they share instead of withdrawing the
    capture. The part they disagree on is the part the RENDERING PROCESS owns
    — the frames of a captured backtrace, an address, an elapsed time — and
    two runs of the same probe agree on it exactly, which is why the probe
    cannot find it alone. Run `330242-0be2c3e1` (2026-09-03) withdrew the
    only such capture of each cluster here, at the FIRST verify stage: the
    twin was emitted with the probe's whole render and the test binary that
    ran it was never the binary that captured it.

    Narrowing is strictly shortening, so the caller's round bound ends the
    loop; a capture that shares no leading text with its replay is not
    narrowed and goes on to be withdrawn."""
    seen = observed_values(output)
    if not seen:
        return keep, []
    out: list[BehaviourSpec] = []
    narrowed: list[BehaviourSpec] = []
    for spec in keep:
        fn = replay_fn_name(spec)
        cut = (narrow_then_to_agreement(spec, seen[fn])
               if fn in failed and spec.required_env and fn in seen else None)
        if cut is None:
            out.append(spec)
            continue
        out.append(cut)
        narrowed.append(cut)
    return out, narrowed


def _withdraw(
    keep: list[BehaviourSpec],
    failed: frozenset[str],
    drop: Callable[[BehaviourSpec, str], None],
    reason: str,
) -> tuple[list[BehaviourSpec], list[BehaviourSpec]]:
    """(survivors, withdrawn) for `keep` against a set of failing test names.

    Returning the withdrawn list rather than a count is what lets the caller
    tell "this round made progress" from "the verdict names nothing we can
    remove" — the second is not a round to retry, it is a red file with no
    attribution, and retrying it is how a bounded loop spends its whole budget
    re-rendering the same thing."""
    survivors, went = [], []
    for spec in keep:
        if replay_fn_name(spec) in failed:
            went.append(spec)
            drop(spec, reason)
        else:
            survivors.append(spec)
    return survivors, went


def _verdict_of(output: str) -> tuple[frozenset[str], frozenset[str], bool]:
    """(`names libtest reported as failed`, `names it EXECUTED`, `a verdict was
    reported at all`).

    Read through `parse_binary_results`, the same reader the sweep's oracle
    uses, so "which test failed" is answered by one parser rather than by a
    second hand-rolled scan of cargo's output.

    The trailer is what says a verdict happened, including `running 0 tests` /
    `0 passed; 0 failed`: `passed or failed` reads a completed empty run as
    "did not build", which is what an integration test whose `#![cfg]` excludes
    it compiles as. The executed set is the other half, and the two are only
    useful together — see :func:`_unexecuted`."""
    binaries = parse_binary_results(output)
    failed: set[str] = set()
    executed: set[str] = set()
    saw = False
    for b in binaries:
        failed |= set(b.failed)
        executed |= set(b.passed) | set(b.failed)
        if b.completed:
            saw = True
    return frozenset(failed), frozenset(executed), saw


def _unexecuted(keep: Sequence[BehaviourSpec], executed: frozenset[str]) -> list[BehaviourSpec]:
    """The captures in `keep` whose replay test the run did not report.

    A completed trailer says the binary ran; it does not say it ran OUR tests,
    and treating it as if it did turns a cfg/env mismatch from a loud failure
    into a silent pass. The live shape: `_verify_behaviour_tests` runs
    `cargo test -p <crate> --test <stem>` and passes no `--features`, while a
    `feature/<pred>` cluster contributes no RUSTFLAGS of its own and
    `cluster_cfg_attr` gates the emitted file on
    `any(<pred>, feature = "<pred>")`. That file compiles to `running 0 tests`
    under its own verification, and every capture in it would be admitted with
    the log line claiming it was verified against the original. anyhow does not
    hit it only because its non-default cluster is the custom-cfg one, which
    does set `RUSTFLAGS=--cfg <pred>`.

    libtest names every test in the binary and the verification run passes no
    filter, so on a healthy run this is empty and a non-empty answer means the
    file did not run — never that some subset was skipped."""
    return [s for s in keep if replay_fn_name(s) not in executed]


def emit_behaviour_tests(
    project_dir: Path,
    cfg_cluster: str,
    specs: Iterable[BehaviourSpec],
    *,
    crate: str,
    verify: Verify,
    failing_alone: FailingAlone,
    settle: Settle,
    perturb_env: Mapping[str, str] = (),  # type: ignore[assignment]
    cfg_attr: str = "",
    balanced: Callable[[str], bool] = lambda _s: True,
    recapture: Recapture | None = None,
) -> tuple[Path | None, tuple[str, ...], list[dict[str, str]], tuple[BehaviourSpec, ...]]:
    """Write the replayable captures as one `tests/<stem>_gen_1.rs`, verified.

    Returns `(path or None, dropped behaviour ids, misses, emitted specs)`.
    The caller removes the dropped ids from the suite, upserts the emitted
    specs (capture-world originals with `required_env` set, plus perturb-world
    twins), and writes the misses to the sidecar.

    A capture whose replay disagrees with it at the capture env is not a
    specification. A capture whose value moves under `perturb_env` is
    recaptured in that world (`recapture`) and kept as two tests; without a
    recapture that prints a different Then it is withdrawn — the contract
    that shipped as drop-the-mover, now the fallback when the other world
    cannot be read. Leaving a mover in the suite as a single Then would
    hand t1.5 a MUST that no tree can satisfy.

    ``failing_alone(stem, names)`` names the tests of the green file that do
    not pass alone in a process of their own (`adapters.test_worlds`); their
    captures are withdrawn like a replay disagreement. Both callables are
    required: a default that skipped them admitted the file as written. A
    test that passes
    only beside another test's side effect on process state is not a
    specification of the crate (run `801510-bb22799f`, 2026-10-20: a t0c test
    passed only after a sibling's `set_var`, and task 3 read it red or green
    by scheduling).

    ``settle(dest, body)`` measures the verified file's build worlds and
    rewrites its gate to them (`test_worlds.settle_test_gate`, the reading
    task 0c's blocks and the doc examples take). Before it, the file stayed
    gated to the world it was captured in, so a capture that also replays in
    another world checked nothing there. None from it means the file does
    not pass where it was written, and nothing is kept.

    None with no drops is the expected state for a suite whose captures are all
    legacy free-form blobs. A delimiter-unbalanced render is refused rather
    than written: a broken file in `tests/` does not fail one test, it stops
    the whole crate compiling and every verdict with it."""
    if not callable(failing_alone) or not callable(settle):
        raise TypeError(
            "emit_behaviour_tests requires the alone check and the world settle")
    stem = behaviour_test_stem(cfg_cluster)
    _clear_previous(project_dir, stem)
    keep = replayable(specs)
    misses: list[dict[str, str]] = []
    dropped: list[str] = []
    if not keep:
        return None, (), misses, ()

    def _drop(spec: BehaviourSpec, reason: str) -> None:
        dropped.append(spec.behaviour_id)
        misses.append({"unit": spec.behaviour_id, "reason": reason, "when": spec.when})

    (Path(project_dir) / "tests").mkdir(parents=True, exist_ok=True)
    dest = _free_dest(project_dir, stem)

    def _withdraw_unrun(missing: list[BehaviourSpec], *, perturbed: bool) -> None:
        """Fail closed on a completed run that did not execute the emitted
        tests. Nothing was verified, so nothing may be admitted — and it is
        withdrawn under its own reason rather than the build failure's, because
        the file compiled fine and the fix is a cfg/env one."""
        which = "environment-perturbed replay" if perturbed else "replay"
        log(
            f"  [task0d] cluster {cfg_cluster}: the {which} completed without running "
            f"{len(missing)} of {len(keep)} emitted test(s) — the file's cfg does not hold "
            f"under the verification env; {len(keep)} capture(s) withdrawn."
        )
        emit(
            "task0d.behaviour_tests_not_executed",
            data={
                "cluster": cfg_cluster,
                "captures": len(keep),
                "not_executed": len(missing),
                "perturbed": perturbed,
            },
        )
        with suppress(OSError):
            dest.unlink()
        for s in keep:
            _drop(s, f"{which} completed but did not run the emitted test — not verified")

    # The file is on disk from `write_text` onwards, and it is only correct to
    # leave one there that a verification run has been seen green. Every
    # ordinary exit below already removes it; an EXCEPTION did not, and
    # `run_task0d` catches whatever this raises, logs `non-fatal, suite kept`
    # and then saves the suite — so a raise here used to leave an unverified
    # `tests/` file in the crate AND every capture still listed as a MUST.
    try:
        for _round in range(MAX_VERIFY_ROUNDS):
            body = render_behaviour_test_file(keep, crate=crate, cfg_attr=cfg_attr)
            if not balanced(body):
                log(
                    f"  [task0d] cluster {cfg_cluster}: behaviour test file REFUSED "
                    f"(delimiters unbalanced) — {len(keep)} capture(s) not emitted."
                )
                emit("task0d.behaviour_tests_refused", data={"cluster": cfg_cluster, "captures": len(keep)})
                with suppress(OSError):
                    dest.unlink()
                for s in keep:
                    _drop(s, "replay refused: delimiters unbalanced")
                return None, tuple(dropped), misses, ()
            dest.write_text(body, encoding="utf-8")

            ok, out = verify(dest.stem, {})
            failed, executed, saw_verdict = _verdict_of(out)
            if not saw_verdict:
                # No verdict at all: the file did not compile, or the binary died
                # before libtest reported anything. Nothing can be attributed to a
                # single capture, so none of them may stay — a tests/ file that does
                # not build takes the whole crate's oracle with it.
                log(
                    f"  [task0d] cluster {cfg_cluster}: emitted behaviour tests produced no "
                    f"verdict (rc ok={ok}) — {len(keep)} capture(s) withdrawn. "
                    f"{out.strip()[-400:]}"
                )
                emit("task0d.behaviour_tests_unverifiable", data={"cluster": cfg_cluster, "captures": len(keep)})
                with suppress(OSError):
                    dest.unlink()
                for s in keep:
                    _drop(s, "replay did not build or reported no verdict")
                return None, tuple(dropped), misses, ()
            if failed:
                keep, _narrowed = _narrow_world_pinned(keep, failed, out)
                if _narrowed:
                    log(
                        f"  [task0d] cluster {cfg_cluster}: narrowed "
                        f"{len(_narrowed)} world-pinned capture(s) to the text "
                        "the capture and the replay share — the rendering "
                        "process owns the rest."
                    )
                    continue
                keep, went = _withdraw(keep, failed, _drop, "replay disagreed with the capture")
                log(
                    f"  [task0d] cluster {cfg_cluster}: {len(went)} capture(s) did not "
                    f"reproduce as a test against the original and were withdrawn."
                )
                if keep and went:
                    continue
                # Nothing left, or a red verdict that names no capture in this file
                # — which cannot be made green by dropping anything. Either way the
                # file does not stay.
                if keep:
                    for s in keep:
                        _drop(s, "replay red on a verdict naming no capture in the file")
                with suppress(OSError):
                    dest.unlink()
                return None, tuple(dropped), misses, ()
            if unrun := _unexecuted(keep, executed):
                _withdraw_unrun(unrun, perturbed=False)
                return None, tuple(dropped), misses, ()
            alone = frozenset(failing_alone(dest.stem, sorted(executed)))
            if alone:
                keep, went = _withdraw(
                    keep, alone, _drop,
                    "passes only beside the file's other tests: red "
                    "when run alone in its own process")
                log(
                    f"  [task0d] cluster {cfg_cluster}: {len(went)} "
                    "capture(s) pass only beside the file's other tests "
                    "and were withdrawn.")
                if keep and went:
                    continue
                with suppress(OSError):
                    dest.unlink()
                for s in keep:
                    _drop(s, "replay red alone on a test naming no capture")
                return None, tuple(dropped), misses, ()

            # Green under the capture env. Now the axis run-twice cannot see: the
            # same binary under a different environment. A capture whose value
            # moves with `RUST_BACKTRACE` is stable on every run and still not a
            # specification of the CRATE.
            if perturb_env:
                _ok2, out2 = verify(dest.stem, perturb_env)
                failed2, executed2, saw2 = _verdict_of(out2)
                if not saw2:
                    # Same fail-closed rule as the capture-env run: no trailer
                    # cannot rule env-dependence out, and keeping the file would
                    # admit a capture whose value may move with the environment
                    # as a specification of the crate.
                    log(
                        f"  [task0d] cluster {cfg_cluster}: the environment-perturbed "
                        f"replay reported no verdict — {len(keep)} capture(s) withdrawn."
                    )
                    emit(
                        "task0d.behaviour_tests_unverifiable",
                        data={"cluster": cfg_cluster, "captures": len(keep), "perturbed": True},
                    )
                    with suppress(OSError):
                        dest.unlink()
                    for s in keep:
                        _drop(s, "perturbed replay reported no verdict — environment-dependence not ruled out")
                    return None, tuple(dropped), misses, ()
                if failed2:
                    still: list[BehaviourSpec] = []
                    env_went: list[BehaviourSpec] = []
                    split = False
                    env_reason = f"environment-dependent: the observed value moves under {dict(perturb_env)}"
                    for spec in keep:
                        if replay_fn_name(spec) not in failed2:
                            still.append(spec)
                            continue
                        if spec.required_env:
                            # Already pinned to one world. A red under this
                            # extra is a replay disagreement in that world
                            # only when the extra IS that world; otherwise
                            # set_var should have isolated it.
                            if dict(spec.required_env) == dict(perturb_env):
                                env_went.append(spec)
                                _drop(spec, "replay disagreed with the capture")
                            else:
                                still.append(spec)
                            continue
                        # TWICE, deliberately: the twin's Then may only pin
                        # what two independent processes printed. A render
                        # whose tail names the rendering process (a captured
                        # backtrace's frames) agrees on its head and nothing
                        # else, and `split_env_worlds` writes that head as a
                        # prefix Then instead of an `assert_eq!` that is red
                        # by construction in the test binary (run
                        # `330242-0be2c3e1`, 2026-09-03: the only Debug probe
                        # of each cluster withdrawn as "replay disagreed with
                        # the capture").
                        outs = [recapture(spec, perturb_env)
                                for _ in range(2)] if recapture is not None else []
                        stdouts = [o for o in outs if o is not None]
                        if not stdouts or not capture_moved(spec, stdouts[0]):
                            env_went.append(spec)
                            _drop(spec, env_reason)
                            continue
                        worlds = split_env_worlds(spec, stdouts, perturb_env)
                        if worlds is None:
                            env_went.append(spec)
                            _drop(spec, "the perturbed world's recaptures "
                                        "agreed on no leading text — nothing "
                                        "in this render is the crate's")
                            continue
                        orig, twin = worlds
                        still.append(orig)
                        still.append(twin)
                        split = True
                    keep = still
                    if split:
                        log(
                            f"  [task0d] cluster {cfg_cluster}: recaptured "
                            f"{sum(1 for s in keep if s.behaviour_id.endswith('::env'))} "
                            f"capture(s) under the perturbed environment as a second world."
                        )
                    if env_went:
                        log(
                            f"  [task0d] cluster {cfg_cluster}: {len(env_went)} capture(s) are "
                            "environment-dependent and were withdrawn."
                        )
                    if split and keep:
                        continue
                    if keep and env_went:
                        continue
                    if keep:
                        for s in keep:
                            _drop(s, "perturbed replay red on a verdict naming no capture in the file")
                    with suppress(OSError):
                        dest.unlink()
                    return None, tuple(dropped), misses, ()
                if unrun := _unexecuted(keep, executed2):
                    _withdraw_unrun(unrun, perturbed=True)
                    return None, tuple(dropped), misses, ()

            if settle(dest, body) is None:
                with suppress(OSError):
                    dest.unlink()
                for s in keep:
                    _drop(s, "does not pass in the world it was captured in "
                          "when run by the world settle")
                return None, tuple(dropped), misses, ()
            log(
                f"  [task0d] cluster {cfg_cluster}: {len(keep)} captured behaviour(s) "
                f"emitted as {dest.name}, verified against the original"
                f"{' and under a perturbed environment' if perturb_env else ''} — "
                f"observed values, not guessed ones."
            )
            emit(
                "task0d.behaviour_tests_emitted",
                data={"cluster": cfg_cluster, "captures": len(keep), "file": dest.name, "withdrawn": len(dropped)},
            )
            return dest, tuple(dropped), misses, tuple(keep)

        # Every round dropped something and the budget ran out with the file still
        # red: withdraw the rest rather than leave a failing test in the oracle.
        log(
            f"  [task0d] cluster {cfg_cluster}: behaviour tests still red after "
            f"{MAX_VERIFY_ROUNDS} round(s) — {len(keep)} capture(s) withdrawn."
        )
        with suppress(OSError):
            dest.unlink()
        for s in keep:
            _drop(s, "replay still red after the verification budget")
        return None, tuple(dropped), misses, ()
    except BaseException:
        with suppress(OSError):
            dest.unlink()
        raise
