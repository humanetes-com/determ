"""The behaviour book is built ONCE for every build world and projected into
each world's store.

Run `72627-751534fe` (2026-09-30, anyhow, `stable` + `nightly`): the strip
bake built the book twice, one call at a time, 10,090 s and 10,639 s, over
227 examples identical in every field but `cfg_cluster`; the two prompts
differed by one glossary line (`Error::thiserror_provide`, nightly only).
The plan's sentences these pin: a union build asks each feature once
whatever the number of worlds (negative control: one book per cluster asks
once per world); serial and parallel lanes give the same book and reading;
a name only some worlds compile is shown once, marked, and reaches only
those worlds' stores; a residue feature reaches only its worlds; an id whose
Then differs between worlds keeps both Thens. The shapes are invented: three
worlds `a`, `b`, `c`, one core fn and one fn gated on a cfg atom only `b`
and `c` set.
"""
from __future__ import annotations

import re
import threading
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path

import pytest

from pipeline_core.application.behaviour_book_build import BookBudget, build_behaviour_book
from pipeline_core.application.strip_bake import BakeArtifactsDeps, bake_skeleton_artifacts
from pipeline_core.application.tasks.task0d import Task0dDeps, build_world_book, run_task0d
from pipeline_core.domain.behaviour import (
    BehaviourSpec,
    BehaviourSubKind,
    BehaviourSuite,
    Feature,
    GlossaryEntry,
    ObservableOutcome,
    Rule,
    then_value_text,
)
from pipeline_core.domain.behaviour_book import glossary_entries, private_names
from pipeline_core.domain.cfg_worlds import partition, world_mark, worlds_of
from pipeline_core.domain.models import BuildCluster, FnRecord, SkeletoniseResult
from pipeline_core.domain.world_book import book_is_current, project_book, union_glossary, union_worlds
from pipeline_core.prompts.behaviour_rules import build_rule_authoring_prompt, glossary_lines

_ATOM = "gate_x"


def _cluster(name: str, *, gated: bool) -> BuildCluster:
    return BuildCluster(
        name=name, env_extra={}, tarpaulin_exclude_files=[],
        excluded_cfg=[] if gated else [_ATOM], active_cfg=[_ATOM] if gated else [],
        prompt_nightly_rule="", is_default=name == "a")


def _spec(bid: str, when: str, value: str, world: str) -> BehaviourSpec:
    return BehaviourSpec(bid, "use crate_x::*;", when, ObservableOutcome.display(value),
                         subkind=BehaviourSubKind.OUTCOME, cfg_cluster=world)


_RECORDS = [
    FnRecord(file="lib.rs", fn_name="core_fn", is_pub=True, is_exactly_pub=True),
    FnRecord(file="lib.rs", fn_name="gated_fn", is_pub=True, is_exactly_pub=True,
             cfg_predicates=(_ATOM,)),
]
_SOURCES = [("lib.rs", "/// Core.\npub fn core_fn() {}\n/// Gated.\npub fn gated_fn() {}\n")]


def _suites(worlds: Sequence[str], *, gated_in: Sequence[str] = (),
            differ_in: str = "") -> dict[str, BehaviourSuite]:
    out: dict[str, BehaviourSuite] = {}
    for w in worlds:
        specs = [_spec(f"t::core::{i}", f"core_fn({i})", f"v{i}", w) for i in range(5)]
        if w in gated_in:
            specs += [_spec(f"t::gated::{i}", f"gated_fn({i})", f"g{i}", w) for i in range(2)]
        if differ_in:
            specs.append(_spec("t::core::moves", "core_fn(9)",
                               "other" if w == differ_in else "same", w))
        out[w] = BehaviourSuite(cfg_cluster=w, specs=tuple(specs))
    return out


