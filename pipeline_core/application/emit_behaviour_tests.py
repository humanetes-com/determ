"""Captured behaviours -> real `#[test]`s in `tests/`.

WHY THIS EXISTS. Until this module, a captured behaviour could not reach the
oracle at all. The suite is read as PROMPT TEXT — `render_behaviour_list` for
t0c, `_fmt_behaviours` for t1.5 — and by the exercises writer; nothing turned a
`BehaviourSpec` into Rust test source, so an observable that no authored test
asserted stayed a note about the crate rather than a check on it. Verified
across `pipeline_core/` before this was written: every consumer of `spec.then`
rendered prose or compared captured output.

WHY IT DOES NOT FACE `judge_block_contract`. t0c's admission exists to catch a
model GUESSING a value: it refuses `contains(...)`, refuses a block that pins no
listed behaviour, and the runtime gate refuses a wrong literal. A capture has
already been executed against the original crate and its expected value is what
the crate printed. Re-judging it with a model-facing pin contract can only
refuse something already proven true — the contract's premise (this value is a
guess) is false here.

WHY A DEDICATED STEM. The emitted file is `tests/behaviour_gen_<N>.rs`, not a
per-source-file stem. Coexistence with t0c is NOT implemented by the filename:
`read_all_gen_tests` is stem-scoped, so a capture filed under any stem is
invisible to another file's `already_pinned` and t0c would still demand the pin.
The split lives at t0c's listing site instead. A dedicated stem simply keeps the
two writers from colliding on `_gen_<N>` numbering, since `next_gen_tests_n` is
stem-scoped too.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from pathlib import Path

from pipeline_core.domain.behaviour import BehaviourSpec
from pipeline_core.domain.behaviour_probe import is_probe_spec, render_replay_test
from pipeline_core.domain.generation import (
    as_integration_test,
    gen_tests_integration_rel,
    parse_gen_tests_integration_filename,
)
from pipeline_core.log import emit, log

BEHAVIOUR_TEST_STEM = "behaviour"


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
    specs: Sequence[BehaviourSpec], *, crate: str,
) -> str:
    """One integration-test file asserting every replayable capture."""
    bodies = [render_replay_test(s, fn_name=replay_fn_name(s)) for s in specs]
    block = "\n".join(b for b in bodies if b)
    return as_integration_test(block, crate=crate)


def next_behaviour_test_n(project_dir: Path) -> int:
    """The next free `tests/behaviour_gen_<N>.rs`."""
    tests = Path(project_dir) / "tests"
    used = [0]
    if tests.is_dir():
        for f in tests.iterdir():
            parsed = parse_gen_tests_integration_filename(f.name)
            if parsed and parsed[0] == BEHAVIOUR_TEST_STEM:
                used.append(parsed[1])
    return max(used) + 1


def emit_behaviour_tests(
    project_dir: Path,
    cfg_cluster: str,
    specs: Iterable[BehaviourSpec],
    *,
    crate: str,
    balanced: Callable[[str], bool] = lambda _s: True,
) -> Path | None:
    """Write the replayable captures as one `tests/behaviour_gen_<N>.rs`.

    Returns the path, or None when there is nothing replayable — which is the
    expected state for a suite whose captures are all legacy free-form blobs.
    A delimiter-unbalanced render is refused rather than written: a broken file
    in `tests/` does not fail one test, it stops the whole crate compiling and
    every verdict with it."""
    keep = replayable(specs)
    if not keep:
        return None
    body = render_behaviour_test_file(keep, crate=crate)
    if not balanced(body):
        log(f"  [task0d] cluster {cfg_cluster}: behaviour test file REFUSED "
            f"(delimiters unbalanced) — {len(keep)} capture(s) not emitted.")
        emit("task0d.behaviour_tests_refused",
             data={"cluster": cfg_cluster, "captures": len(keep)})
        return None
    n = next_behaviour_test_n(project_dir)
    dest = Path(project_dir) / gen_tests_integration_rel(BEHAVIOUR_TEST_STEM, n)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(body, encoding="utf-8")
    log(f"  [task0d] cluster {cfg_cluster}: {len(keep)} captured behaviour(s) "
        f"emitted as {dest.name} — observed values, not guessed ones.")
    emit("task0d.behaviour_tests_emitted",
         data={"cluster": cfg_cluster, "captures": len(keep), "file": dest.name})
    return dest
