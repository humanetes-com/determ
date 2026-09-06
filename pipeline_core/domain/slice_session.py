"""The red-slice debugging session — pure planning, routing and rendering.

The stateless fill route rebuilt a fresh document for every attempt on a
failing test's units and kept nothing between attempts. Run `3003-93e9a9bf`
(2026-09-02, cluster `stable`): the one dispatchable write that could move
the two red tests was attempted six times across three iterations — four
compile failures on the same private-field diagnostic (the accessor list a
retry carried lived in a per-call slot and was gone at the next iteration's
first call), then two ten-test regressions the model never saw the printed
values of — while 155 of the run's 205 model-minutes went to units the loop
had already labelled as re-buys.

A session is one bounded conversation over one SLICE: the failing tests, the
crate fns those tests execute (the coverage owner map plus the attributed
units), and the whole passing suite as the constraint. Each turn the model
may return a change set over any subset of the slice, a context request, or
notes; the harness answers with the compiler's diagnostics or the suite's
verdicts — the failing assertions with their printed and observed values —
appended to the same transcript. This module holds the parts that need no
IO: which tests form one slice, how a reply's bodies route to units, and the
text of every turn the harness writes back.

No live test or unit name is an input anywhere here: the same functions plan
a Display dispatcher in `lib.rs` and its `render.rs` implementer on a crate
that does not exist yet.
"""
from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from pipeline_core.domain.attribution import container_key, locator_of, site_label
from pipeline_core.domain.impl_routing import norm_src_rel
from pipeline_core.domain.models import FnLocator, FnRecord, FnSite

NOTES_OPEN = "<NOTES>"
NOTES_CLOSE = "</NOTES>"


@dataclass(frozen=True, slots=True)
class SessionSlice:
    """One session's work: the failing tests it answers for and the units
    it may write, with the units grouped by file for rendering."""

    tests: tuple[str, ...]
    units: tuple[FnSite, ...]

    @property
    def files(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(u.file for u in self.units))

    def label(self) -> str:
        return f"session[{len(self.tests)} test(s), {len(self.units)} unit(s)]"

    def member_scope(self) -> frozenset[str]:
        return frozenset(f"{u.file}::{u.fn_name}" for u in self.units)


def _stub_blocks(units: Collection[FnSite], hollow: Collection[FnLocator]) -> bool:
    """True when any slice site shares a dispatch name with a still-hollow body."""
    hollow_names = frozenset(hollow)
    return any(locator_of(u) in hollow_names for u in units)


def _bare_test(name: object) -> str:
    return str(name).rsplit("::", 1)[-1]


def _as_locator(key: object) -> FnLocator | None:
    if isinstance(key, FnLocator):
        return FnLocator(norm_src_rel(key.file), key.fn_name)
    if isinstance(key, FnSite):
        return FnLocator(norm_src_rel(key.file), key.fn_name)
    if isinstance(key, tuple) and len(key) >= 2:
        return FnLocator(norm_src_rel(str(key[0])), str(key[1]))
    return None


def attributed_units_for_session(
    eligible: Mapping[Any, Collection[Any]],
    site_frontier: Mapping[FnSite, Collection[Any]],
) -> dict[str, set[FnSite]]:
    """Session units from the site frontier, filtered by dispatchable locators.

    Eligible answers whether a locator may be dispatched this iter (exhausted
    skip, test-target filter, red-build, blocked-consumer). It is not a source
    of units: a locator with no frontier site used to enter as
    ``FnSite(file, fn, "")``, and ``expand_locator_sites`` then named every
    container of that name on the tree — coverage's Debug site re-admitted
    Display and the merge exceeded the cap (run ``110993-6efa23be``,
    2026-09-03). Classifier / reverse-map / ``lift_sites`` still put
    locator-only sites *in* the frontier; those still expand. Coverage owners
    are a separate argument to ``session_slices_for_failing``.
    """
    eligible_locs: dict[FnLocator, set[str]] = {}
    for key, tests in eligible.items():
        loc = _as_locator(key)
        if loc is None:
            continue
        eligible_locs.setdefault(loc, set()).update(_bare_test(t) for t in tests)
    out: dict[str, set[FnSite]] = {}
    for site, tests in site_frontier.items():
        allowed = eligible_locs.get(
            FnLocator(norm_src_rel(site.file), site.fn_name))
        if not allowed:
            continue
        for t in tests:
            bn = _bare_test(t)
            if bn not in allowed:
                continue
            out.setdefault(bn, set()).add(site)
    return out