class _Model:
    """A deterministic, thread-safe fake: keyed by the prompt, never by call
    order. The draft covers the chunk's first example; the prediction is
    right only when a rule is given, so every draft is kept. `twin` adds a
    second draft saying the same thing, so the set judge spends its
    leave-one-out calls and keeps one copy."""

    def __init__(self, values: Mapping[str, str], *, twin: bool = False) -> None:
        self.values = dict(values)
        self.twin = twin
        self.prompts: list[str] = []
        self._lock = threading.Lock()

    def ask(self, prompt: str) -> str:
        with self._lock:
            self.prompts.append(prompt)
        ids = re.findall(r'<EXAMPLE id="([^"]+)">', prompt)
        if prompt.startswith("You are writing the specification"):
            twin = f'<RULE covers="{ids[0]}">Renders the value.</RULE>' if self.twin else ""
            return f'<RULE covers="{ids[0]}">Returns the rendered value.</RULE>{twin}'
        ruled = "RULES (hold together" in prompt
        return "\n".join(f'<THEN id="{i}">{self.values[i] if ruled else "wrong"}</THEN>' for i in ids)

    def drafts(self) -> int:
        return sum(1 for p in self.prompts if p.startswith("You are writing the specification"))


def _values(suites: Mapping[str, BehaviourSuite]) -> dict[str, str]:
    u = union_worlds(suites)
    return {s.behaviour_id: then_value_text(s.then) for s in u.suite.specs if s.then}


def _deps(model: _Model, *, lanes: int = 1, logs: list[str] | None = None,
          events: list[tuple[str, dict[str, object]]] | None = None,
          saved: dict[str, BehaviourSuite] | None = None,
          features: dict[str, dict[str, str]] | None = None) -> Task0dDeps:
    logs = [] if logs is None else logs
    events = [] if events is None else events
    saved = {} if saved is None else saved
    features = {} if features is None else features
    return Task0dDeps(
        log=logs.append, emit=lambda ev, **f: events.append((ev, f)),
        mark_phase=lambda *_a, **_k: None, phase_valid=lambda _p, _k: False,
        commit_and_mark=lambda *_a, **_k: None,
        read_sources=lambda _sd: list(_SOURCES), read_test_sources=lambda _pd: [],
        harvest=lambda *_a, **_k: BehaviourSuite(cfg_cluster=""),
        save_suite=lambda pd, s: saved.__setitem__(s.cfg_cluster, s) or pd,
        load_suite=lambda _pd, c: BehaviourSuite(cfg_cluster=c),
        ask=model.ask, public_surface=lambda _p, _c: "", symbol_inventory=lambda _p, _c: "",
        enumerate_fns=lambda _sd: list(_RECORDS), enumerate_mods=lambda _sd: [],
        enumerate_types=lambda _sd: [],
        run_probe=lambda *_a, **_k: None,  # type: ignore[arg-type,return-value]
        exercises_by_coverage=lambda *_a, **_k: {},
        BOOK_BUDGET=BookBudget(examples_per_prompt=2, lanes=lanes),
        write_features=lambda _pd, c, f: features.__setitem__(c, dict(f)),
    )


def _world_book(suites: Mapping[str, BehaviourSuite], clusters: Sequence[BuildCluster],
                model: _Model, **kw: object) -> dict[str, BehaviourSuite]:
    return build_world_book(_deps(model, **kw), Path("."), "crate_x", clusters, suites,  # type: ignore[arg-type]
                            list(_RECORDS), [], [], list(_SOURCES))


# ---------------------------------------------------------------- the model

def test_partition_places_an_element_by_the_worlds_that_compile_it() -> None:
    clusters = [_cluster("a", gated=False), _cluster("b", gated=True), _cluster("c", gated=True)]
    p = partition([("core", ()), ("gated", (_ATOM,)), ("off", (f"not({_ATOM})",))],
                  lambda e: worlds_of(e[1], clusters), ["a", "b", "c"])
    assert p.core == (("core", ()),)
    assert p.residue == {"a": (("off", (f"not({_ATOM})",)),),
                         "b": (("gated", (_ATOM,)),), "c": (("gated", (_ATOM,)),)}
    assert [p.mark(x) for x in p.placed] == [(), ("b", "c"), ("a",)]
    assert world_mark(["a", "b", "c"], ["a", "b", "c"]) == ()


