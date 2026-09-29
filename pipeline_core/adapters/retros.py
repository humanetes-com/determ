"""Per-fn retrospectives store — the engine's compounding cross-iteration
learning record (`.phase_c_logs/retrospectives.json`), keyed by
(file, fn, canonical container).
"""

from __future__ import annotations

import json
import os
from collections.abc import Collection, Iterable, Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any

from pipeline_core.domain.cfg_dead_sites import CfgDeadSites
from pipeline_core.domain.evidence_premise import retro_evidence_text
from pipeline_core.domain.identity import RetroKey
from pipeline_core.domain.models import FnLocator
from pipeline_core.domain.pair_fill import locators_from_scope
from pipeline_core.domain.retro_gate import FUTILITY_SUSPENDED, cleared_blockers
from pipeline_core.domain.rust_idents import canon_container
from pipeline_core.domain.workspace_layout import PHASE_C_LOGS_DIR
from pipeline_core.log import emit, log

__all__ = [
    "canon_container", "futile_suspension_keys", "is_fn_fully_exhausted",
    "load_retrospectives", "migrate_absolute_gate_retros",
    "migrate_confounded_retros", "reset_exhaustion_flags",
    "retire_unblocked_convictions", "retro_evidence_texts", "retro_key",
    "retrospectives_path", "save_retrospectives", "suspend_futile_guidance",
]


def retrospectives_path(project_dir: Path) -> Path:
    """Path to the per-fn retrospectives JSON. Keyed by (file, fn, container)
    so same-named fns in different impls track independently. Survives
    across module iterations — that's the point: learning compounds."""
    d = project_dir / PHASE_C_LOGS_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d / "retrospectives.json"


def load_retrospectives(project_dir: Path) -> dict[str, Any]:
    """Load the on-disk retrospective record. Returns {} on first run or
    when the file is malformed (caller treats missing entries as 'no
    prior retrospective'). Keys written before container canonicalisation
    are re-keyed on load so old records keep matching."""
    p = retrospectives_path(project_dir)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
        rekeyed: dict[str, Any] = {}
        for key, rec in data.items():
            parts = key.split("::", 2)
            if len(parts) == 3:
                key = retro_key(parts[0], parts[1], parts[2])
            rekeyed[key] = rec
        return rekeyed
    except Exception:
        return {}


def save_retrospectives(project_dir: Path, data: dict[str, Any]) -> None:
    with suppress(Exception):
        retrospectives_path(project_dir).write_text(
            json.dumps(data, indent=2), encoding="utf-8")


# canon_container relocated to domain/rust_idents.py (the per-impl
# body routing needs the same identity); re-exported here so the retro-key
# call sites and the pipeline.py smoke-pin alias stay unchanged.


def retro_key(file_rel: str, fn_name: str, container: str) -> RetroKey:
    """Stable key for the retrospective dict — the ONE mint point of
    :class:`RetroKey`. Container is included so overloaded `fn new` impls
    (Adhoc/Trait/Boxed) track independently; it is canonicalised so different
    renderings of one impl share a key."""
    return RetroKey(f"{file_rel}::{fn_name}::{canon_container(container)}")