def _pending_after_filters(
    slices: Mapping[str, Collection[FnSite]],
    hollow: Collection[FnLocator],
) -> tuple[dict[str, frozenset[FnSite]], int, int]:
    pending: dict[str, frozenset[FnSite]] = {}
    n_empty = n_hollow = 0
    for test in sorted(slices):
        units = frozenset(slices[test])
        if not units:
            n_empty += 1
            continue
        if _stub_blocks(units, hollow):
            n_hollow += 1
            continue
        pending[test] = units
    return pending, n_empty, n_hollow


def _merge_pending(
    pending: Mapping[str, frozenset[FnSite]],
) -> list[tuple[list[str], set[FnSite]]]:
    groups: list[tuple[list[str], set[FnSite]]] = []
    for test, units in pending.items():
        merged_into: tuple[list[str], set[FnSite]] | None = None
        for grp in groups:
            if grp[1] & units:
                if merged_into is None:
                    grp[0].append(test)
                    grp[1].update(units)
                    merged_into = grp
                else:
                    merged_into[0].extend(grp[0])
                    merged_into[1].update(grp[1])
                    grp[0].clear()
                    grp[1].clear()
        if merged_into is None:
            groups.append(([test], set(units)))
    return groups


def _fit_under_cap(
    names: Sequence[str],
    pending: Mapping[str, frozenset[FnSite]],
    max_units: int,
) -> tuple[list[tuple[list[str], set[FnSite]]], list[str]]:
    """Pack one merged group whose UNION is over the cap into sessions that
    are each under it, without truncating any test's slice.

    Tests are placed in name order. A test whose own slice exceeds the cap
    is returned in the second element and belongs to no session (the
    drop-not-truncate contract, unchanged). Every other test joins the
    session that already holds the most of its executed path — the fewest
    new units, ties to the earliest session — when the union stays under
    the cap, else it opens a session. Deterministic; the same inputs pack
    the same way on every call.

    A hub unit every test executes (the crate's construction spine) lands in
    every session that needs it. Sessions run sequentially on one tree, so
    the second sees the first's bodies; that is the same unit written twice
    under two judgments, which file-first already does every pass.
    """
    bins: list[tuple[list[str], set[FnSite]]] = []
    oversized: list[str] = []
    for test in sorted(dict.fromkeys(names)):
        units = pending[test]
        if len(units) > max_units:
            oversized.append(test)
            continue
        fits = [i for i, (_, held) in enumerate(bins)
                if len(held | units) <= max_units]
        if not fits:
            bins.append(([test], set(units)))
            continue
        best = max(fits, key=lambda i: (len(bins[i][1] & units), -i))
        bins[best][0].append(test)
        bins[best][1].update(units)
    return bins, oversized


@dataclass(frozen=True, slots=True)
class SlicePlanGaps:
    """Why ``plan_slices`` admitted nothing, and what it packed: empty
    slices, hollow (stub-blocked) slices, single tests over the unit cap,
    and merged groups whose union was over the cap but whose tests were
    fitted into sessions under it. The first three used to share one log
    clause, so a wake could not tell an empty leftover from an exploded
    coverage slice (run ``110993-6efa23be``, 2026-09-03).

    ``over_cap`` counts TESTS whose own slice exceeds the cap — the only
    drop left on this path. Before packing it counted merged GROUPS, and a
    coverage-complete leftover set whose 32 tests each executed 8-23 sites
    (union 54) was refused as one over-cap group on every fill of run
    ``2965-453dd7a4`` (2026-09-05), so the session route never started.
    """

    empty: int
    hollow: int
    over_cap: int
    over_cap_units: int
    max_units: int
    #: Sessions produced by packing over-union groups.
    fitted: int = 0
    #: Over-union groups that were packed.
    over_union: int = 0
    #: The largest over-union group's unit count (what the old rule dropped).
    fitted_from_units: int = 0

    def log_clause(self) -> str:
        parts: list[str] = []
        if self.empty:
            parts.append(f"{self.empty} empty")
        if self.hollow:
            parts.append(f"{self.hollow} hollow")
        if self.over_cap:
            parts.append(
                f"{self.over_cap} over SLICE_SESSION_MAX_UNITS={self.max_units}"
                f" ({self.over_cap_units} unit(s))")
        return "; ".join(parts) if parts else "no session"

    def fitted_clause(self) -> str:
        """The packing statement, empty when no over-union group existed."""
        if not self.over_union:
            return ""
        return (f"{self.fitted} session(s) fitted under the cap from "
                f"{self.over_union} over-union group(s) of up to "
                f"{self.fitted_from_units} unit(s) "
                f"(SLICE_SESSION_MAX_UNITS={self.max_units})")