# ----------------------------------------------------- one ask per example

def test_a_union_build_asks_each_feature_once_whatever_the_number_of_worlds() -> None:
    clusters = [_cluster("a", gated=False), _cluster("b", gated=False)]
    suites = _suites(["a", "b"])
    union_model = _Model(_values(suites))
    books = _world_book(suites, clusters, union_model)
    single = _Model(_values(suites))
    _world_book({"a": suites["a"]}, clusters[:1], single)
    assert union_model.drafts() == single.drafts() == 3  # 5 core examples, 2 per prompt
    assert len(union_model.prompts) == len(single.prompts)
    assert books["a"].features and books["b"].features
    assert [f.to_dict() for f in books["a"].features] == [f.to_dict() for f in books["b"].features]

    # Negative control: the per-cluster path — one book per world — asks
    # every core example once per world.
    control = _Model(_values(suites))
    for c in clusters:
        g = glossary_entries(_RECORDS, [], _SOURCES)
        build_behaviour_book(suites[c.name], crate="crate_x", glossary=g,
                             private=private_names(_RECORDS, [], g), ask=control.ask,
                             budget=BookBudget(examples_per_prompt=2))
    assert control.drafts() == 2 * union_model.drafts()


def test_the_book_asks_through_its_own_binding_and_leaves_ask_to_the_programs() -> None:
    """The book's drafts go to `book_ask` (bound with `BEHAVIOUR_BOOK_THINK`);
    `ask`, which the probe and rule-probe program calls use, gets none of them.
    Control: with no `book_ask` the same drafts go to `ask`, as before."""
    clusters = [_cluster("a", gated=False)]
    suites = _suites(["a"])
    program_model, book_model = _Model(_values(suites)), _Model(_values(suites))
    deps = replace(_deps(program_model), book_ask=book_model.ask)
    build_world_book(deps, Path("."), "crate_x", clusters, suites,  # type: ignore[arg-type]
                     list(_RECORDS), [], [], list(_SOURCES))
    assert book_model.drafts() == 3 and program_model.drafts() == 0

    control = _Model(_values(suites))
    _world_book(suites, clusters, control)
    assert control.drafts() == 3


def test_the_book_reasons_only_when_the_setting_asks(monkeypatch: pytest.MonkeyPatch) -> None:
    from pipeline_core.settings import Settings
    monkeypatch.delenv("BEHAVIOUR_BOOK_THINK", raising=False)
    assert Settings.from_env().behaviour_book_think is False
    monkeypatch.setenv("BEHAVIOUR_BOOK_THINK", "1")
    assert Settings.from_env().behaviour_book_think is True


def test_serial_and_parallel_lanes_build_the_same_book_and_reading() -> None:
    clusters = [_cluster("a", gated=False), _cluster("b", gated=True), _cluster("c", gated=True)]
    suites = _suites(["a", "b", "c"], gated_in=["b", "c"], differ_in="c")
    serial_logs: list[str] = []
    parallel_logs: list[str] = []
    serial_model = _Model(_values(suites), twin=True)
    serial = _world_book(suites, clusters, serial_model, lanes=1, logs=serial_logs)
    parallel = _world_book(suites, clusters, _Model(_values(suites), twin=True), lanes=4,
                           logs=parallel_logs)
    assert {k: v.to_dict() for k, v in serial.items()} == {k: v.to_dict() for k, v in parallel.items()}
    assert [ln.replace("lanes=4", "lanes=1") for ln in parallel_logs] == serial_logs
    assert any("lanes=4" in ln for ln in parallel_logs)
    # the set judge ran: leave-one-out calls carry one of the two drafts,
    # and each chunk keeps one copy of the twin sentences (the first)
    loo = [p for p in serial_model.prompts if "RULES (hold together" in p and "\n2. " not in p]
    drafted = serial_model.drafts()
    assert len(loo) == 2 * drafted
    assert any(f"not necessary {drafted}" in ln for ln in serial_logs)
    assert all(r.sentence == "Returns the rendered value."
               for b in serial.values() for r in b.rules)


