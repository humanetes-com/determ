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

WHY ONE TEST BINARY PER ENVIRONMENT WORLD. std reads the backtrace
environment once per process, on the first capture, and caches the answer,
so a test's own `set_var` holds only when no test in the same process
captured first. With every capture of a cluster in one file, a pinned
capture's verdict was decided by test order: run `1878910-be648607`
(2026-10-31) rendered `disabled` for every `::env` twin, and run
`2426367-f566edfc` read the same eight originals green in one emission and
red in the next. Each distinct `required_env` therefore gets a file of its
own (`world_files`), every test of which sets that env first, so whichever
thread captures first has already set it, whatever env the harness started
the binary under. The sweep, task 3 and coverage run those files as they
run any other.

WHY NOT COEXISTENCE BY FILENAME. `read_all_gen_tests` is stem-scoped, so a
capture filed under any stem is invisible to another file's `already_pinned`
and t0c would still demand the pin. The split lives at t0c's listing site
instead; the dedicated stem only keeps the two writers off each other's
`_gen_<N>` numbering, since `next_gen_tests_n` is stem-scoped too.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from pipeline_core.domain.behaviour import BehaviourSpec
from pipeline_core.domain.behaviour_probe import (
    ProbeBuild,
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
from pipeline_core.domain.rust_scan import rust_delimiters_balanced
from pipeline_core.domain.test_results import (
    parse_binary_results,
    parse_rustc_compile_errors,
    split_test_stdout_sections,
)
from pipeline_core.log import emit, log

BEHAVIOUR_TEST_STEM = "behaviour"
# (test binary stem, extra env) -> (process ok, combined stdout+stderr).
Verify = Callable[[str, Mapping[str, str]], tuple[bool, str]]
# (test binary stem, the names to run, extra env over the cluster's) -> the
# names that fail alone, each in a process of its own under that env.
FailingAlone = Callable[[str, Sequence[str], Mapping[str, str]], Sequence[str]]
# (the emitted file, its body) -> the gate now on it, or None when it does
# not pass in its own world (`application.test_worlds.settle_test_gate`).
Settle = Callable[[Path, str], str | None]
# Recapture Then under an extra env and one build of the original: raw
# stdout, or None if the program failed to run. Injected so the emitter does
# not own cargo.
Recapture = Callable[[BehaviourSpec, Mapping[str, str], ProbeBuild], str | None]


def verify_progress(keep: Sequence[BehaviourSpec]) -> tuple[int, int, int]:
    """(unpinned captures, captures, total length of the pinned Thens).

    The verify loop re-renders only after a round that lowered this
    lexicographically, so it ends without a round count. A world split pins
    both halves (`split_env_worlds`) and a pinned capture never splits again,
    so the first part falls although the file grows; a withdrawal lowers the
    second; a narrowing strictly shortens a pinned Then
    (`narrow_then_to_agreement` returns None on an equal prefix) and keeps
    the pin. A count of rounds is what this replaces: on `1878910-be648607`
    the third round withdrew 8 twins, the count was spent, and the 22 and 24
    captures that round had passed were stamped red with no reading."""
    pinned = [s for s in keep if s.required_env]
    return (len(keep) - len(pinned), len(keep),
            sum(len(s.then.value) for s in pinned
                if s.then is not None and s.then.value))


def behaviour_test_stem(cfg_cluster: str) -> str:
    """`behaviour_<cluster>` — the per-cluster stem. Two clusters harvesting
    into one `tests/` must not write the same file, and must not each write a
    numbered copy of the other's tests either."""
    safe = "".join(c if (c.isalnum() or c == "_") else "_" for c in cfg_cluster)
    return f"{BEHAVIOUR_TEST_STEM}_{safe}" if safe else BEHAVIOUR_TEST_STEM


_WORLD = "_world_"


def world_stem(cfg_cluster: str, required_env: Iterable[tuple[str, str]]) -> str:
    """The test binary a capture pinned to `required_env` replays in:
    `behaviour_<cluster>` when it pins nothing, else
    `behaviour_<cluster>_world_<key>_<value>…_<digest>`, keys sorted.

    One binary holds one world because the observable env is read once per
    process (module docstring). The digest is of the pairs themselves, so two
    worlds whose readable parts sanitize alike still get two binaries, and
    the same world gets the same name on every emission."""
    base = behaviour_test_stem(cfg_cluster)
    pairs = sorted(dict(required_env).items())
    if not pairs:
        return base
    readable = "".join(c if c.isalnum() else "_"
                       for c in "_".join(f"{k}_{v}" for k, v in pairs).lower())
    while "__" in readable:
        readable = readable.replace("__", "_")
    digest = hashlib.sha256(repr(pairs).encode("utf-8")).hexdigest()[:8]
    return f"{base}{_WORLD}{readable.strip('_')}_{digest}"


def is_world_stem_of(stem: str, cfg_cluster: str) -> bool:
    """True when `stem` is one of `cfg_cluster`'s behaviour binaries: its base
    stem or a :func:`world_stem`. The shape is matched whole, so the base
    stem of a cluster whose name merely starts with `<cluster>_world_` is
    not taken for one of this cluster's worlds."""
    base = behaviour_test_stem(cfg_cluster)
    return stem == base or re.fullmatch(
        re.escape(base + _WORLD) + r"[a-z0-9_]*[a-z0-9]_[0-9a-f]{8}", stem) is not None


def world_files(keep: Sequence[BehaviourSpec], cfg_cluster: str) -> dict[str, list[BehaviourSpec]]:
    """Stem -> the captures replayed in that binary, stems sorted (the base
    stem first). The one place a capture is given a test binary: every
    render of the emitter goes through it, so a pinned capture cannot land
    beside a capture of another world."""
    out: dict[str, list[BehaviourSpec]] = {}
    for spec in keep:
        out.setdefault(world_stem(cfg_cluster, spec.required_env), []).append(spec)
    return dict(sorted(out.items()))


def replay_fn_name(spec: BehaviourSpec) -> str:
    """A stable, unique Rust fn name for this spec's replay test.

    Derived from the behaviour id so re-emitting the same suite produces the
    same file: an emitter whose output churned would make every sweep's verdict
    vector incomparable with the last."""
    safe = "".join(c if (c.isalnum() or c == "_") else "_" for c in spec.behaviour_id)
    while "__" in safe:
        safe = safe.replace("__", "_")
    return f"behaviour_{safe.strip('_').lower()}"


def replay_failure_section(output: str, fn_name: str, *, limit: int = 800) -> str:
    """`fn_name`'s own failure block in a verify run's output, or "".

    libtest prints the `test <name> ... FAILED` status lines first and each
    failing test's captured output later, under `---- <name> stdout ----`.
    The block is where an ordinary `assert_eq!` prints left and right and a
    world-pinned replay prints its rendered text, so it is read through the
    one splitter of those blocks (`split_test_stdout_sections`). A reader that
    started at the first line naming the test, as this one did until
    2026-10-26, recorded the status line alone for every failure but the last
    and every other test's block for the last. Bounded so a cargo log is not
    copied whole into the miss record."""
    if not output or not fn_name:
        return ""
    for name, body in split_test_stdout_sections(output):
        if name == fn_name:
            return body.strip()[:limit]
    return ""


def _balanced_replay_blocks(
    specs: Sequence[BehaviourSpec], balanced: Callable[[str], bool],
) -> tuple[list[BehaviourSpec], list[BehaviourSpec]]:
    """(blocks the check accepts, blocks it refuses).

    One capture is judged on its own replay text. The assembled file is a
    separate check: a block set can still be wrapped in a file the same
    check refuses, and that file is not written."""
    good: list[BehaviourSpec] = []
    bad: list[BehaviourSpec] = []
    for spec in specs:
        block = render_replay_test(spec, fn_name=replay_fn_name(spec))
        if block and not balanced(block):
            bad.append(spec)
        else:
            good.append(spec)
    return good, bad


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


def capture_line_spans(body: str, specs: Sequence[BehaviourSpec]) -> dict[str, tuple[int, int]]:
    """Each capture's test in the rendered file: fn name -> (first line, last
    line), 1-based as rustc numbers them. A capture's span runs from its
    attributes to the line before the next capture's; the file's shared
    header (the gate, the crate glob) sits in no span."""
    names = {replay_fn_name(s) for s in specs}
    lines = body.split("\n")
    starts: list[tuple[int, str]] = []
    for i, line in enumerate(lines):
        head = line.strip()
        if head.startswith("pub "):
            head = head[4:].lstrip()
        if not head.startswith("fn "):
            continue
        name = head[3:].split("(", 1)[0].strip()
        if name not in names:
            continue
        start = i
        while start > 0 and lines[start - 1].strip().startswith("#["):
            start -= 1
        starts.append((start + 1, name))
    spans: dict[str, tuple[int, int]] = {}
    for k, (start, name) in enumerate(starts):
        end = starts[k + 1][0] - 1 if k + 1 < len(starts) else len(lines)
        spans[name] = (start, end)
    return spans


def no_verdict_culprits(
    output: str,
    keep: Sequence[BehaviourSpec],
    body: str,
    dest: Path,
    executed: frozenset[str],
    alone: Callable[[str, Sequence[str]], Sequence[str]],
) -> list[tuple[BehaviourSpec, str]]:
    """The captures a verify round that reported no verdict can be put on,
    each with its reason; [] when the reading names none.

    A file that does not build, or a binary that dies, used to withdraw every
    capture in it on the ground that nothing could be attributed to one. Both
    can be: rustc names the line of each error in the emitted file, and a test
    binary that compiled can run each unreported test alone. Run
    `15370-6a4cfd45` (2026-10-30) lost 24 stable and 26 nightly captures to one
    mis-rendered assertion each (`tests/behaviour_stable_gen_1.rs:96`), run
    `2129106-ead996ab` 52.

    Located compile errors decide first: the captures whose span holds one.
    An error outside every span (the shared header) names no capture. With no
    compile error at all, the captures the round did not report are run alone
    under the round's env (`alone`) and the ones that fail are the culprits.
    A build that failed without a located error is read as naming nobody, so
    the caller withdraws the file as before."""
    diags = parse_rustc_compile_errors(output)
    if diags or "could not compile" in output:
        spans = capture_line_spans(body, keep)
        here = [d for d in diags if Path(d.file).name == dest.name]
        out: list[tuple[BehaviourSpec, str]] = []
        for spec in keep:
            lo, hi = spans.get(replay_fn_name(spec), (0, -1))
            hit = [d for d in here if lo <= d.line <= hi]
            if hit:
                out.append((spec, f"replay did not compile: {hit[0].rendered()}"))
        return out
    unreported = [replay_fn_name(s) for s in keep if replay_fn_name(s) not in executed]
    if not unreported:
        return []
    red = frozenset(alone(dest.stem, unreported))
    return [(s, "replay binary died; red when run alone in its own process")
            for s in keep if replay_fn_name(s) in red]


def _clear_previous(project_dir: Path, cfg_cluster: str) -> None:
    """Delete this cluster's previously emitted files, its base file and
    every world file, so a re-entry rewrites rather than accumulates and a
    world that no capture pins any more leaves no binary behind. Only files
    carrying the writer's origin marker are touched — an authored
    `tests/<stem>_gen_<N>.rs` is the crate's oracle and is never ours to
    remove."""
    tests = Path(project_dir) / "tests"
    if not tests.is_dir():
        return
    for f in sorted(tests.iterdir()):
        parsed = parse_gen_tests_integration_filename(f.name)
        if not parsed or not is_world_stem_of(parsed[0], cfg_cluster):
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

    Narrowing is strictly shortening and keeps the pin, so it lowers
    :func:`verify_progress`; a capture that shares no leading text with its replay is not
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
    attribution, and retrying it re-renders the same thing (the loop's
    progress check, :func:`verify_progress`, would refuse that round)."""
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




@dataclass(frozen=True)
class _Reading:
    """One test binary's verify run: process ok, the output, the names
    libtest failed and executed, and whether it reported a verdict."""

    ok: bool
    out: str
    failed: frozenset[str]
    executed: frozenset[str]
    saw: bool


def _read(run: tuple[bool, str]) -> _Reading:
    ok, out = run
    failed, executed, saw = _verdict_of(out)
    return _Reading(ok, out, failed, executed, saw)


def _union(sets: Iterable[Iterable[str]]) -> frozenset[str]:
    out: set[str] = set()
    for names in sets:
        out |= set(names)
    return frozenset(out)


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
    balanced: Callable[[str], bool] = rust_delimiters_balanced,
    recapture: Recapture | None = None,
) -> tuple[tuple[Path, ...], tuple[str, ...], list[dict[str, str]], tuple[BehaviourSpec, ...]]:
    """Write the replayable captures as one `tests/<stem>_gen_1.rs` per
    environment world (`world_files`), verified.

    Returns `(paths, dropped behaviour ids, misses, emitted specs)`; `paths`
    is empty when nothing was emitted. The caller removes the dropped ids
    from the suite, upserts the emitted specs (capture-world originals with
    `required_env` set, plus perturb-world twins), and writes the misses to
    the sidecar.

    A capture whose replay disagrees with it in its own world's binary is
    not a specification. A capture whose value moves under `perturb_env` is
    recaptured in that world under every :class:`ProbeBuild` of the original
    (`recapture`), and kept as two tests in two binaries, the twin pinning
    only what the builds agreed on; without a recapture under every build
    that prints a different Then it is withdrawn. Leaving a mover in the
    suite as a single Then would hand t1.5 a MUST that no tree can satisfy.

    ``failing_alone(stem, names, extra)`` names the tests of a green binary
    that do not pass alone in a process of their own
    (`adapters.test_worlds`); their captures are withdrawn like a replay
    disagreement. Both callables are required: a default that skipped them
    admitted the file as written. A test that passes only beside another
    test's side effect on process state is not a specification of the crate
    (run `801510-bb22799f`, 2026-10-20: a t0c test passed only after a
    sibling's `set_var`, and task 3 read it red or green by scheduling).

    ``settle(dest, body)`` measures each verified file's build worlds and
    rewrites its gate to them (`test_worlds.settle_test_gate`, the reading
    task 0c's blocks and the doc examples take). None from it means that
    file does not pass where it was written, and its captures go; the other
    files are separate binaries and stay.

    A file that reports no verdict, a red verdict naming none of its
    captures, or a run that did not execute its tests withdraws that file's
    captures, never another file's: each binary is built and run on its own.

    None with no drops is the expected state for a suite whose captures are
    all legacy free-form blobs. A delimiter-unbalanced render is refused
    rather than written: a broken file in `tests/` does not fail one test,
    it stops the whole crate compiling and every verdict with it."""
    if not callable(failing_alone) or not callable(settle):
        raise TypeError(
            "emit_behaviour_tests requires the alone check and the world settle")
    _clear_previous(project_dir, cfg_cluster)
    keep = replayable(specs)
    misses: list[dict[str, str]] = []
    dropped: list[str] = []
    if not keep:
        return (), (), misses, ()

    # The verify output of the round a drop is charged to. A verdict drop
    # can name a capture that round passed (the "naming no capture" exits
    # take whoever remains); `replay_failed` says which, and the assertion
    # text is kept only for a name that round failed.
    round_out = ""
    round_failed: frozenset[str] | None = None

    def _drop(spec: BehaviourSpec, reason: str, *, verdict: bool = False) -> None:
        row = {"unit": spec.behaviour_id, "reason": reason, "when": spec.when}
        if verdict and round_failed is not None:
            name = replay_fn_name(spec)
            failed_this = name in round_failed
            row["replay_failed"] = "yes" if failed_this else "no"
            section = replay_failure_section(round_out, name) if failed_this else ""
            if section:
                row["replay_output"] = section
        dropped.append(spec.behaviour_id)
        misses.append(row)

    (Path(project_dir) / "tests").mkdir(parents=True, exist_ok=True)
    dests: dict[str, Path] = {}

    def _dest(stem: str) -> Path:
        if stem not in dests:
            dests[stem] = _free_dest(project_dir, stem)
        return dests[stem]

    def _unlink(stem: str) -> None:
        path = dests.pop(stem, None)
        if path is not None:
            with suppress(OSError):
                path.unlink()

    def _unlink_all() -> None:
        for stem in list(dests):
            _unlink(stem)

    def _withdraw_file(stem: str, files: Mapping[str, list[BehaviourSpec]],
                       reason: str, *, verdict: bool = False) -> None:
        """Every capture of one binary goes and its file is removed."""
        nonlocal keep
        gone = {replay_fn_name(s) for s in files[stem]}
        for s in files[stem]:
            _drop(s, reason, verdict=verdict)
        keep = [s for s in keep if replay_fn_name(s) not in gone]
        _unlink(stem)

    def _which(perturbed: bool) -> str:
        return "environment-perturbed replay" if perturbed else "replay"

    def _no_verdict(readings: Mapping[str, _Reading], files: Mapping[str, list[BehaviourSpec]],
                    bodies: Mapping[str, str], extra: Mapping[str, str], *, perturbed: bool) -> bool:
        """Settle the binaries that reported no verdict; False when none did.

        The binary did not build, or died before libtest reported. The
        captures the reading locates in that file go and the rest are
        rendered again (`no_verdict_culprits`). A reading that names none
        withdraws that file: a `tests/` file that does not build takes the
        whole crate's oracle with it, and a perturbed run without a trailer
        cannot rule environment-dependence out."""
        nonlocal keep
        silent = [stem for stem, r in readings.items() if not r.saw]
        for stem in silent:
            r = readings[stem]
            culprits = no_verdict_culprits(
                r.out, files[stem], bodies[stem], dests[stem], r.executed,
                lambda st, names: failing_alone(st, names, extra))
            if culprits:
                gone = {replay_fn_name(s) for s, _why in culprits}
                log(
                    f"  [task0d] cluster {cfg_cluster}: the {_which(perturbed)} of "
                    f"{dests[stem].name} reported no verdict — {len(gone)} capture(s) "
                    f"located as the cause and withdrawn ({culprits[0][1]}); "
                    f"{len(files[stem]) - len(gone)} rendered again."
                )
                emit(
                    "task0d.behaviour_tests_located",
                    data={"cluster": cfg_cluster, "file": dests[stem].name,
                          "withdrawn": len(gone), "kept": len(files[stem]) - len(gone),
                          "perturbed": perturbed, "first": culprits[0][1][:300]},
                )
                for spec, why in culprits:
                    _drop(spec, why)
                keep = [s for s in keep if replay_fn_name(s) not in gone]
                continue
            log(
                f"  [task0d] cluster {cfg_cluster}: the {_which(perturbed)} of "
                f"{dests[stem].name} reported no verdict (rc ok={r.ok}) — "
                f"{len(files[stem])} capture(s) withdrawn. {r.out.strip()[-400:]}"
            )
            emit("task0d.behaviour_tests_unverifiable",
                 data={"cluster": cfg_cluster, "file": dests[stem].name,
                       "captures": len(files[stem]), "perturbed": perturbed})
            _withdraw_file(
                stem, files,
                "perturbed replay reported no verdict — environment-dependence not ruled out"
                if perturbed else "replay did not build or reported no verdict")
        return bool(silent)

    def _unrun(readings: Mapping[str, _Reading], files: Mapping[str, list[BehaviourSpec]],
               *, perturbed: bool) -> bool:
        """Fail closed on a completed run that did not execute a file's
        tests; False when every file ran all of its own. Nothing in that
        file was verified, so nothing of it may be admitted — and it goes
        under its own reason rather than the build failure's, because the
        file compiled fine and the fix is a cfg/env one."""
        hit = False
        for stem, r in readings.items():
            missing = _unexecuted(files[stem], r.executed)
            if not missing:
                continue
            hit = True
            log(
                f"  [task0d] cluster {cfg_cluster}: the {_which(perturbed)} of "
                f"{dests[stem].name} completed without running {len(missing)} of "
                f"{len(files[stem])} emitted test(s) — the file's cfg does not hold "
                f"under the verification env; {len(files[stem])} capture(s) withdrawn."
            )
            emit(
                "task0d.behaviour_tests_not_executed",
                data={"cluster": cfg_cluster, "file": dests[stem].name,
                      "captures": len(files[stem]), "not_executed": len(missing),
                      "perturbed": perturbed},
            )
            _withdraw_file(stem, files,
                           f"{_which(perturbed)} completed but did not run the emitted test — not verified")
        return hit

    def _nameless(readings: Mapping[str, _Reading], files: Mapping[str, list[BehaviourSpec]],
                  failed: frozenset[str]) -> list[str]:
        """The binaries whose red verdict names none of their own captures —
        which cannot be made green by dropping anything, so the file goes."""
        out = []
        for stem, r in readings.items():
            mine = {replay_fn_name(s) for s in files[stem]}
            if r.failed & failed and not (r.failed & mine):
                out.append(stem)
        return out

    def _none_left() -> tuple[tuple[Path, ...], tuple[str, ...], list[dict[str, str]], tuple[BehaviourSpec, ...]]:
        _unlink_all()
        return (), tuple(dropped), misses, ()

    # Every file is on disk from `write_text` onwards, and it is only correct
    # to leave one there that a verification run has been seen green. Every
    # ordinary exit below already removes it; an EXCEPTION did not, and
    # `run_task0d` catches whatever this raises, logs `non-fatal, suite kept`
    # and then saves the suite — so a raise here used to leave an unverified
    # `tests/` file in the crate AND every capture still listed as a MUST.
    try:
        progress: tuple[int, int, int] | None = None
        last_round = ""
        while True:
            # A round re-renders only after the one before it made progress.
            # A branch that continues without it is a defect of that branch:
            # loud, bounded, and nothing stamped red without a reading.
            if not keep:
                return _none_left()
            now = verify_progress(keep)
            if progress is not None and not now < progress:
                log(
                    f"  [task0d] cluster {cfg_cluster}: HEALTH ‼ "
                    f"BEHAVIOUR_VERIFY_NO_PROGRESS — the {last_round} round "
                    "continued without withdrawing, splitting or narrowing a "
                    f"capture; {len(keep)} capture(s) withdrawn unverified."
                )
                emit("task0d.behaviour_verify_no_progress",
                     data={"cluster": cfg_cluster, "branch": last_round,
                           "captures": len(keep)})
                for s in keep:
                    _drop(s, "not verified: the round made no progress")
                return _none_left()
            progress = now
            # One capture whose own replay text fails the check is dropped.
            # The others are rendered again, and each assembled file is
            # checked with the same function: a broken tests/ file is still
            # not written.
            good, bad = _balanced_replay_blocks(keep, balanced)
            for s in bad:
                _drop(s, "replay refused: delimiters unbalanced")
            keep = good
            if not keep:
                log(
                    f"  [task0d] cluster {cfg_cluster}: behaviour test file REFUSED "
                    f"(delimiters unbalanced) — every remaining capture's replay "
                    "text failed the check."
                )
                emit("task0d.behaviour_tests_refused", data={"cluster": cfg_cluster, "captures": len(bad)})
                return _none_left()
            files = world_files(keep, cfg_cluster)
            for stale in [st for st in dests if st not in files]:
                _unlink(stale)
            bodies = {st: render_behaviour_test_file(fs, crate=crate, cfg_attr=cfg_attr)
                      for st, fs in files.items()}
            refused = [st for st, b in bodies.items() if not balanced(b)]
            if refused:
                for st in refused:
                    log(
                        f"  [task0d] cluster {cfg_cluster}: behaviour test file REFUSED "
                        f"(delimiters unbalanced) — {len(files[st])} capture(s) of "
                        f"{st} not emitted."
                    )
                    emit("task0d.behaviour_tests_refused",
                         data={"cluster": cfg_cluster, "captures": len(files[st]), "stem": st})
                    _withdraw_file(st, files, "replay refused: delimiters unbalanced")
                last_round = "refused"
                continue
            for st, body in bodies.items():
                _dest(st).write_text(body, encoding="utf-8")

            readings = {st: _read(verify(dests[st].stem, {})) for st in files}
            round_out = "\n".join(r.out for r in readings.values())
            failed = _union(r.failed for r in readings.values())
            round_failed = failed
            if _no_verdict(readings, files, bodies, {}, perturbed=False):
                last_round = "no-verdict"
                continue
            if failed:
                keep, _narrowed = _narrow_world_pinned(keep, failed, round_out)
                if _narrowed:
                    log(
                        f"  [task0d] cluster {cfg_cluster}: narrowed "
                        f"{len(_narrowed)} world-pinned capture(s) to the text "
                        "the capture and the replay share — the rendering "
                        "process owns the rest."
                    )
                    last_round = "narrow"
                    continue
                keep, went = _withdraw(keep, failed, lambda s, why: _drop(s, why, verdict=True),
                                       "replay disagreed with the capture")
                log(
                    f"  [task0d] cluster {cfg_cluster}: {len(went)} capture(s) did not "
                    f"reproduce as a test against the original and were withdrawn."
                )
                for st in _nameless(readings, files, failed):
                    _withdraw_file(st, files, "replay red on a verdict naming no capture in the file",
                                   verdict=True)
                last_round = "withdraw"
                continue
            if _unrun(readings, files, perturbed=False):
                last_round = "unrun"
                continue
            alone = _union(failing_alone(dests[st].stem, sorted(r.executed), {})
                           for st, r in readings.items())
            if alone:
                keep, went = _withdraw(
                    keep, alone, _drop,
                    "passes only beside the file's other tests: red "
                    "when run alone in its own process")
                log(
                    f"  [task0d] cluster {cfg_cluster}: {len(went)} "
                    "capture(s) pass only beside the file's other tests "
                    "and were withdrawn.")
                if not went:
                    for s in keep:
                        _drop(s, "replay red alone on a test naming no capture")
                    return _none_left()
                last_round = "alone"
                continue

            # Green under the capture env. Now the axis run-twice cannot see:
            # the same binaries under a different environment. A capture whose
            # value moves with `RUST_BACKTRACE` is stable on every run and
            # still not a specification of the CRATE.
            if perturb_env:
                readings2 = {st: _read(verify(dests[st].stem, perturb_env)) for st in files}
                if _no_verdict(readings2, files, bodies, perturb_env, perturbed=True):
                    last_round = "perturbed no-verdict"
                    continue
                failed2 = _union(r.failed for r in readings2.values())
                if failed2:
                    round_out = "\n".join(r.out for r in readings2.values())
                    round_failed = failed2
                    still: list[BehaviourSpec] = []
                    env_went: list[BehaviourSpec] = []
                    not_held: list[str] = []
                    split = False
                    env_reason = f"environment-dependent: the observed value moves under {dict(perturb_env)}"
                    for spec in keep:
                        if replay_fn_name(spec) not in failed2:
                            still.append(spec)
                            continue
                        if spec.required_env:
                            # Its binary sets its world before anything reads
                            # it, so the harness env cannot reach it and this
                            # red is against the model, not a world. Loud,
                            # then judged in its own world.
                            not_held.append(
                                f"{world_stem(cfg_cluster, spec.required_env)}::{replay_fn_name(spec)}")
                            _drop(spec, "replay disagreed with the capture", verdict=True)
                            continue
                        # Once per build of the original, deliberately: the
                        # twin's Then may only pin what the crate printed
                        # whatever its build. Two runs of one build agree on
                        # the frames of a captured backtrace, and the twin
                        # then narrows to the original's private fns at their
                        # lines (`ProbeBuild`).
                        outs = ([recapture(spec, perturb_env, build) for build in ProbeBuild]
                                if recapture is not None else [])
                        if not outs or outs[0] is None or not capture_moved(spec, outs[0]):
                            env_went.append(spec)
                            _drop(spec, env_reason, verdict=True)
                            continue
                        stdouts = [o for o in outs if o is not None]
                        if len(stdouts) < len(outs):
                            env_went.append(spec)
                            _drop(spec, f"{env_reason}; the perturbed world did not run under "
                                        "every build of the original, so what the build "
                                        "printed cannot be told from what the crate did",
                                  verdict=True)
                            continue
                        worlds = split_env_worlds(spec, stdouts, perturb_env)
                        if worlds is None:
                            env_went.append(spec)
                            _drop(spec, "the perturbed world's recaptures "
                                        "agreed on no leading text — nothing "
                                        "in this render is the crate's", verdict=True)
                            continue
                        orig, twin = worlds
                        still.append(orig)
                        still.append(twin)
                        split = True
                    keep = still
                    if not_held:
                        log(
                            f"  [task0d] cluster {cfg_cluster}: HEALTH ‼ "
                            f"BEHAVIOUR_WORLD_NOT_HELD — {len(not_held)} pinned "
                            "capture(s) changed verdict with the harness env in a "
                            "binary that sets their world: " + ", ".join(not_held)
                        )
                        emit("task0d.behaviour_world_not_held",
                             data={"cluster": cfg_cluster, "tests": not_held})
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
                    for st in _nameless(readings2, files, failed2):
                        _withdraw_file(st, files,
                                       "perturbed replay red on a verdict naming no capture in the file",
                                       verdict=True)
                    last_round = "perturbed"
                    continue
                if _unrun(readings2, files, perturbed=True):
                    last_round = "perturbed unrun"
                    continue

            for st in list(files):
                if settle(dests[st], bodies[st]) is None:
                    _withdraw_file(st, files, "does not pass in the world it was captured in "
                                   "when run by the world settle")
            if not dests:
                return _none_left()
            names = [dests[st].name for st in sorted(dests)]
            log(
                f"  [task0d] cluster {cfg_cluster}: {len(keep)} captured behaviour(s) "
                f"emitted as {', '.join(names)}, verified against the original"
                f"{' and under a perturbed environment' if perturb_env else ''} — "
                f"observed values, not guessed ones."
            )
            emit(
                "task0d.behaviour_tests_emitted",
                data={"cluster": cfg_cluster, "captures": len(keep), "file": names[0],
                      "files": names, "withdrawn": len(dropped)},
            )
            return tuple(dests[st] for st in sorted(dests)), tuple(dropped), misses, tuple(keep)
    except BaseException:
        _unlink_all()
        raise