def slice_plan_gaps(
    slices: Mapping[str, Collection[FnSite]],
    *,
    stubs: Collection[FnLocator] = (),
    max_units: int = 32,
) -> SlicePlanGaps:
    """The drop reasons ``plan_slices`` applies and the packing it did,
    counted separately."""
    pending, n_empty, n_hollow = _pending_after_filters(slices, frozenset(stubs))
    n_over = 0
    over_units = 0
    n_fitted = 0
    n_over_union = 0
    fitted_from = 0
    for names, members in _merge_pending(pending):
        if not names or len(members) <= max_units:
            continue
        bins, oversized = _fit_under_cap(names, pending, max_units)
        n_over += len(oversized)
        for t in oversized:
            over_units = max(over_units, len(pending[t]))
        if bins:
            n_over_union += 1
            n_fitted += len(bins)
            fitted_from = max(fitted_from, len(members))
    return SlicePlanGaps(n_empty, n_hollow, n_over, over_units, max_units,
                         fitted=n_fitted, over_union=n_over_union,
                         fitted_from_units=fitted_from)


def locator_only_expansions(
    slices: Mapping[str, Collection[FnSite]],
    tree_sites: Iterable[FnSite],
) -> tuple[tuple[str, str, int], ...]:
    """Locator-only slice members that ``expand_locator_sites`` fans out to
    more than one tree site. The log line this feeds is the census signature
    for the plan-path collapse (one locator becoming every container)."""
    by_name: dict[tuple[str, str], int] = {}
    for site in tree_sites:
        key = (norm_src_rel(site.file), site.fn_name)
        by_name[key] = by_name.get(key, 0) + 1
    seen: dict[tuple[str, str], int] = {}
    for units in slices.values():
        for u in units:
            if u.container:
                continue
            key = (norm_src_rel(u.file), u.fn_name)
            n = by_name.get(key, 0)
            if n > 1:
                seen[key] = n
    return tuple(sorted((f, n, c) for (f, n), c in seen.items()))


def plan_slices(
    slices: Mapping[str, Collection[FnSite]],
    *,
    stubs: Collection[FnLocator] = (),
    max_units: int = 32,
) -> list[SessionSlice]:
    """Group the failing tests into sessions: tests whose unit sets overlap
    share one session (the union). A test whose slice touches a still-hollow
    body is left to the stub routes — a session rewrites behaviour, and a
    verdict behind a stub is not a behaviour verdict. A single test whose
    slice is over `max_units` is dropped rather than truncated: a session
    that may only write half the executed path cannot be judged as one
    transaction.

    A merged group whose UNION is over the cap while every member test's
    own slice fits is packed into sessions under the cap (`_fit_under_cap`),
    never dropped. The drop contract is about one test's executed path; it
    said nothing about a leftover set that merely shares a construction
    spine. Run `2965-453dd7a4` (2026-09-05, cluster stable): 32 failing
    tests, each executing 8-23 sites, union 54, `construct` and `drop` in
    every map — refused as one over-cap group on every fill, zero sessions
    started, while file-first re-bought the same producers for four
    batches. Each packed session still holds every one of its tests' whole
    slices; no test is split across sessions.

    Deterministic: tests sorted, units sorted, output ordered by first test.
    """
    pending, _, _ = _pending_after_filters(slices, frozenset(stubs))
    out: list[SessionSlice] = []
    for names, members in _merge_pending(pending):
        if not names:
            continue
        groups: list[tuple[list[str], set[FnSite]]]
        if len(members) <= max_units:
            groups = [(list(names), set(members))]
        else:
            groups, _oversized = _fit_under_cap(names, pending, max_units)
        for gnames, gmembers in groups:
            out.append(SessionSlice(
                tests=tuple(sorted(dict.fromkeys(gnames))),
                units=tuple(sorted(gmembers,
                                   key=lambda u: (u.file, u.fn_name, u.container)))))
    return out


