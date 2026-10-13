"""Domain types + pure helper for failure→fn attribution.

`FailureKind` is a StrEnum (its members ARE their wire strings, so existing callers that
treat the kind as a plain string keep working) and it also answers whether the path it
names may CONVICT a unit. `line_to_fn_record` is the pure (text, line) → fn-record
mapping, moved out of pipeline.py.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, NamedTuple

from pipeline_core.domain.format_traits import format_traits_used
from pipeline_core.domain.impl_routing import norm_src_rel
from pipeline_core.domain.models import FnLocator, FnSite, Frontier, SiteFrontier
from pipeline_core.domain.rust_idents import _skip_generics, canon_token_text


class FormatterPair(NamedTuple):
    """A trait-format dispatcher and the sibling implementer its body
    delegates to, as the format-only attribution hop resolved them — both
    container-qualified, because `FnLocator` erases the impl (`Debug for
    Error::fmt` and `Display for Error::fmt` share a locator).

    Recorded whenever the hop follows a dispatcher's call to a crate
    formatting implementer, or — when the body named no crate qualifier —
    the inherent method `trait.lower()` on the tree, in EITHER attribution
    direction. The pair is a dispatch unit of its own when the two members
    live in different files:
    the paying write can be co-dependent — a delegation arm in the
    dispatcher plus the section it delegates to in the implementer — and
    file-scoped fills judged independently are each rejected by the revert
    gate while the joint state would cash (run `358833-aa15d93c`,
    2026-09-01: three `src/fmt.rs` opens and two `src/error.rs` reverts,
    no joint judgment possible)."""

    dispatcher: FnSite
    implementer: FnSite

    @property
    def cross_file(self) -> bool:
        return self.dispatcher.file != self.implementer.file

    def member_locators(self) -> frozenset[FnLocator]:
        return frozenset((
            FnLocator(self.dispatcher.file, self.dispatcher.fn_name),
            FnLocator(self.implementer.file, self.implementer.fn_name)))


def pair_for_unit(
    pairs: Iterable[FormatterPair], unit: FnLocator,
) -> FormatterPair | None:
    """The cross-file pair a dispatched unit belongs to, if any — the match
    is on file+name because dispatch keys are locators. Same-file pairs are
    never returned: file-first already writes both members under one
    judgment, so they need no route of their own."""
    for p in pairs:
        if p.cross_file and unit in p.member_locators():
            return p
    return None


class FailureKind(StrEnum):
    """Which attribution path produced a frontier (for logs/events)."""

    RUNTIME = "runtime"
    COMPILE = "compile"
    BEHAVIOR = "behavior"
    CLASSIFIER = "classifier"
    RECENTLY_FILLED = "recently_filled"
    REVERSE_MAP = "reverse_map"
    MERGED = "merged"
    # Fns reached through a test binary that produced NO verdict at all. A
    # binary that segfaults prints no `FAILED` line, so its tests convict
    # nothing and every other path is blind to them — and on `anyhow`
    # 2026-08-05 those were the only tests whose repair could have moved the
    # merged tree's score. It does NOT convict: the binary went down as a
    # whole and nothing in the output says which of its tests did it, so this
    # is "who might be responsible", never grounds to revert a fill.
    CRASHED_BINARY = "crashed_binary"
    NO_FRONTIER = "no_frontier"

    @property
    def convicts(self) -> bool:
        """True when this path OBSERVED the failure AT the unit — a panic
        located in its body, a compile error on its lines, or the failing
        test's own body naming it. Those are the only links strong enough to
        REVERT a fill. Every other path answers "who might be responsible"
        (a reachability guess, a classifier's opinion, "it was filled
        recently", the producers a futile test escalates to); widening work
        on such a guess costs compute, but judging on it makes acceptance
        unreachable by construction — the unit is asked to fix a test it
        cannot influence, so no attempt count ever helps."""
        return self in _CONVICTING_KINDS


_CONVICTING_KINDS = frozenset({
    FailureKind.RUNTIME, FailureKind.COMPILE, FailureKind.BEHAVIOR})


@dataclass(frozen=True, slots=True)
class FailureFrontier:
    """The iteration's work list, carrying BOTH of its projections so the two
    readings can never drift apart.

    `dispatch` — which unit to attempt and which failing tests motivate it.
    Over-inclusion here is cheap and often deliberate (unattributed stubs,
    producer escalation): the worst case is an attempt that changes nothing.

    `convicting` — the tests allowed to REVERT a SITE when it compiles but
    they still fail, keyed by :class:`FnSite`. Over-inclusion here is fatal,
    not merely wasteful: a unit convicted by a test it cannot influence is
    reverted on every attempt, in every iteration, forever. Locator-keyed
    maps passed to ``build`` / ``of_direct`` lift to the empty-container
    site so existing tests stay green; live attribution writes real
    containers.

    Built once via `build`, which enforces every test on site ``s`` is in
    ``dispatch[FnLocator(s.file, s.fn_name)]`` and drops sites whose locator
    is absent from dispatch — the invariant lives here, not in each fill
    route."""

    dispatch: Frontier
    convicting: SiteFrontier
    # What the failing tests SAID — panic site + assertion text per test, keyed
    # as libtest printed the name (`test_report.failure_evidence_by_test`). The
    # prompt that asks for a re-fill owes the model this: on 2026-08-20 (run
    # `902902-daee577d`) the per-impl REFILL section read "its attributed test
    # (see Tests below) still FAILS" while the tests it meant (`test_iter`,
    # `test_len`, `test_rev`, `test_clone`) were absent from the prompt and
    # their `left: 4 / right: 1` never reached the model. Carried on the
    # frontier rather than threaded as one more engine parameter because the
    # frontier already travels every seam the evidence must reach.
    evidence: Mapping[str, str] = field(default_factory=dict)
    # The failing tests' OWN source — signature + body as
    # `behavior_frontier_from_texts` extracts it. Homonym scoring
    # (`format_traits_used`) reads this channel, not `evidence`: libtest
    # failure text never carries `{:?}` (5706 evidence blocks, 0 trait
    # tokens on run `463713-b7c666c8`), so scoring evidence uniquely owned
    # nobody and dropped Debug asserts from every `fmt` homonym.
    sources: Mapping[str, str] = field(default_factory=dict)
    # Dispatcher/implementer pairs the format-only attribution hop resolved
    # (`FormatterPair`), carried on the frontier for the same reason the
    # evidence is: the pair-fill route lives at the far end of the engine
    # seams and the frontier already travels all of them. Only cross-file
    # pairs ever dispatch (`pair_for_unit`).
    pairs: frozenset[FormatterPair] = frozenset()
    # Failing tests (bare names) whose first failure is inside a
    # still-stubbed function (`test_outcome.StubHold`): their words are that
    # stub's own panic and say nothing about any body. Every other failing
    # test on a unit OBSERVED it (`observed_tests_for`), and a stub's first
    # fill shows what those printed (run `39786-fd38e72f` fill 3: an owed
    # test's `unwrap()` on `None` reached none of its stub's prompts).
    held: frozenset[str] = frozenset()
    # Per unit, a note every prompt that prints this unit's evidence carries:
    # the compiler's words on its last body when its last owed offer missed
    # on a compile failure (`refill_evidence.unit_notes_text`).
    unit_notes: Mapping[FnLocator, str] = field(default_factory=dict)
    # Per unit, the red tests that unit owes this sweep (`DueReading.tests_for`).
    # Stub and re-fill excerpts print these before the other observed tests.
    # Empty keeps alphabetical order.
    owed: Mapping[FnLocator, frozenset[str]] = field(default_factory=dict)

    @classmethod
    def build(
        cls, dispatch: Frontier,
        convicting: Mapping[FnLocator, set[str]] | Mapping[FnSite, set[str]],
        evidence: Mapping[str, str] | None = None,
        sources: Mapping[str, str] | None = None,
        pairs: Iterable[FormatterPair] = (),
        held: Iterable[str] = (),
        unit_notes: Mapping[FnLocator, str] | None = None,
        owed: Mapping[FnLocator, Iterable[str]] | None = None,
    ) -> FailureFrontier:
        sites: SiteFrontier = {}
        for key, tests in convicting.items():
            site = key_as_site(key)
            loc = FnLocator(site.file, site.fn_name)
            if loc not in dispatch:
                continue
            kept = set(tests) & dispatch[loc]
            if kept:
                sites.setdefault(site, set()).update(kept)
        # A pair travels with the frontier only while a failing test is
        # dispatched against one of its members — the same invariant as
        # conviction above. `dispatch_including_pairs` already leaves out a
        # pair naming no test on either endpoint; carrying it here anyway
        # handed the pair route a run with no aimed test (run
        # `484518-91681c1a` fill 5, 2026-09-23: both members' tests had
        # moved to a still-stubbed producer and been deferred, the pairs
        # opened first and bought nothing).
        live_pairs = frozenset(
            p for p in pairs
            if any(dispatch.get(loc) for loc in p.member_locators()))
        owed_out: dict[FnLocator, frozenset[str]] = {}
        for key, names in (owed or {}).items():
            loc = key if isinstance(key, FnLocator) else FnLocator(key[0], key[1])
            owed_out[loc] = frozenset(str(t) for t in names)
        return cls(dict(dispatch), sites,
                   dict(evidence or {}), dict(sources or {}),
                   live_pairs, frozenset(t.rsplit("::", 1)[-1] for t in held),
                   dict(unit_notes or {}), owed_out)

    @classmethod
    def of_direct(cls, attributed: Frontier) -> FailureFrontier:
        """A frontier whose every attribution rests on direct evidence — the
        two projections coincide."""
        return cls(
            dict(attributed),
            {key_as_site(k): set(v) for k, v in attributed.items() if v})

    @classmethod
    def of_dispatch(cls, dispatch: Frontier) -> FailureFrontier:
        """A frontier whose attributions carry no direct evidence — every unit
        is attempted, none can be reverted for a still-failing test. The
        compile gate and the global non-regression ratchet still apply."""
        return cls(dict(dispatch), {})

    def tests_for(self, key: FnLocator) -> set[str]:
        return self.dispatch.get(key, set())

    def observed_tests_for(self, key: FnLocator) -> set[str]:
        """`tests_for(key)` minus the tests held in a still-stubbed function:
        the tests whose words are about a body, not about a stub."""
        return {t for t in self.tests_for(key)
                if t.rsplit("::", 1)[-1] not in self.held}

    def tests_for_body(self, file: str, fn_name: str, container: str) -> set[str]:
        """The tests attributed to this body.

        The dispatch map keys `(file, fn_name)`, so it is the reading when
        this name has one container or the convicting map does not split
        the containers. When convicting names two or more containers of
        this name, each body gets only the tests that convict that
        container: a test of one body does not attribute to the other.
        A container the map does not name gets nothing — the locator fact
        does not stand for a body the sweep already told apart."""
        loc = set(self.tests_for(FnLocator(file, fn_name)))
        if not container:
            return loc
        want = norm_src_rel(file)
        distinguished = {
            container_key(site.container)
            for site in self.convicting
            if site.fn_name == fn_name and norm_src_rel(site.file) == want
            and site.container}
        if len(distinguished) < 2:
            return loc
        return set(self.convicting_for_site(FnSite(file, fn_name, container)))

    def convicting_for(self, key: FnLocator) -> set[str]:
        """Union of every site with this file+name — file-first U2 still
        keys on locators. Homonym-precise reads use
        :meth:`convicting_for_site`."""
        out: set[str] = set()
        for site, tests in self.convicting.items():
            if site.file == key.file and site.fn_name == key.fn_name:
                out |= tests
        return out

    def convicting_for_site(self, site: FnSite) -> set[str]:
        """Tests that convict this canonical site, exactly.

        Empty-container of_direct rows are a different site (``container=""``)
        and are NOT mixed in: a Debug lookup must not inherit a locator-lifted
        Debug-assert that of_direct parked on the bare ``fmt`` bucket. The
        stamp's scoring fallback still reads :meth:`convicting_for` for those
        of_direct fixtures.
        """
        want = key_as_site(site)
        return set(self.convicting.get(want, set()))

    def _named(self, mapping: Mapping[str, str], test: str) -> str:
        """Exact name first; a bare name (the escalation's `test_rev`) matches
        a printed `chain::test_rev` on the last `::` segment, the same rule
        `oracle_ledger.bare_test_name` applies when reconciling the two forms."""
        if test in mapping:
            return mapping[test]
        leaf = test.rsplit("::", 1)[-1]
        for name, text in mapping.items():
            if name.rsplit("::", 1)[-1] == leaf:
                return text
        return ""

    def evidence_for(self, test: str) -> str:
        """The failing test's own words (libtest panic / assertion), or ""."""
        return self._named(self.evidence, test)

    def source_for(self, test: str) -> str:
        """The failing test's own source (signature + body), or ""."""
        return self._named(self.sources, test)

    def __bool__(self) -> bool:
        return bool(self.dispatch)


class AttributionResult(NamedTuple):
    """`frontier` is the DISPATCH map (every path's attributions unioned);
    `convicting` is the direct-evidence subset keyed by :class:`FnSite` (see
    `FailureKind.convicts`). Both are returned together so a caller cannot
    reach for one and silently get the other. Locator-keyed maps from tests
    and fallbacks lift to the empty-container site at write."""

    frontier: Frontier
    kind: FailureKind
    convicting: SiteFrontier
    # Test-own source keyed as attribution named the test. Empty when the
    # behavior path did not run (no `test_texts`). Default `{}` so existing
    # three-argument constructors stay valid.
    sources: Mapping[str, str] = {}
    # The formatter dispatcher/implementer pairs the behavior path's hop
    # resolved this sweep (`FormatterPair`). Empty when the hop did not fire.
    pairs: frozenset[FormatterPair] = frozenset()
    # Every site the attribution paths named this sweep, container-qualified.
    # The session route reads this; dispatch keeps the locator-keyed frontier.
    site_frontier: SiteFrontier = {}
    # Per failing test, the text its failure is ABOUT: the panicking
    # statement cut at the panicking call, plus the `let`s it reads
    # (`domain.failing_observable`). Absent for a test whose failure
    # location is not inside its own fn.
    observables: Mapping[str, str] = {}

    def convicting_for(self, key: FnLocator) -> set[str]:
        """Union over sites with this file+name — the locator view tests
        used when convicting was itself a Frontier."""
        out: set[str] = set()
        for k, tests in self.convicting.items():
            if k[0] == key.file and k[1] == key.fn_name:
                out |= tests
        return out

    def observable_traits_for(self, test: str) -> frozenset[str]:
        """The format traits the test's FAILING observable renders through.

        The question the format-implementer hop, the futility-escalation
        withhold and the product sustain ask. Read on the observable when
        the failure location is inside the test's own fn, else on the whole
        source (the reading those three had before the observable existed).
        `assertion_owned_by_container` asks a different question and keeps
        the whole source: on an empty trait set it falls through to a
        tie-of-zeros keep that stamps every homonym."""
        obs = self.observables.get(test)
        if obs is None:
            leaf = test.rsplit("::", 1)[-1]
            obs = next((v for k, v in self.observables.items()
                        if k.rsplit("::", 1)[-1] == leaf), None)
        return format_traits_used(obs if obs is not None
                                  else self.source_for(test))

    def source_for(self, test: str) -> str:
        """The failing test's own source (signature + body), or "". Exact
        name first, then last-`::`-segment match — cargo prints
        `module::test` where attribution may have keyed the bare name, the
        same reconciliation `FailureFrontier._named` applies."""
        if test in self.sources:
            return self.sources[test]
        leaf = test.rsplit("::", 1)[-1]
        for name, text in self.sources.items():
            if name.rsplit("::", 1)[-1] == leaf:
                return text
        return ""


def key_as_site(key: FnLocator | FnSite | tuple[str, ...]) -> FnSite:
    """Lift a dispatch locator (or a 2-tuple) to the empty-container site;
    canonicalise an already-qualified site. ``build`` uses this so locator-
    keyed test fixtures keep working after convicting became a SiteFrontier."""
    if isinstance(key, FnSite) or (isinstance(key, tuple) and len(key) == 3):
        return canon_site(FnSite(key[0], key[1], key[2]))
    return FnSite(key[0], key[1], "")


def canon_site(site: FnSite) -> FnSite:
    """Identity comparison form: container run through :func:`container_key`."""
    cont = site.container or ""
    return FnSite(site.file, site.fn_name, container_key(cont) if cont else "")


def record_site(record: Any) -> FnSite:
    """The :class:`FnSite` for an extractor :class:`~pipeline_core.domain.models.FnRecord`."""
    if hasattr(record, "file"):
        return canon_site(FnSite(record.file, record.fn_name, record.container or ""))
    return canon_site(FnSite(
        str(record.get("file", "")),
        str(record.get("fn_name", "")),
        str(record.get("container", "") or ""),
    ))


def locator_of(site: FnSite) -> FnLocator:
    """Project a site to the dispatch key the outer loop uses."""
    return FnLocator(site.file, site.fn_name)


def site_label(site: FnSite) -> str:
    """The log form of a site: `src/error.rs::fmt (impl Debug for Error)`;
    a free fn renders as `src/lib.rs::run`."""
    base = f"{site.file}::{site.fn_name}"
    return f"{base} ({site.container})" if site.container else base


def site_payload(site: FnSite) -> dict[str, str]:
    """The event form of a site. Structured, never a `file::fn` string:
    `::` occurs inside containers (`impl fmt::Display for Widget`), and the
    flat form dropped the container from every session and owner-map event
    the loop wrote, so the collapse of two `fmt` impls onto one key was not
    detectable from `events.jsonl` (run `3026-8fb154bc`, 2026-09-03)."""
    return {"file": site.file, "fn": site.fn_name, "container": site.container}


def site_from_payload(obj: object) -> FnSite:
    """Inverse of :func:`site_payload`. A legacy `file::fn` string reads as a
    locator-only site (empty container) so events written before the
    structured form stay loadable."""
    if isinstance(obj, Mapping):
        return canon_site(FnSite(
            str(obj.get("file", "")), str(obj.get("fn", "")),
            str(obj.get("container", "") or "")))
    file_rel, _, fn = str(obj).rpartition("::")
    return FnSite(file_rel, fn, "")


@dataclass(frozen=True)
class SiteRecords:
    """A tree's fn records keyed by site, plus the sites the tree declares
    more than once after cfg selection (each with its arms' cfg gates)."""

    by_site: Mapping[FnSite, Any]
    collisions: tuple[tuple[FnSite, tuple[str, ...]], ...] = ()


def records_by_site(
    records: Iterable[Any], *, compiled: Callable[[Any], bool],
) -> SiteRecords:
    """One record per `FnSite`, chosen the way the build chooses.

    The extractor emits every declared variant of a fn: both arms of a
    `#[cfg(x)]` / `#[cfg(not(x))]` twin in one impl carry the same
    `(file, fn_name, container)` under every `--cfg-true` setting (probed
    2026-09-03 on a two-arm inherent impl). `compiled` is the cluster's cfg
    verdict on a record — `CfgSet.excludes_for_cargo` over its predicates —
    and among colliding records only the compiled arms may key the site.
    What still collides after that (an undecidable twin, two inline modules
    declaring one free fn) keeps the first record in file order and is
    reported in `collisions`, never raised: the ValueError this replaces
    aborted the whole fill phase on a property of the tree, not a fault of
    the call that noticed it, and the live anyhow trees carry no collision
    while the first crate with a cfg twin would have died on iteration 1."""
    groups: dict[FnSite, list[Any]] = {}
    for rec in records:
        groups.setdefault(record_site(rec), []).append(rec)
    by_site: dict[FnSite, Any] = {}
    collisions: list[tuple[FnSite, tuple[str, ...]]] = []
    for site, group in groups.items():
        kept = [r for r in group if compiled(r)] or group
        by_site[site] = kept[0]
        if len(kept) > 1:
            collisions.append((site, tuple(
                " && ".join(getattr(r, "cfg_predicates", ()) or ()) or "(no cfg)"
                for r in kept)))
    return SiteRecords(by_site, tuple(collisions))


def lift_sites(dispatch: Frontier) -> SiteFrontier:
    """Empty-container sites for a locator-only map (monkeypatches, fallbacks).

    Keys are read positionally: a `Frontier` key is structurally a 2-tuple
    (`FnLocator`'s own contract), and the classifier and reverse-map
    fallbacks — and their test fixtures — build theirs as bare tuples. An
    attribute read (`k.file`) raised on those the first time a
    non-convicting kind was lifted into the site frontier (2026-09-03)."""
    return {FnSite(str(k[0]), str(k[1]), ""): set(v)
            for k, v in dispatch.items() if v}


def as_dispatch(raw: Frontier | tuple[Frontier, SiteFrontier]) -> Frontier:
    """Public extractors may return ``(dispatch, sites)``; fix-loop still
    wants the dispatch map alone."""
    if isinstance(raw, tuple):
        return raw[0]
    return raw


def container_key(container: str) -> str:
    """A container header in its comparable form: everything before the body
    brace, whitespace collapsed, then every space dropped unless it separates
    two identifier characters.

    Two printers produce container text and identity comparisons must not
    split on their formatting. Whitespace-collapse ALONE is not enough: the
    extractor's rendered header is token-spaced (`impl Context < T , E > for
    Result < T , E >`) while a routed body's header is source-verbatim
    (`impl<T, E> Context<T, E> for Result<T, E>`), and comparing the merely
    collapsed forms keys them apart. That is the identical two-printers class
    `installable_fns._canon` records for field types (five sweeps of silent
    offers, 2026-08-22), and it recurred here on the peel's first live
    outing: run `264297-dd681686`'s context.rs compile failure resolved its
    diagnostics to extractor-shaped keys, matched none of the source-shaped
    landed bodies, and refused `none_implicated` — the whole-reply revert the
    per-body peel exists to prevent (2026-08-24 ~22:5x UTC). The space
    canonicalization is `rust_idents.canon_token_text`, the one mechanism
    both sides of every such seam go through.

    Spacing is only half of that seam: the extractor's `render_impl_header`
    also OMITS the impl's generics list (`impl<T, E> Context<T, E> for
    Result<T, E>` renders as `impl Context < T , E > for Result < T , E >`)
    and never carries a `where` clause, while a routed body's source-shaped
    header can carry both. The comparable form therefore drops a leading
    `impl<...>` generics list and everything from a depth-0 ` where ` on —
    the identity the extractor's own container convention already uses.
    Distinct impls stay distinct: the self/trait types keep their generic
    ARGUMENTS (`impl Tr for X<u32>` vs `impl Tr for X<T>`)."""
    t = " ".join(container.split("{", 1)[0].split())
    if t.startswith("impl"):
        rest = t[len("impl"):].lstrip()
        if rest.startswith("<"):
            rest = _skip_generics(rest)
        t = "impl " + rest.lstrip()
    depth = 0
    for i, ch in enumerate(t):
        if ch in "<([":
            depth += 1
        elif ch in ">)]":
            depth -= 1
        elif depth == 0 and t.startswith(" where ", i):
            t = t[:i]
            break
    return canon_token_text(t)


def line_to_fn_record(
    file_text: str, line_no: int, fn_records: list[dict[str, Any]],
    col_no: int | None = None,
) -> dict[str, Any] | None:
    """Given file text and a 1-based (line, col), return the fn record whose body
    spans that position, else None. `fn_records` must already be filtered to the
    file. The extractor emits compact one-line-per-item output, so a line can hold
    several fns; `col_no` (when present) disambiguates, otherwise the first fn whose
    body range overlaps the line wins."""
    if line_no < 1 or not fn_records:
        return None
    lines_iter = file_text.splitlines(keepends=True)
    if line_no > len(lines_iter):
        return None
    line_start = sum(len(ln) for ln in lines_iter[:line_no - 1])
    line_end = line_start + len(lines_iter[line_no - 1])
    if col_no is not None and col_no >= 1:
        byte_offset = line_start + col_no - 1
        for rec in fn_records:
            s = rec.get("sig_byte_start", 0)
            e = rec.get("body_byte_end", 0)
            if s <= byte_offset < e:
                return rec
    # Fallback: pick the first fn whose body overlaps the line range.
    for rec in fn_records:
        s = rec.get("sig_byte_start", 0)
        e = rec.get("body_byte_end", 0)
        if max(s, line_start) < min(e, line_end):
            return rec
    return None


def union_unattributed_stubs(
    frontier: Frontier, remaining_stubs: list[FnLocator],
) -> Frontier:
    """Add every remaining `unimplemented!()` stub to the frontier so a helper
    that NO failing test attributes directly (a leaf called only transitively,
    e.g. inside another fn's test) still gets filled in phase-c.

    Without this, an un-attributed stub survives phase-c entirely — it never
    enters the test→fn frontier, so it's only filled by the post-TASK3
    whole-file sweep, while its tested callers thrash as "blocked by an unfilled
    sibling stub" until the cluster plateaus. An un-attributed stub carries an
    EMPTY test set (the u2 gate then accepts it on compile; the outer TDD loop
    re-tests). Already-attributed keys keep their test set unchanged. Returns a
    new dict; inputs are not mutated."""
    merged: Frontier = {key: set(tests) for key, tests in frontier.items()}
    for key in remaining_stubs:
        merged.setdefault(key, set())
    return merged