# ------------------------------------------------- residue reaches its worlds

def test_a_residue_name_is_shown_once_marked_and_reaches_only_its_worlds() -> None:
    clusters = [_cluster("a", gated=False), _cluster("b", gated=True), _cluster("c", gated=True)]
    suites = _suites(["a", "b", "c"], gated_in=["b", "c"])
    model = _Model(_values(suites))
    events: list[tuple[str, dict[str, object]]] = []
    books = _world_book(suites, clusters, model, events=events)
    names = {w: {g.name: g.worlds for g in b.glossary} for w, b in books.items()}
    assert "gated_fn" not in names["a"]
    assert names["b"]["gated_fn"] == names["c"]["gated_fn"] == ("b", "c")
    assert names["a"]["core_fn"] == ()
    features = {w: {f.item for f in b.features} for w, b in books.items()}
    assert "gated_fn" not in features["a"] and "gated_fn" in features["b"] and "gated_fn" in features["c"]
    # The one prompt that drafted gated_fn's rules names its worlds.
    drafted = [p for p in model.prompts if p.startswith("You are writing") and "gated_fn(0)" in p]
    assert len(drafted) == 1 and "only in the b, c build" in drafted[0]
    assert [ev for ev, _ in events] == ["task0d.behaviour_book"]
    assert events[0][1]["worlds"] == ["a", "b", "c"] and events[0][1]["residue_names"] == 1


def test_an_id_whose_then_differs_between_worlds_keeps_both_thens() -> None:
    clusters = [_cluster("a", gated=False), _cluster("c", gated=False)]
    suites = _suites(["a", "c"], differ_in="c")
    u = union_worlds(suites)
    variants = {s.behaviour_id: then_value_text(s.then) for s in u.suite.specs
                if s.behaviour_id.startswith("t::core::moves")}
    assert variants == {"t::core::moves@a": "same", "t::core::moves@c": "other"}
    assert u.marks() == {"t::core::moves@a": ("a",), "t::core::moves@c": ("c",)}
    books = _world_book(suites, clusters, _Model(_values(suites)))
    for w in ("a", "c"):
        cited = {i for f in books[w].features for r in f.rules for i in r.example_ids}
        ids = {i for f in books[w].features for i in f.example_ids}
        assert "t::core::moves" in ids and not any("@" in i for i in ids | cited)


def test_a_rules_refuted_examples_are_projected_to_local_ids_like_its_credited_ones() -> None:
    """Negative control: a projection that rewrites only `example_ids`
    leaves union ids (`id@worlds`) in a world's store."""
    suites = _suites(["a", "c"], differ_in="c")
    u = union_worlds(suites)
    rule = Rule("r0", "Returns the rendered value.", ("t::core::0",), "core_fn",
                refuted_on=("t::core::moves@c", "t::core::1"))
    book = replace(u.suite, features=(Feature("core_fn", "fn", ("t::core::0", "t::core::1",
                                                               "t::core::moves@c"), (rule,)),))
    c = project_book(book, u, "c", suites["c"])
    a = project_book(book, u, "a", suites["a"])
    assert c.features[0].rules[0].refuted_on == ("t::core::moves", "t::core::1")
    assert a.features[0].rules[0].refuted_on == ("t::core::1",)
    assert "@" in replace(rule).refuted_on[0]