def slice_plan_payload(
    slices: Mapping[str, Collection[FnSite]],
    plans: Sequence[SessionSlice],
    gaps: SlicePlanGaps,
) -> dict[str, Any]:
    """The `phase_c.session.plan` event: what the engine handed
    `plan_slices` and what came back, emitted on every fill — a refusal
    included. Before this the refusal was one log line and the union it
    refused was not in `events.jsonl`, so the packing fixture for run
    `2965-453dd7a4` had to be computed from the owner-map event instead of
    the engine's actual argument (hops and locator expansion included)."""
    union: set[FnSite] = set()
    for units in slices.values():
        union.update(units)
    return {
        "tests": len(slices),
        "units_union": len(union),
        "max_units": gaps.max_units,
        "empty": gaps.empty,
        "hollow": gaps.hollow,
        "over_cap": gaps.over_cap,
        "over_cap_units": gaps.over_cap_units,
        "over_union": gaps.over_union,
        "fitted": gaps.fitted,
        "fitted_from_units": gaps.fitted_from_units,
        "slices": {t: sorted(site_label(u) for u in us)
                   for t, us in sorted(slices.items())},
        "sessions": [{"tests": list(p.tests), "units": len(p.units)}
                     for p in plans],
    }


@dataclass(frozen=True, slots=True)
class RoutedBody:
    unit: FnSite
    container: str
    sig_text: str
    body: str
    where_clause: str | None


@dataclass(frozen=True, slots=True)
class ChangeSet:
    routed: tuple[RoutedBody, ...]
    rejected: tuple[str, ...]   # human-readable reasons, one per dropped body

    @property
    def units(self) -> frozenset[FnSite]:
        return frozenset(r.unit for r in self.routed)


_FN_PREFIX_WORDS = frozenset({"pub", "async", "const", "unsafe", "extern", "default"})


def declares_other_fn(body: str, own_name: str) -> str | None:
    """The name of a fn DEFINITION inside `body` other than `own_name`, or
    None. A reply that sends `pub(crate) unsafe fn display(...) { … }
    pub(crate) unsafe fn debug(...) { … }` as the body of `debug` splices as
    two nested fns and dies of E0401 (run `164454-8ec9c4d4`, 2026-09-02,
    session turn 2); naming the violation costs no cargo round."""
    for raw in body.splitlines():
        ln = raw.strip()
        if not ln or ln.startswith("//"):
            continue
        # Both parens: `pub(crate)` is `pub ( crate )`. Spacing only `(` left
        # the token `crate)` which is not in the skip set, so the live
        # `pub(crate) unsafe fn display` sitting in a `debug` body (run
        # `164454-8ec9c4d4`, 2026-09-02) was not a definition.
        toks = ln.replace("(", " ( ").replace(")", " ) ").split()
        i = 0
        while i < len(toks) and (toks[i] in _FN_PREFIX_WORDS or toks[i].startswith('"')
                                 or toks[i] in ("(", "crate", ")", "super", "self", "in")
                                 or toks[i].startswith("crate")):
            i += 1
        if i + 1 < len(toks) and toks[i] == "fn":
            name = toks[i + 1].split("<", 1)[0].split("(", 1)[0]
            if name and name != own_name:
                return name
    return None