def reset_exhaustion_flags(
    project_dir: Path, reason: str, only: Iterable[str] | None = None,
) -> list[str]:

    """Clear `fully_exhausted` flags (keep the lessons — category, guidance,
    history all stay) at a phase boundary where the evidence context changed
    (e.g. the merged tree landed dozens of fills). The cross-iteration skip
    those flags drive was earned against a narrower tree; one fresh,
    note-informed attempt per boundary is the same re-entry rule the per-fn
    engine applies within a pass, applied across phases.

    Capped PER FN via `reset_attempts` (env `RETRO_RESET_MAX_ATTEMPTS`,
    default 2): a fn that has already been reset and immediately
    re-exhausted (no improvement) this many times stops being re-admitted,
    even while OTHER work in the cluster keeps progressing. Without this a
    genuinely-stuck fn pays one full LLM-call-plus-compile-cycle tax on
    EVERY future boundary for the rest of the run, for zero payoff each
    time — live-observed on anyhow (iter 3 re-admitted 16 fns, net
    `improved=False`, right after iter 2's 43-fn batch DID pay off) — the
    unconditional version can't tell "first informed retry" from "the same
    fn getting its Nth futile one."

    Returns the RELEASED KEYS (`file::fn::container` strings), not a count:
    a release is only real if it opens every gate that holds the unit, and
    the TDD driver keeps its own in-process exhaustion set (`refill_attempts`
    + `permanently_exhausted`) that this disk write cannot reach — run
    `2219473` (2026-08-10) re-admitted 11 units here and dispatched zero,
    because all of them sat refill-capped in that other store. The caller
    needs the identities to open its side.

    ``only`` narrows the release to the named store keys (the escalation's
    targets — `release_escalated_convictions`); None keeps the blanket
    boundary release. The cap applies either way: a keyed release spends the
    same per-record budget, so a unit the escalation names every sweep is
    re-admitted twice and then stays convicted."""
    max_attempts = int(os.getenv("RETRO_RESET_MAX_ATTEMPTS", "2"))
    retros = load_retrospectives(project_dir)
    released: list[str] = []
    capped = 0
    wanted = None if only is None else {str(k) for k in only}
    for key, rec in retros.items():
        if not (isinstance(rec, dict) and rec.get("fully_exhausted")):
            continue
        if wanted is not None and str(key) not in wanted:
            continue

        attempts = int(rec.get("reset_attempts", 0))
        if attempts >= max_attempts:
            capped += 1
            continue
        rec["fully_exhausted"] = False
        rec["reset_attempts"] = attempts + 1
        released.append(str(key))
    if released or capped:
        save_retrospectives(project_dir, retros)
        log(f"  [phase-c] reset {len(released)} fully_exhausted flag(s) "
            f"({capped} at the {max_attempts}-attempt cap, left exhausted) — {reason}")
        emit("phase_c.exhaustion.reset",
             data={"cleared": len(released), "capped": capped,
                   "reason": reason})
    return released



