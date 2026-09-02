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

from pipeline_core.domain.attribution import container_key
from pipeline_core.domain.models import FnLocator, FnRecord

NOTES_OPEN = "<NOTES>"
NOTES_CLOSE = "</NOTES>"


@dataclass(frozen=True, slots=True)
class SessionSlice:
    """One session's work: the failing tests it answers for and the units
    it may write, with the units grouped by file for rendering."""

    tests: tuple[str, ...]
    units: tuple[FnLocator, ...]

    @property
    def files(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(u.file for u in self.units))

    def label(self) -> str:
        return f"session[{len(self.tests)} test(s), {len(self.units)} unit(s)]"

    def member_scope(self) -> frozenset[str]:
        return frozenset(f"{u.file}::{u.fn_name}" for u in self.units)


def plan_slices(
    slices: Mapping[str, Collection[FnLocator]],
    *,
    stubs: Collection[FnLocator] = (),
    max_units: int = 32,
) -> list[SessionSlice]:
    """Group the failing tests into sessions: tests whose unit sets overlap
    share one session (the union). A test whose slice touches a still-hollow
    body is left to the stub routes — a session rewrites behaviour, and a
    verdict behind a stub is not a behaviour verdict. A merged slice over
    `max_units` is dropped rather than truncated: a session that may only
    write half the executed path cannot be judged as one transaction.

    Deterministic: tests sorted, units sorted, output ordered by first test.
    """
    hollow = frozenset(stubs)
    pending: dict[str, frozenset[FnLocator]] = {}
    for test in sorted(slices):
        units = frozenset(slices[test])
        if not units or units & hollow:
            continue
        pending[test] = units
    groups: list[tuple[list[str], set[FnLocator]]] = []
    for test, units in pending.items():
        merged_into: tuple[list[str], set[FnLocator]] | None = None
        for grp in groups:
            if grp[1] & units:
                if merged_into is None:
                    grp[0].append(test)
                    grp[1].update(units)
                    merged_into = grp
                else:
                    # A test bridging two groups joins them.
                    merged_into[0].extend(grp[0])
                    merged_into[1].update(grp[1])
                    grp[0].clear()
                    grp[1].clear()
        if merged_into is None:
            groups.append(([test], set(units)))
    out: list[SessionSlice] = []
    for names, members in groups:
        if not names or len(members) > max_units:
            continue
        out.append(SessionSlice(
            tests=tuple(sorted(dict.fromkeys(names))),
            units=tuple(sorted(members, key=lambda u: (u.file, u.fn_name)))))
    return out


@dataclass(frozen=True, slots=True)
class RoutedBody:
    unit: FnLocator
    container: str
    sig_text: str
    body: str
    where_clause: str | None