def route_change_set(
    parsed: Sequence[Mapping[str, object]],
    records: Mapping[FnSite, FnRecord],
) -> ChangeSet:
    """Match the reply's `<FN name block>` bodies to the slice's records.

    A body routes when exactly one slice unit has that fn name and, when the
    reply named a block, a container that canonicalises to the same key. A
    name shared by several slice units with no block (or a block matching
    none) is rejected with the candidates named, so the next turn can name
    one; a name outside the slice is rejected as out of scope — the editable
    set is fixed at turn 0 and a body for a fn the session may not write is
    the one answer that must never reach the tree."""
    by_name: dict[str, list[FnSite]] = {}
    for site in records:
        by_name.setdefault(site.fn_name, []).append(site)
    routed: list[RoutedBody] = []
    rejected: list[str] = []
    seen: set[FnSite] = set()
    for fd in parsed:
        name = str(fd.get("fn_name") or "")
        block = str(fd.get("block") or "")
        body = str(fd.get("body") or "")
        where = fd.get("where_clause")
        cands = by_name.get(name, [])
        if not cands:
            rejected.append(f"`{name}`: not in this session's editable set")
            continue
        if block:
            want = container_key(block)
            matched = [c for c in cands
                       if container_key(records[c].container) == want]
            if not matched:
                # Fall back to the file-unique name when the block is off.
                matched = cands if len(cands) == 1 else []
        else:
            matched = cands if len(cands) == 1 else []
        if len(matched) != 1:
            opts = ", ".join(
                f"{c.file} `{records[c].container or 'free fn'}`" for c in cands)
            rejected.append(
                f"`{name}`: ambiguous — name the impl header in block=; "
                f"candidates: {opts}")
            continue
        site = matched[0]
        if site in seen:
            rejected.append(f"`{name}` in {site.file}: given twice; first kept")
            continue
        other = declares_other_fn(body, name)
        if other is not None:
            rejected.append(
                f"`{name}`: the body carries a fn DEFINITION (`{other}`) — send "
                f"the body of `{name}` only, no signatures, one <FN> per fn")
            continue
        seen.add(site)
        rec = records[site]
        routed.append(RoutedBody(
            unit=site, container=rec.container, sig_text=rec.sig_text,
            body=body, where_clause=str(where) if where else None))
    return ChangeSet(tuple(routed), tuple(rejected))


def extract_notes(reply: str) -> str:
    """The model's own carried-forward notes, or ""."""
    i = reply.find(NOTES_OPEN)
    if i < 0:
        return ""
    j = reply.find(NOTES_CLOSE, i)
    body = reply[i + len(NOTES_OPEN):] if j < 0 else reply[i + len(NOTES_OPEN):j]
    return body.strip()


# --- the harness's turns --------------------------------------------------

@dataclass(frozen=True, slots=True)
class TargetStatus:
    """One target test after a turn: green, or red with what it said and
    what the probe observed."""

    test: str
    passing: bool
    said: str = ""
    observed: str = ""


@dataclass(frozen=True, slots=True)
class TurnReading:
    """What the suite said after a compiling change set."""

    targets: tuple[TargetStatus, ...]
    newly_red: tuple[str, ...] = ()
    newly_red_said: Mapping[str, str] = field(default_factory=dict)
    newly_red_observed: Mapping[str, str] = field(default_factory=dict)
    crashed: tuple[str, ...] = ()
    pass_delta: int = 0

    @property
    def targets_green(self) -> bool:
        return all(t.passing for t in self.targets)

    @property
    def clean(self) -> bool:
        return self.targets_green and not self.newly_red and not self.crashed

    @property
    def regressed(self) -> bool:
        return bool(self.newly_red or self.crashed)


def render_compile_turn(diagnostics: str, rejected: Sequence[str] = ()) -> str:
    lines = ["RESULT: the change set did NOT compile; it was not applied. "
             "The tree is as it was before this turn. Resolve every "
             "diagnostic below in your next change set (resend every body "
             "you want on the tree — each turn's change set is applied on "
             "the tree as it stands after the previous KEPT turn)."]
    if rejected:
        lines.append("Bodies not applied: " + "; ".join(rejected))
    lines.append("")
    lines.append(diagnostics.rstrip())
    return "\n".join(lines)


def render_rejected_only_turn(rejected: Sequence[str]) -> str:
    return ("RESULT: no body in that reply could be applied. "
            + "; ".join(rejected)
            + "\nReply with <FN name=\"…\" block=\"…\"> bodies for fns in the "
              "editable set, a <REQUEST …/>, or nothing to end the session.")


def _indent(text: str, pad: str = "    ") -> str:
    return "\n".join(f"{pad}{ln}" for ln in text.splitlines() if ln.strip())