def retire_unblocked_convictions(
    project_dir: Path, still_stubbed: dict[str, frozenset[str]],
    cluster: str = "", *,
    cfg_dead: Collection[tuple[str, str]] = frozenset(),
) -> int:

    """Release every `fully_exhausted` verdict whose recorded blocking stubs
    have since been filled. `still_stubbed` maps a file (repo-relative, as the
    retro key spells it) to the fn names still hollow in it, and must be the
    WHOLE of what this sweep found — a file absent from it is read as "nothing
    hollow left here", which is the only way a record whose last blocker filled
    is ever released. Returns the number of flags cleared.

    Two guards keep that reading from becoming a mass release:

    * `cluster` scopes it to records earned in the sweeping cluster. The store
      is shared and the stub scan is cluster-scoped, so `src/nightly.rs`'s fns
      are absent from every `stable` sweep while still being `unimplemented!()`
      on disk. A record with no cluster stamp is legacy — earned in an unknown
      context, which is not the same as this one — and is left alone, the same
      rule `RetroProvenance.matches` applies to the verdict itself.
    * an EMPTY map releases nothing. A mid-reconstruction cluster with no hollow
      fn anywhere is far likelier to be a scan that failed than a tree that
      finished — this pipeline has already had a walk report a crate with 186
      fns and no tests because it would not follow a symlink — and the cost of
      being wrong is a full frontier of model calls on units already convicted.
      When the tree really has finished, the loop is about to exit and the
      release would have bought nothing.

    Deliberately NOT routed through the score gate that guards
    `reset_exhaustion_flags`. That gate asks "has the cluster's pass count moved
    since the last release", which is the right question for a blanket
    re-admission and the wrong one here: a producer filling is itself the new
    evidence, and on `anyhow` cluster `stable` (2026-08-07) the score could not
    move until exactly these units were released — run `259637-36db9aad` logged
    `NOT re-admitting exhausted fn(s)` on every sweep from iteration 4 to its
    death while `src/error.rs::*impl*::Error` and `src/error.rs::new::Error` —
    the two units the ~60 missing tests run through — stayed convicted against a
    tree whose vtable helpers had all filled.

    Bounded without a cap: the stored blocker set is narrowed to those still
    hollow on every release, and a stub set only shrinks, so a record can be
    released at most once per fn it recorded and each release answers to a
    distinct fill. A record with no `blocked_on` stamp is never touched.

    ``cfg_dead`` is the cluster's positive census of ``(file, fn)`` sites its
    cfg-set never compiles, stub or filled (`cluster_dead_sites`). It does
    two things.
    A blocker in it is read as cleared: nothing in this cluster will ever see
    it fill, so a record `blocked_on: ["provide"]` under `stable` — where
    `provide` is `#[cfg(error_generic_member_access)]` — holds an empty
    blocker list (the rule `screen_guidance` already applies to the guidance
    half). And a non-empty census is proof the scanner RAN, which is what lets
    an empty ``still_stubbed`` be read as a finished tree instead of a failed
    scan: on `anyhow`/`stable` (run `1246060-2339497b`, 2026-08-21) the only
    hollow body left was the cfg-dead `provide`, the cluster-scoped map was
    therefore `{}`, and the guard below returned before looking at the one
    record it existed to release."""
    if not still_stubbed and not cfg_dead:
        return 0
    dead = CfgDeadSites(frozenset(cfg_dead))
    retros = load_retrospectives(project_dir)
    cleared = 0
    freed: list[str] = []
    for key, rec in retros.items():
        if not (isinstance(rec, dict) and rec.get("fully_exhausted")):
            continue
        if rec.get("cluster") != cluster:
            continue
        file_rel = str(key).split("::", 1)[0]
        dead_here = dead.fns_in(file_rel)
        released = cleared_blockers(
            rec, still_stubbed.get(file_rel, frozenset()) - dead_here)

        if not released:
            continue
        rec["fully_exhausted"] = False
        rec["blocked_on"] = sorted(
            frozenset(str(s) for s in rec.get("blocked_on", [])) - released)
        cleared += 1
        freed.append(f"{key} (unblocked by {', '.join(sorted(released))})")
    if cleared:
        save_retrospectives(project_dir, retros)
        log(f"  [phase-c] released {cleared} exhausted unit(s) — a fn each one "
            f"was convicted for calling is no longer a stub: {'; '.join(freed[:6])}"
            + (f" (+{cleared - 6} more)" if cleared > 6 else ""))
        emit("phase_c.exhaustion.unblocked",
             data={"cleared": cleared, "units": freed[:20]})
    return cleared


def futile_suspension_keys(
    occurrences: Mapping[FnLocator, tuple[tuple[str, str], ...]],
    futile: Iterable[FnLocator],
) -> set[str]:
    """The store keys whose GUIDANCE a futility verdict has answered.

    ``occurrences`` is `fn_occurrence_index`'s view of the tree: (file, fn) →
    every occurrence's (container, sig). Two record shapes are implicated:

    * the per-fn record of every occurrence of a futile unit — the guidance
      there advises the exact rewrite futility measured as changing nothing;
    * a per-impl record (`fn == "*impl*"`), but ONLY when every member of its
      container is futile. The block record's guidance covers the whole
      block, and the escalation's verdict is per accessor — suspending
      `impl Error`'s record because one of its five members went futile for
      one test would silence lessons about the other four. The live case this
      exists for is the single-member block: `DoubleEndedIterator for Chain`
      holds only `next_back`, so its record IS the accessor's record.
    """
    futile_set = set(futile)
    keys: set[str] = set()
    for loc in futile_set:
        for container, _sig in occurrences.get(loc, ()):
            keys.add(str(retro_key(loc.file, loc.fn_name, container)))
    members: dict[tuple[str, str], set[str]] = {}
    for loc, occs in occurrences.items():
        for container, _sig in occs:
            if container:
                members.setdefault((loc.file, container), set()).add(
                    loc.fn_name)
    futile_names = {(loc.file, loc.fn_name) for loc in futile_set}
    for (file_rel, container), fns in members.items():
        if fns and all((file_rel, fn) in futile_names for fn in fns):
            keys.add(str(retro_key(file_rel, "*impl*", container)))
    return keys