def test_the_glossary_union_marks_what_a_world_lacks_and_the_prompt_says_so() -> None:
    g = union_glossary({"a": [GlossaryEntry("x", "fn")],
                        "b": [GlossaryEntry("x", "fn"), GlossaryEntry("y", "fn", "Only b.")]})
    assert [(e.name, e.worlds) for e in g] == [("x", ()), ("y", ("b",))]
    text = glossary_lines(g)
    assert "- `y` (fn; only in the b build): Only b." in text and "- `x` (fn)" in text
    assert "built in more than one configuration" in text
    assert "configuration" not in glossary_lines([GlossaryEntry("x", "fn")])
    spec = _spec("e1", "x()", "1", "b")
    prompt = build_rule_authoring_prompt(crate="c", feature="y", feature_lines=[g[1]], glossary=g,
                                         examples=[spec], worlds={"e1": ("b",)})
    assert "BUILT ONLY IN: b" in prompt
    assert GlossaryEntry.from_dict(g[1].to_dict()) == g[1] and "worlds" not in g[0].to_dict()


def test_a_signature_survives_both_glossary_merges_and_reaches_the_vocabulary_line() -> None:
    """The record carrying the doc sentence and the one carrying the
    declaration may differ (two cfg'd definitions of one spelling, or two
    worlds); each field is taken where it is present. Negative control: the
    whole-entry replacement both merges used, which keeps the entry with
    the summary and loses the signature."""
    records = [
        FnRecord(file="a.rs", fn_name="pick", is_pub=True, is_exactly_pub=True,
                 sig_text="fn pick(self)\n    -> Result<u8, Self>"),
        FnRecord(file="b.rs", fn_name="pick", is_pub=True, is_exactly_pub=True,
                 sig_text="fn pick(self) -> Result<u8, Self>", sig_byte_start=0),
    ]
    sources = [("a.rs", "pub fn pick(self) -> Result<u8, Self> {}\n"),
               ("b.rs", "/// Picks one.\npub fn pick(self) -> Result<u8, Self> {}\n")]
    records[1] = replace(records[1], sig_byte_start=sources[1][1].index("pub fn"))
    (entry,) = glossary_entries(records, [], sources)
    assert (entry.summary, entry.signature) == ("Picks one.", "fn pick(self) -> Result<u8, Self>")
    assert "- `pick` (fn) `fn pick(self) -> Result<u8, Self>`: Picks one." in glossary_lines([entry])
    assert GlossaryEntry.from_dict(entry.to_dict()) == entry

    bare = GlossaryEntry("pick", "fn", signature=entry.signature)
    doc = GlossaryEntry("pick", "fn", "Picks one.")
    (u,) = union_glossary({"a": [bare], "b": [doc]})
    assert (u.summary, u.signature) == ("Picks one.", entry.signature)

    def whole_entry(prior: GlossaryEntry, g: GlossaryEntry) -> GlossaryEntry:
        return g if not prior.summary and g.summary else prior
    assert whole_entry(bare, doc).signature == ""


# ---------------------------------------------------------- both callers

def test_t0d_builds_one_book_after_every_cluster_and_logs_it_once(tmp_path: Path) -> None:
    """Negative control is the pre-change shape: one `behaviour-book:` line
    and one event per cluster."""
    suites = _suites(["nightly", "stable"])
    logs: list[str] = []
    events: list[tuple[str, dict[str, object]]] = []
    saved: dict[str, BehaviourSuite] = {}
    model = _Model(_values(suites))
    deps = replace(_deps(model, logs=logs, events=events, saved=saved),
                   harvest=lambda _s, *, cfg_cluster, **_k: suites[cfg_cluster],
                   PROBE_MAX_PER_CLUSTER=0, EXERCISES_ENABLED=False)
    clusters = [_cluster("stable", gated=False), _cluster("nightly", gated=True)]
    run_task0d(deps, tmp_path, "crate_x", clusters, tmp_path / "src")
    book_lines = [ln for ln in logs if "behaviour-book:" in ln]
    assert len(book_lines) == 1 and book_lines[0].startswith("  [task0d] worlds nightly+stable:")
    assert [ev for ev, _ in events].count("task0d.behaviour_book") == 1
    assert saved["stable"].features and saved["nightly"].features
    assert model.drafts() == 3