def render_test_turn(
    reading: TurnReading, rejected: Sequence[str] = (), *,
    dropped: Sequence[str] = (), dropped_diagnostics: str = "",
    coverage_note: str = "",
) -> str:
    """The verdict turn: every target's state, every newly-red sibling with
    its printed values, the observed values beside each. The one contract
    that makes the session different from the stateless route — the model
    reads what its body PRINTED, not that a test failed.

    `dropped` names the bodies the compiler blamed and the peel took off the
    tree before judging the rest (run `164454-8ec9c4d4`, 2026-09-02, session
    turn 1: the right implementer body was discarded together with a
    sibling's type error). `coverage_note` is the measured statement of which
    changed units the still-failing targets execute (same run, turns 3–8:
    five rewrites of an implementer the dispatcher never called)."""
    lines: list[str] = []
    if reading.clean:
        lines.append("RESULT: the change set compiled, every target test "
                     "passes and no previously-passing test changed verdict. "
                     "It is KEPT.")
        if dropped:
            lines.append("  (bodies the compiler blamed were dropped first: "
                         + ", ".join(dropped) + ")")
        return "\n".join(lines)
    if dropped:
        lines.append(
            f"COMPILER: {len(dropped)} body(ies) did not compile and were DROPPED "
            "(the tree keeps their previous text); the remaining bodies of the "
            "change set are on the tree and judged below. Dropped: "
            + ", ".join(dropped) + ". Their diagnostics:")
        if dropped_diagnostics:
            lines.append(_indent(dropped_diagnostics))
    lines.append("RESULT: the change set compiled and is on the tree for "
                 "this turn. Verdicts:")
    for t in reading.targets:
        if t.passing:
            lines.append(f"  PASS {t.test}")
            continue
        lines.append(f"  FAIL {t.test} — it said:")
        if t.said:
            lines.append(_indent(t.said))
        if t.observed:
            lines.append(_indent(t.observed))
    if reading.newly_red:
        lines.append(
            f"  REGRESSION: {len(reading.newly_red)} previously-passing test(s) "
            "went red under this change set — the next change set must keep "
            "them green (`left` is what the body now produces, `right` what "
            "the test expects):")
        for name in reading.newly_red:
            lines.append(f"    {name} said:")
            said = reading.newly_red_said.get(name, "")
            if said:
                lines.append(_indent(said, "      "))
            obs = reading.newly_red_observed.get(name, "")
            if obs:
                lines.append(_indent(obs, "      "))
    if reading.crashed:
        lines.append(
            "  CRASH: test binar" + ("ies" if len(reading.crashed) > 1 else "y")
            + f" {list(reading.crashed)} aborted before reporting — a memory "
            "fault or a stub panic; a body in this change set routes execution "
            "into it.")
    if rejected:
        lines.append("  Bodies not applied: " + "; ".join(rejected))
    if coverage_note:
        lines.append("  " + coverage_note)
    lines.append(
        "Next: a change set (every body you want on the tree, applied on the "
        "tree as it now stands), a <REQUEST …/>, or an empty reply to stop.")
    return "\n".join(lines)


def coverage_note_for(
    changed: Iterable[FnSite],
    owners: Mapping[str, Collection[FnSite]],
    editable: Iterable[FnSite],
    *,
    unchanged_since_turn: int | None = None,
) -> str:
    """The measured coverage statement after a judged turn whose targets
    still fail: which of the changed units the failing targets execute,
    which they do not, and which editable units they do execute now."""
    executed: set[FnSite] = set()
    for ks in owners.values():
        executed |= set(ks)
    changed_l = sorted(changed, key=lambda u: (u.file, u.fn_name, u.container))
    reached = [u for u in changed_l if u in executed]
    missed = [u for u in changed_l if u not in executed]
    def fmt(us: Iterable[FnSite]) -> str:
        # Site labels, not `file::fn`: two `fmt` impls in one file would
        # otherwise read as the same unit named twice.
        return ", ".join(site_label(u) for u in us) or "(none)"

    parts = ["COVERAGE of the still-failing target(s) on the tree as it now stands — "
             f"changed units they EXECUTE: {fmt(reached)}; changed units they do "
             f"NOT execute: {fmt(missed)}"]
    if missed:
        parts.append("(a body the targets never enter cannot change what they "
                     "observe; route execution into it or change a unit they do "
                     "execute)")
    ed_exec = [u for u in sorted(editable, key=lambda u: (u.file, u.fn_name, u.container))
               if u in executed]
    parts.append(f"— editable units the targets execute now: {fmt(ed_exec)}.")
    if unchanged_since_turn is not None:
        parts.append(f"The observed values are byte-identical to turn "
                     f"{unchanged_since_turn}'s.")
    return " ".join(parts)


def _units_named(
    name: str,
    table: Mapping[str, Collection[FnSite]] | None,
) -> set[FnSite]:
    """Units keyed by a cargo-qualified name or its last `::` segment."""
    if not table:
        return set()
    out: set[FnSite] = set()
    bare = name.rsplit("::", 1)[-1]
    for key, units in table.items():
        if key == name or key.rsplit("::", 1)[-1] == bare:
            out.update(units)
    return out