def escalation_release_keys(
    occurrences: Mapping[FnLocator, tuple[tuple[str, str], ...]],
    targets: Iterable[FnLocator],
) -> set[str]:
    """The store keys whose EXHAUSTION a futility escalation has re-opened.

    The escalation is new evidence about its targets: a producer convicted
    before anyone knew this test needed it was judged on a different question.
    The driver already re-admits such targets from its process-local set; this
    names the disk records so the same release reaches the store — the second
    gate on the same unit, which the process-local release never touched, and
    the one the impl route honoured as `skipped (prior retro marked exhausted)`
    at both dispatches of `StdErrorforContextError<C,Error>` in run
    `1246060-2339497b` (2026-08-21).

    Unlike `futile_suspension_keys`, a per-impl record is named when ANY of its
    members is a target: the block is dispatched as a whole, and the cost of a
    wrong release here is one bounded re-attempt (the reset cap), while the
    cost of the opposite error is a unit skipped on every sweep."""
    target_set = set(targets)
    keys: set[str] = set()
    for loc in target_set:
        for container, _sig in occurrences.get(loc, ()):
            keys.add(str(retro_key(loc.file, loc.fn_name, container)))
            if container:
                keys.add(str(retro_key(loc.file, "*impl*", container)))
    return keys


def release_escalated_convictions(
    project_dir: Path, keys: Iterable[str], *, reason: str,
) -> list[str]:
    """Release the `fully_exhausted` flags of ``keys`` through the capped
    boundary reset, so the escalation's re-admission opens BOTH gates on its
    targets while the per-record budget still bounds it. Returns the released
    keys, as `reset_exhaustion_flags` does."""
    wanted = {str(k) for k in keys}
    if not wanted:
        return []
    return reset_exhaustion_flags(project_dir, reason, only=wanted)


def retro_evidence_texts(project_dir: Path) -> dict[FnLocator, str]:
    """Per unit, the store's rows about it, as text a fingerprint can digest.

    The `retro` channel of `domain/evidence_premise`: the rows that feed
    `RETROSPECTIVE_GUIDANCE` and — on a pair record — the next fill's
    `LAST_ERROR` (`last_compile_error` / `last_error_preview` /
    `last_failed_body`, which `impl_fill` reads off this same record).

    A row reaches a unit two ways, and both are read off the row itself
    rather than off its key's shape. Its KEY is `file::fn::container`, so
    the first two segments name the unit. And a JOINT row — the pair
    route's, whose key is `<file>::*pair*::<dispatcher>+<implementer>` —
    names its members in `joint_members`, so the pair's accumulated
    diagnosis reaches `src/error.rs::fmt` and `src/fmt.rs::debug`
    themselves instead of being filed under a `*pair*` unit no locator ever
    equals. Nothing here parses the container.
    """
    store = load_retrospectives(project_dir)
    by_unit: dict[FnLocator, dict[str, Any]] = {}
    for key, rec in store.items():
        parts = str(key).split("::")
        units: set[FnLocator] = set()
        if len(parts) >= 2 and parts[0] and parts[1]:
            units.add(FnLocator(parts[0], parts[1]))
        if isinstance(rec, dict):
            units |= locators_from_scope(rec.get("joint_members") or ())
        for unit in units:
            by_unit.setdefault(unit, {})[str(key)] = rec
    return {unit: retro_evidence_text(rows) for unit, rows in by_unit.items()}