@dataclass(frozen=True, slots=True)
class ChangeSet:
    routed: tuple[RoutedBody, ...]
    rejected: tuple[str, ...]   # human-readable reasons, one per dropped body

    @property
    def units(self) -> frozenset[FnLocator]:
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
    records: Mapping[FnLocator, FnRecord],
) -> ChangeSet:
    """Match the reply's `<FN name block>` bodies to the slice's records.

    A body routes when exactly one slice unit has that fn name and, when the
    reply named a block, a container that canonicalises to the same key. A
    name shared by several slice units with no block (or a block matching
    none) is rejected with the candidates named, so the next turn can name
    one; a name outside the slice is rejected as out of scope — the editable
    set is fixed at turn 0 and a body for a fn the session may not write is
    the one answer that must never reach the tree."""
    by_name: dict[str, list[FnLocator]] = {}
    for loc in records:
        by_name.setdefault(loc.fn_name, []).append(loc)
    routed: list[RoutedBody] = []
    rejected: list[str] = []
    seen: set[FnLocator] = set()
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
        loc = matched[0]
        if loc in seen:
            rejected.append(f"`{name}` in {loc.file}: given twice; first kept")
            continue
        other = declares_other_fn(body, name)
        if other is not None:
            rejected.append(
                f"`{name}`: the body carries a fn DEFINITION (`{other}`) — send "
                f"the body of `{name}` only, no signatures, one <FN> per fn")
            continue
        seen.add(loc)
        rec = records[loc]
        routed.append(RoutedBody(
            unit=loc, container=rec.container, sig_text=rec.sig_text,
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
    changed: Iterable[FnLocator],
    owners: Mapping[str, Collection[FnLocator]],
    editable: Iterable[FnLocator],
    *,
    unchanged_since_turn: int | None = None,
) -> str:
    """The measured coverage statement after a judged turn whose targets
    still fail: which of the changed units the failing targets execute,
    which they do not, and which editable units they do execute now."""
    executed: set[FnLocator] = set()
    for ks in owners.values():
        executed |= set(ks)
    changed_l = sorted(changed, key=lambda u: (u.file, u.fn_name))
    reached = [u for u in changed_l if u in executed]
    missed = [u for u in changed_l if u not in executed]
    def fmt(us: Iterable[FnLocator]) -> str:
        return ", ".join(f"{u.file}::{u.fn_name}" for u in us) or "(none)"

    parts = ["COVERAGE of the still-failing target(s) on the tree as it now stands — "
             f"changed units they EXECUTE: {fmt(reached)}; changed units they do "
             f"NOT execute: {fmt(missed)}"]
    if missed:
        parts.append("(a body the targets never enter cannot change what they "
                     "observe; route execution into it or change a unit they do "
                     "execute)")
    ed_exec = [u for u in sorted(editable, key=lambda u: (u.file, u.fn_name)) if u in executed]
    parts.append(f"— editable units the targets execute now: {fmt(ed_exec)}.")
    if unchanged_since_turn is not None:
        parts.append(f"The observed values are byte-identical to turn "
                     f"{unchanged_since_turn}'s.")
    return " ".join(parts)


def _units_named(
    name: str,
    table: Mapping[str, Collection[FnLocator]] | None,
) -> set[FnLocator]:
    """Units keyed by a cargo-qualified name or its last `::` segment."""
    if not table:
        return set()
    out: set[FnLocator] = set()
    bare = name.rsplit("::", 1)[-1]
    for key, units in table.items():
        if key == name or key.rsplit("::", 1)[-1] == bare:
            out.update(units)
    return out


def session_slices_for_failing(
    failing: Iterable[str],
    *,
    owners: Mapping[str, Collection[FnLocator]] | None = None,
    units_by_test: Mapping[str, Collection[FnLocator]] | None = None,
    pairs: Iterable[tuple[FnLocator, FnLocator]] = (),
) -> dict[str, frozenset[FnLocator]]:
    """Per-failing-test unit sets for the red-slice session.

    Coverage keys are not the failing set: an instrumented run that
    dropped the cluster cfg never lists a cfg-gated test, so iterating
    `owners.items()` planned no session for tests attribution already
    owned (run 216855-03b2a21b, 2026-09-02, cluster nightly iters 4-9:
    the owner map was ui with 0 units). Every failing name gets a
    slice — measured owners union attributed units — then the formatter
    pair hop. plan_slices still drops empty and hollow.
    """
    raw: dict[str, set[FnLocator]] = {}
    for item in failing:
        name = str(item)
        bare = name.rsplit("::", 1)[-1]
        raw.setdefault(bare, set()).update(
            _units_named(name, owners) | _units_named(name, units_by_test))
    return slices_with_pair_hops(raw, pairs)


def slices_with_pair_hops(
    slices: Mapping[str, Collection[FnLocator]],
    pairs: Iterable[tuple[FnLocator, FnLocator]],
) -> dict[str, frozenset[FnLocator]]:
    """`slices` with every dispatcher/implementer hop closed: a slice holding
    one member of a formatter pair gains the other. The implementer a
    dispatcher does not yet call is by definition not in the coverage owner
    map — run `164454-8ec9c4d4` (2026-09-02) iter 2 opened a session that
    could edit the Debug dispatcher and not `ErrorImpl::debug`, and the
    model's first turn was a request for `src/fmt.rs`."""
    out: dict[str, frozenset[FnLocator]] = {}
    hop = list(pairs)
    for test, units in slices.items():
        members = set(units)
        for a, b in hop:
            if a in members or b in members:
                members |= {a, b}
        out[test] = frozenset(members)
    return out


def render_budget_turn(turns_left: int) -> str:
    if turns_left <= 0:
        return ("This was the last turn of the session's budget. Reply with "
                f"{NOTES_OPEN} what you tried, what the printed values showed and "
                f"what remains unresolved {NOTES_CLOSE} and nothing else.")
    return f"({turns_left} turn(s) of budget remain.)"


def kept_units(
    baseline_bodies: Mapping[FnLocator, str],
    current_bodies: Mapping[FnLocator, str],
) -> frozenset[FnLocator]:
    """The slice units whose body differs from the session baseline."""
    return frozenset(
        u for u, b in current_bodies.items()
        if baseline_bodies.get(u, b) != b)


def units_by_file(units: Iterable[FnLocator]) -> dict[str, list[FnLocator]]:
    out: dict[str, list[FnLocator]] = {}
    for u in units:
        out.setdefault(u.file, []).append(u)
    return out