def session_slices_for_failing(
    failing: Iterable[str],
    *,
    owners: Mapping[str, Collection[FnSite]] | None = None,
    units_by_test: Mapping[str, Collection[FnSite]] | None = None,
    pairs: Iterable[tuple[FnSite, FnSite]] = (),
) -> dict[str, frozenset[FnSite]]:
    """Per-failing-test unit sets for the red-slice session.

    Coverage keys are not the failing set: an instrumented run that
    dropped the cluster cfg never lists a cfg-gated test, so iterating
    `owners.items()` planned no session for tests attribution already
    owned (run 216855-03b2a21b, 2026-09-02, cluster nightly iters 4-9:
    the owner map was ui with 0 units). Every failing name gets a
    slice — measured owners union attributed units — then the formatter
    pair hop. plan_slices still drops empty and hollow.
    """
    raw: dict[str, set[FnSite]] = {}
    for item in failing:
        name = str(item)
        bare = name.rsplit("::", 1)[-1]
        raw.setdefault(bare, set()).update(
            _units_named(name, owners) | _units_named(name, units_by_test))
    return slices_with_pair_hops(raw, pairs)


def slices_with_pair_hops(
    slices: Mapping[str, Collection[FnSite]],
    pairs: Iterable[tuple[FnSite, FnSite]],
) -> dict[str, frozenset[FnSite]]:
    """`slices` with every dispatcher/implementer hop closed: a slice holding
    one member of a formatter pair gains the other. The implementer a
    dispatcher does not yet call is by definition not in the coverage owner
    map — run `164454-8ec9c4d4` (2026-09-02) iter 2 opened a session that
    could edit the Debug dispatcher and not `ErrorImpl::debug`, and the
    model's first turn was a request for `src/fmt.rs`."""
    out: dict[str, frozenset[FnSite]] = {}
    hop = list(pairs)
    for test, units in slices.items():
        members = set(units)
        for a, b in hop:
            if a in members or b in members:
                members |= {a, b}
        out[test] = frozenset(members)
    return out


def expand_locator_sites(
    slices: Mapping[str, Collection[FnSite]],
    tree_sites: Iterable[FnSite],
) -> dict[str, frozenset[FnSite]]:
    """A locator-only unit (empty container) becomes every site of that name
    on the tree.

    Attribution paths that know no container — the classifier, the
    reverse-map and recently-filled fallbacks, a lifted locator map, a
    vanished test's static candidates — name a `(file, fn)`, and
    `FnLocator`'s contract is that a fill route "expands the locator into
    the containers it will actually write". The session is that route: the
    model gets every same-named body, each with its own executed mark, and
    `block=` chooses, instead of the first record in file order (the
    collapse that hid `impl Debug for Error` behind the Display impl, run
    `3026-8fb154bc`, 2026-09-03). A name the tree does not declare is kept
    as given and reported missing downstream. Files compare under
    `norm_src_rel`, the two rootings extractor records and loop keys carry."""
    by_name: dict[tuple[str, str], list[FnSite]] = {}
    for site in tree_sites:
        by_name.setdefault((norm_src_rel(site.file), site.fn_name), []).append(site)
    out: dict[str, frozenset[FnSite]] = {}
    for test, units in slices.items():
        members: set[FnSite] = set()
        for u in units:
            if u.container:
                members.add(u)
                continue
            members.update(by_name.get((norm_src_rel(u.file), u.fn_name)) or (u,))
        out[test] = frozenset(members)
    return out


def render_budget_turn(turns_left: int) -> str:
    if turns_left <= 0:
        return ("This was the last turn of the session's budget. Reply with "
                f"{NOTES_OPEN} what you tried, what the printed values showed and "
                f"what remains unresolved {NOTES_CLOSE} and nothing else.")
    return f"({turns_left} turn(s) of budget remain.)"


def kept_units(
    baseline_bodies: Mapping[FnSite, str],
    current_bodies: Mapping[FnSite, str],
) -> frozenset[FnSite]:
    """The slice units whose body differs from the session baseline."""
    return frozenset(
        u for u, b in current_bodies.items()
        if baseline_bodies.get(u, b) != b)


def units_by_file(units: Iterable[FnSite]) -> dict[str, list[FnSite]]:
    out: dict[str, list[FnSite]] = {}
    for u in units:
        out.setdefault(u.file, []).append(u)
    return out