def suspend_futile_guidance(
    project_dir: Path, keys: Iterable[str], *,
    cluster: str, tests: Iterable[str],
) -> list[str]:

    """Stamp ``keys``' records `futility_suspended` for ``cluster``.

    The read side is `domain/retro_gate.admissible_guidance`, which withholds
    a suspended record's guidance for the stamping cluster only — the store
    is shared, and the futility was measured under one cluster's tree. The
    verdict half (`fully_exhausted`) is deliberately untouched: skipping a
    futile accessor's re-dispatch is aligned with what futility measured.

    Only records that exist AND carry guidance are stamped (a stamp on a
    record nobody reads is state with no reader), and re-stamping the same
    cluster only widens the test list. Returns the keys newly stamped or
    widened, so the caller's log can name them.
    """
    retros = load_retrospectives(project_dir)
    stamped: list[str] = []
    new_tests = {str(t) for t in tests}
    for key in sorted({str(k) for k in keys}):
        rec = retros.get(key)
        if not isinstance(rec, dict) or not rec.get("guidance"):
            continue
        susp = rec.get(FUTILITY_SUSPENDED)
        if isinstance(susp, dict) and susp.get("cluster") == cluster:
            already = {str(t) for t in susp.get("tests", ())}
            merged = already | new_tests
            if merged == already:
                continue
            susp["tests"] = sorted(merged)
        else:
            rec[FUTILITY_SUSPENDED] = {
                "cluster": cluster, "tests": sorted(new_tests)}
        stamped.append(key)
    if stamped:
        save_retrospectives(project_dir, retros)
        log(f"  [phase-c] suspended stored guidance on {len(stamped)} "
            f"record(s) — the futility escalation measured their rewrites as "
            f"changing nothing for {sorted(new_tests)}: "
            + "; ".join(stamped[:6])
            + (f" (+{len(stamped) - 6} more)" if len(stamped) > 6 else ""))
        emit("phase_c.retros.futility_suspended", data={
            "cluster": cluster, "tests": sorted(new_tests),
            "keys": stamped[:20], "count": len(stamped)})
    return stamped


def is_fn_fully_exhausted(
    retros: dict[str, Any], file_rel: str, fn_name: str, container: str,
) -> bool:
    """The cross-iteration skip, progress-gated. A fn is permanently skipped
    only when (a) the LLM classified it GENUINELY_HARD, or (b) a
    retrospective fired AND the next-best error count did NOT improve over
    the prior best (`fully_exhausted` flag set at write time). No hard
    retrospective-count cap — as long as each retro reduces errors, the
    loop is free to continue. `retrospective_count` is kept only for
    telemetry."""
    key = retro_key(file_rel, fn_name, container)
    rec = retros.get(key)
    if not rec:
        return False
    # A downstream_only record means every error was in ANOTHER file — the
    # fn itself is probably fine and was parked for a break it didn't cause.
    # Ignore the flag (records written before the writer stopped exhausting
    # on downstream_only stay on disk; reinterpreting heals them without a
    # state migration).
    if rec.get("signal") == "downstream_only":
        return False
    return bool(rec.get("fully_exhausted"))


# Gate-verdict sentence openers (regression gate feedback, cargo_runner) — a
# retro whose ONLY failure evidence starts with one of these and carries zero
# compile errors was concluded from runtime test-count comparisons. Records
# written before the gate became binary-scoped drew those comparisons over
# crash-flickering binaries: the "evidence" was noise and the guidance
# confabulated around it.
_GATE_VERDICT_PREFIXES = (
    "This compiled, but it REGRESSED",
    "This dropped the number of tests that EXECUTE",
    "This dropped the cluster pass count",
    "This made test binar",
)