def test_the_bake_builds_one_book_for_every_world_when_a_store_has_none(tmp_path: Path) -> None:
    assert _bake(tmp_path, _suites(["nightly", "stable"])) == [("nightly", "stable")]


def _bake(tmp_path: Path, earlier: Mapping[str, BehaviourSuite]) -> list[tuple[str, ...]]:
    (tmp_path / "src").mkdir(parents=True, exist_ok=True)
    (tmp_path / "src" / "lib.rs").write_text("pub fn f() {}\n", encoding="utf-8")
    calls: list[tuple[str, ...]] = []

    def book(pd: Path, mod: str, cls: Sequence[BuildCluster],
             suites: Mapping[str, BehaviourSuite], src: Path) -> dict[str, BehaviourSuite]:
        calls.append(tuple(sorted(suites)))
        return dict(suites)

    def extract(src: Path, out: Path, cluster: object = None) -> SkeletoniseResult:
        Path(out).mkdir(parents=True, exist_ok=True)
        return SkeletoniseResult(True, [], "")

    deps = BakeArtifactsDeps(
        log=lambda _m: None,
        cfg_scan=lambda _pd: [_cluster("stable", gated=False), _cluster("nightly", gated=True)],
        prune_clusters=lambda cl, _pd, cargo_toml_path=None: cl,
        fn_name_counts=lambda _sd: {}, extract_skeleton=extract,
        is_gen_tests_file=lambda _n: False, gen_tests_origin_cfgs=lambda _t: {},
        save_behaviour_suite=lambda pd, _s: pd,
        enumerate_types=lambda _sd: [], enumerate_fns=lambda _sd: [],
        enumerate_mods=lambda _sd: [], enumerate_uses=lambda _sd: [],
        save_private_surface=lambda pd, _s: pd,
        load_behaviour_suite=lambda _pd, c: earlier[c], build_book=book)
    assert bake_skeleton_artifacts(tmp_path, "crate_x", deps)["ok"]
    return calls


def test_two_per_cluster_books_are_rebuilt_as_one_and_a_world_book_is_kept(tmp_path: Path) -> None:
    """The augmented tag after run `72627-751534fe` held one book per
    cluster, 16 features each, 2 rules kept in one and 3 in the other. A
    bake that builds only when a store has NO book (the negative control
    below: every store has features) would keep both forever, and the
    launch the plan names would never build the world book."""
    clusters = [_cluster("nightly", gated=True), _cluster("stable", gated=False)]
    suites = _suites(["nightly", "stable"])
    per_cluster = {}
    for w, s in suites.items():
        g = glossary_entries(_RECORDS, [], _SOURCES)
        per_cluster[w], _r = build_behaviour_book(
            s, crate="crate_x", glossary=g, private=set(), ask=_Model(_values(suites)).ask,
            budget=BookBudget(examples_per_prompt=2))
    assert all(s.features for s in per_cluster.values())  # the old gate's "has a book"
    assert not book_is_current(per_cluster)
    assert _bake(tmp_path / "per_cluster", per_cluster) == [("nightly", "stable")]

    world = _world_book(suites, clusters, _Model(_values(suites)))
    assert book_is_current(world)
    assert all(s.book_worlds == ("nightly", "stable") for s in world.values())
    assert _bake(tmp_path / "world", world) == []
    # A book built for another world set is not current either.
    assert not book_is_current({"stable": world["stable"]})
    rt = BehaviourSuite.from_dict(world["nightly"].to_dict())
    assert rt.book_worlds == ("nightly", "stable")
