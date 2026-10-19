"""The end gate's retro: a pinning test for each blocking body.

When every oracle test passes and the end gate still blocks
(`domain/end_verdict`), what failed is the oracle's reach, not a body: some
body the public API reaches is `unimplemented!()` and no test goes there in
the reconstruction (run `1680589-7a6824f8`, 2026-10-19: the two
`impl StdError for ContextError` `provide` bodies under nightly cfg). The
genesis update has nothing to read, since no test failed. What the next t2a
needs is a red test owned by that body, and this pass writes one:

- per blocking body and per build world that compiles it, the model is shown
  the body's signature in the rebuilt tree, the public calls the original's
  MIR says reach it (`construction_facts.json`, baked before strip), the
  crate's public API, and the header of a test file of that world. No original
  body: Hard Rule 11 holds, and the original is only RUN;
- the candidate is judged by `domain/pinning_test.judge_pin` on two runs: the
  original crate (the pristine worktree, `reference_run`), where every test
  must pass, and the rebuilt tree, where one must fail inside the body;
- an admitted file is written to `tests/` through the one writer that moves
  the oracle seal with it (`admit`), so the seal and every clock keyed on the
  oracle move by rule, not by drift;
- a test of the candidate that the original fails is withdrawn from it
  (`withdraw_original_failures`) and goes on the record with the original's
  excerpt, so later attempts see it; the rest is run on the original again
  and judged without it;
- a refused candidate goes back into the next attempt's prompt with its
  reason, up to `max_attempts`. The record is kept per build world, not per
  body: the judge decides on the world's whole blocking set, so a refusal
  reached while asking for one body is shown when asking for its siblings
  (run `3991945-1b7ae3e9`, 2026-10-19: the second `provide` body's first
  attempt opened with none of the first body's three refusals).

Returns how many files were admitted. Zero means the retro had nothing new
for this failure, and the module loop ends the module red with the verdict
(`ModuleReconstructor`): a body no public test can be shown to reach is a
named gap, never filled blind.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pipeline_core.domain.end_verdict import EndVerdict
from pipeline_core.domain.fn_execution import site_label
from pipeline_core.domain.generation import PIPELINE_GEN_TEST_ORIGIN
from pipeline_core.domain.models import FnSite
from pipeline_core.domain.pinning_test import (
    PinJudgement,
    PinRefusal,
    TestRun,
    judge_pin,
    observes_blocking,
    pin_shape_refusal,
    withdraw_original_failures,
)
from pipeline_core.domain.text import extract_code_content
from pipeline_core.prompts.pinning_test import build_pinning_test_prompt

__all__ = ["PinningDeps", "run_pinning_pass", "pin_file_stem", "admit_oracle_file"]


@dataclass(frozen=True, slots=True)
class PinningDeps:
    """Collaborators, injected by the composition root."""

    ask: Callable[[str], str]
    # (project_dir, site) -> the baked worlds that compile this body.
    worlds_for: Callable[[Path, FnSite], Sequence[Any]]
    # (project_dir, world, site) -> the route comment lines ([] when none).
    routes_for: Callable[[Path, Any, FnSite], Sequence[str]]
    # (project_dir, site) -> the body's signature line(s) in the rebuilt tree.
    signature_of: Callable[[Path, FnSite], str]
    api_surface: Callable[[Path, str], str]
    # (project_dir, world) -> the crate-level header of an oracle test file
    # gated to that world ("" when there is none).
    example_header: Callable[[Path, Any], str]
    # (world) -> the file's crate-level gate line ("" for no gate).
    gate_line: Callable[[Any], str]
    # (project_dir, module, stem, text, world) -> the run of that file on the
    # ORIGINAL crate under the world's env.
    run_original: Callable[[Path, str, str, str, Any], TestRun]
    # (project_dir, module, stem, text, world, sites) -> the run on the
    # REBUILT tree, plus per site the tests whose first panic lies inside it.
    run_rebuilt: Callable[[Path, str, str, str, Any, Sequence[FnSite]],
                          tuple[TestRun, Mapping[FnSite, frozenset[str]]]]
    # (project_dir, module, stem, text, world, sites) -> the run on the
    # ORIGINAL crate with each of those bodies that returns nothing emptied,
    # and the sites that were (the world's blocking bodies are passed).
    run_original_emptied: Callable[[Path, str, str, str, Any, Sequence[FnSite]],
                                   tuple[TestRun, frozenset[FnSite]]]
    # (project_dir, module, rel path, text) -> written, committed, and the
    # oracle seal moved to that commit.
    admit: Callable[[Path, str, str, str], bool]
    log: Callable[[str], None]
    emit: Callable[..., Any]
    max_attempts: int = 3
    # The sites this pass pinned. A blocking body absent from the tuple is
    # still a gap and is asked again after the tree changes.
    on_pinned: Callable[[tuple[FnSite, ...]], None] = lambda _sites: None


def admit_oracle_file(project_dir: Path, rel: str, text: str, *,
                      commit: Callable[[str], bool], tag: Callable[[], object],
                      seal_clean: Callable[[], bool], log: Callable[[str], None]) -> bool:
    """Write one test into the oracle after strip, and say yes only when the
    seal moved with it: the file is committed, the seal tag is at that
    commit, and the workspace oracle reads clean against the seal.

    Anything short of that is rolled back: the file is removed (and the
    removal committed when the add was), so the oracle on disk is again the
    one the seal holds and the next launch's drift check passes. A True
    that only meant "the file was written" left a committed file under an
    unmoved seal: the log said the seal moved, and the next launch refused
    on drift."""
    path = project_dir / rel
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    committed = commit(f"pipeline: pinning test {rel} admitted to the oracle")
    if committed:
        tag()
        if seal_clean():
            return True
    path.unlink(missing_ok=True)
    if committed:
        commit(f"pipeline: pinning test {rel} withdrawn — the oracle seal did not move")
    log(f"  [t4/pin] HEALTH ‼ {rel}: "
        + ("committed, but the oracle does not read clean against its seal"
           if committed else "the commit failed (or the workspace is not managed)")
        + " — the file is withdrawn and the oracle is the sealed one.")
    return False


def pin_file_stem(site: FnSite, world: Any, existing: Sequence[str]) -> str:
    """`pin_<fn>_<world>_gen_<N>`: a `{stem}_gen_{N}` name every reader of
    generated integration tests already parses, with the first free N."""
    base = f"pin_{site.fn_name}_{getattr(world, 'name', 'world')}"
    n = 1
    while f"{base}_gen_{n}" in existing:
        n += 1
    return f"{base}_gen_{n}"


def _file_text(reply: str, gate: str) -> str:
    body = extract_code_content(reply).strip()
    if not body:
        return ""
    head = gate + f"// {PIPELINE_GEN_TEST_ORIGIN}\n"
    return head + body + "\n"


def _observed(project_dir: Path, module: str, stem: str, text: str, world: Any,
              blocking: tuple[FnSite, ...], judgement: PinJudgement,
              deps: PinningDeps) -> PinJudgement:
    """The judgement after the observation run: the candidate on the
    original crate with the world's blocking bodies emptied
    (`observes_blocking`). Refused when every pinning test still passes.
    Refused when no reading could be taken: a value-returning body cannot
    be emptied to `{}`, and a run that did not build is not evidence."""
    emptied, units = deps.run_original_emptied(project_dir, module, stem, text,
                                               world, blocking)
    seen = observes_blocking(judgement.pinning, emptied)
    if seen is None:
        deps.log("  [t4/pin] the emptied run on the original gave no reading "
                 "(nothing returns nothing, or that run did not build) — "
                 "the pin is refused.")
        return PinJudgement(
            False, PinRefusal.UNMEASURED,
            "the emptied run on the original gave no reading, so whether "
            "the test observes the blocking bodies is not measured")
    if seen:
        return judgement
    return PinJudgement(
        False, PinRefusal.UNOBSERVED,
        "the original crate still passes every test with "
        + ", ".join(site_label(s) for s in sorted(units)) + " emptied to `{}`: no "
        "assertion depends on what those bodies do, so bodies that do nothing "
        "would turn this test green. Assert on an effect of the body.")


def _pin_one(project_dir: Path, module: str, crate: str, site: FnSite,
             world: Any, unpinned: Sequence[FnSite],
             deps: PinningDeps, *, blocking: Sequence[FnSite] = (),
             previous: list[tuple[str, str, str]] | None = None,
             ) -> tuple[tuple[FnSite, ...], int]:
    """Up to `max_attempts` candidates written for `site` in `world`. A
    candidate is judged against every still-unpinned blocking body the world
    compiles: written for one, it may be the test that reaches another, and
    that is still a pin, admitted for that body; the attempts left go on
    asking for `site`. `previous` is the world's record of earlier
    candidates, (written for, file, reason), shared across the bodies of the
    world and appended to here. Returns the bodies pinned and how many files
    were admitted."""
    unpinned = list(unpinned)
    # Every blocking body the world compiles, pinned already or not: all of
    # them are still stubs on the tree, and the observation run empties them.
    world_blocking = tuple(blocking) or tuple(unpinned)
    pinned_all: list[FnSite] = []
    files = 0
    tests_dir = project_dir / "tests"
    gate = deps.gate_line(world)
    flags = " ".join(f"--cfg {c}" for c in getattr(world, "cfg_true", ()) or ())
    if previous is None:
        previous = []
    label = site_label(site)
    blocking_labels = [site_label(s) for s in world_blocking]
    wname = getattr(world, "name", "")
    for attempt in range(1, deps.max_attempts + 1):
        existing = [p.stem for p in tests_dir.glob("*.rs")] if tests_dir.is_dir() else []
        stem = pin_file_stem(site, world, existing)
        rel = f"tests/{stem}.rs"
        prompt = build_pinning_test_prompt(
            crate=crate, world=wname, world_flags=flags, body=label,
            signature=deps.signature_of(project_dir, site),
            routes=deps.routes_for(project_dir, world, site),
            api_surface=deps.api_surface(project_dir, module),
            example_header=deps.example_header(project_dir, world),
            blocking=blocking_labels, previous=previous)
        try:
            reply = deps.ask(prompt)
        except Exception as exc:  # noqa: BLE001 - one call never stops the pass
            deps.log(f"  [t4/pin] {label} ({wname}) attempt {attempt}: call failed: {exc!r}")
            continue
        text = _file_text(reply, gate)
        judgement: PinJudgement | None = (
            pin_shape_refusal(text) if text else None)
        if not text:
            why = "the reply held no Rust file"
        elif judgement is not None:
            why = f"{judgement.refusal}: {judgement.detail}"
        else:
            original = deps.run_original(project_dir, module, stem, text, world)
            # Until nothing is left to cut: the re-run on the rest can fail a
            # test that passed beside the withdrawn one (state a withdrawn
            # test left in the process, a flaky test). Each round removes at
            # least one test and withdrawal never empties the file, so this
            # ends, and `judge_pin` never sees a file it would have to raise on.
            while (cut := withdraw_original_failures(text, original)) is not None:
                gone = ", ".join(sorted(cut.withdrawn))
                deps.log(f"  [t4/pin] {label} ({wname}) attempt {attempt}: withdrew "
                         f"{gone} (failed on the original crate); the rest of the "
                         "file is judged without it.")
                deps.emit("task4.pin_withdrawn", data={
                    "site": label, "world": wname, "attempt": attempt,
                    "withdrawn": sorted(cut.withdrawn), "detail": cut.detail[:2000]})
                previous.append((label, text, f"{PinRefusal.ORIGINAL_FAILED}: {cut.detail}"))
                text = cut.text
                original = deps.run_original(project_dir, module, stem, text, world)
            rebuilt, hits_by_site = deps.run_rebuilt(
                project_dir, module, stem, text, world, unpinned)
            hits = frozenset(t for ts in hits_by_site.values() for t in ts)
            judgement = judge_pin(text, original, rebuilt, hits)
            pinned: tuple[FnSite, ...] = ()
            if judgement.admitted:
                pinned = tuple(s for s in unpinned
                               if hits_by_site.get(s, frozenset()) & judgement.pinning)
                judgement = _observed(project_dir, module, stem, text, world,
                                      world_blocking, judgement, deps)
            if judgement.admitted:
                if deps.admit(project_dir, module, rel, text):
                    deps.log(f"  [t4/pin] {label} ({wname}): ADMITTED {rel} on attempt "
                             f"{attempt} — passes on the original crate, and "
                             f"{', '.join(sorted(judgement.pinning))} fail(s) on the "
                             "rebuilt tree inside "
                             + ", ".join(site_label(s) for s in pinned)
                             + "; the oracle seal moved with it.")
                    deps.emit("task4.pin_admitted", data={
                        "site": label, "world": wname, "file": rel,
                        "attempt": attempt, "pinning": sorted(judgement.pinning),
                        "pins": [site_label(s) for s in pinned]})
                    files += 1
                    pinned_all.extend(pinned)
                    if site in pinned:
                        return tuple(pinned_all), files
                    unpinned = [s for s in unpinned if s not in pinned]
                    previous.append((label, text, "admitted as the pin of "
                                     + ", ".join(site_label(s) for s in pinned)
                                     + ", but its first panic is not in the body "
                                     "asked for here: reach this one instead"))
                    continue
                why = "admission failed (write, commit or seal)"
            else:
                why = f"{judgement.refusal}: {judgement.detail}"
        deps.log(f"  [t4/pin] {label} ({wname}) attempt {attempt}: refused — {why[:400]}")
        deps.emit("task4.pin_refused", data={
            "site": label, "world": wname, "attempt": attempt,
            "reason": str(judgement.refusal) if judgement and judgement.refusal else "no_file",
            "detail": why[:2000]})
        # Stored whole: the evidence in it is already bounded, once and on a
        # line boundary, where the run is read (`pin_runs.reading_of`). A
        # second head cut here dropped the end of the compiler's output on
        # run `4184066-ce008393` (2026-10-19).
        previous.append((label, text or reply[:4000], why))
    return tuple(pinned_all), files


def run_pinning_pass(project_dir: Path, module: str, verdict: EndVerdict,
                     deps: PinningDeps, *, crate: str = "") -> int:
    """A pinning test for each blocking body, in a world that compiles it.
    Returns how many files were admitted."""
    crate = crate or module
    admitted = 0
    pinned: set[FnSite] = set()
    worlds_of = {site: list(deps.worlds_for(project_dir, site)) for site in verdict.blocking}
    # Earlier candidates per world: the judge empties the world's whole
    # blocking set, so every body of that world is shown every refusal.
    tried: dict[str, list[tuple[str, str, str]]] = {}
    for site in verdict.blocking:
        if site in pinned:
            continue
        if not worlds_of[site]:
            deps.log(f"  [t4/pin] {site_label(site)}: no baked world compiles it — "
                     "nothing to pin.")
            continue
        for world in worlds_of[site]:
            wname = getattr(world, "name", "")
            in_world = [s for s in verdict.blocking
                        if any(getattr(w, "name", "") == wname for w in worlds_of[s])]
            unpinned = [s for s in in_world if s not in pinned]
            got, files = _pin_one(project_dir, module, crate, site, world, unpinned, deps,
                                  blocking=in_world, previous=tried.setdefault(wname, []))
            admitted += files
            pinned.update(got)
            if site in pinned:
                break
        if site not in pinned:
            deps.log(f"  [t4/pin] {site_label(site)}: no admitted candidate pins it — "
                     "it stays a named gap of the end gate.")
    deps.log(f"  [t4/pin] {admitted} pinning test file(s) admitted; "
             f"{len(pinned)} of {len(verdict.blocking)} blocking body(ies) pinned.")
    deps.on_pinned(tuple(s for s in verdict.blocking if s in pinned))
    return admitted