_CONFOUND_MIGRATION = "binary_scoped_gate_2026_07"


def migrate_confounded_retros(project_dir: Path) -> tuple[int, int]:
    """One-time evidence-hygiene migration, armed when the regression gate
    became binary-scoped: every existing `fully_exhausted` flag (and its
    reset-attempt budget) was computed under the old whole-run gate whose
    verdicts were crash-confounded — clear them all for one fresh attempt
    under trustworthy verdicts. Guidance is RETIRED (dropped) only for
    records whose last failure evidence was purely a gate verdict with no
    compile error: those lessons explained noise. Compile-error-derived
    guidance stays. Returns (flags_cleared, guidance_retired)."""
    retros = load_retrospectives(project_dir)
    done = retros.get("__migrations__")
    if isinstance(done, list) and _CONFOUND_MIGRATION in done:
        return (0, 0)
    cleared = retired = 0
    for key, rec in retros.items():
        if key == "__migrations__" or not isinstance(rec, dict):
            continue
        if rec.get("fully_exhausted") or rec.get("reset_attempts"):
            rec["fully_exhausted"] = False
            rec["reset_attempts"] = 0
            cleared += 1
        preview = str(rec.get("last_error_preview") or "")
        if (rec.get("guidance")
                and int(rec.get("error_count") or 0) == 0
                and preview.startswith(_GATE_VERDICT_PREFIXES)):
            rec["guidance"] = ""
            rec["retired_reason"] = "confounded gate verdict (pre-binary-scoping)"
            retired += 1
    migrations = retros.setdefault("__migrations__", [])
    if isinstance(migrations, list):
        migrations.append(_CONFOUND_MIGRATION)
    save_retrospectives(project_dir, retros)
    emit("phase_c.retros.migrated", data={
        "migration": _CONFOUND_MIGRATION,
        "flags_cleared": cleared, "guidance_retired": retired})
    return (cleared, retired)


_GATE_MIGRATION = "comparative_gate_2026_07"


def migrate_absolute_gate_retros(project_dir: Path) -> tuple[int, int]:
    """One-time migration, armed when fill acceptance stopped being ABSOLUTE.

    Every `fully_exhausted` flag on disk was earned under the old rules: a
    joint fill was reverted unless it made a unit PERFECT (a strict improvement
    was thrown away), files over the per-file fn cap were skipped whole instead
    of being partitioned into groups, and the prompts that produced those
    attempts still carried layout-contradicted notes and guidance. A verdict of
    "this unit is hard" drawn from those attempts counts no fair sample of the
    current engine, so it convicts nothing — clear the flags and the
    reset budgets and let each unit earn a verdict again.

    Guidance is KEPT: whether a lesson still applies is judged per attempt
    against the layout being compiled (domain/retro_gate), not by wiping the
    shared store — the same string is true for the other cluster's layout.
    Returns (flags_cleared, budgets_cleared)."""
    retros = load_retrospectives(project_dir)
    done = retros.get("__migrations__")
    if isinstance(done, list) and _GATE_MIGRATION in done:
        return (0, 0)
    cleared = budgets = 0
    for key, rec in retros.items():
        if key == "__migrations__" or not isinstance(rec, dict):
            continue
        if rec.get("fully_exhausted"):
            rec["fully_exhausted"] = False
            cleared += 1
        if rec.get("reset_attempts"):
            rec["reset_attempts"] = 0
            budgets += 1
    migrations = retros.setdefault("__migrations__", [])
    if isinstance(migrations, list):
        migrations.append(_GATE_MIGRATION)
    save_retrospectives(project_dir, retros)
    emit("phase_c.retros.migrated", data={
        "migration": _GATE_MIGRATION,
        "flags_cleared": cleared, "budgets_cleared": budgets})
    return (cleared, budgets)
