"""Use-case edges: compositions of two public items, and which an example
already asserts (`domain/use_cases.py`).

Invented vocabulary throughout. The class: each public item right alone on
the inputs its test wrote, and a composition nobody observes.
"""
from __future__ import annotations

from pathlib import Path

from pipeline_core.domain.behaviour import (
    BehaviourSpec,
    BehaviourSubKind,
    GlossaryEntry,
    ObservableOutcome,
)
from pipeline_core.domain.behaviour_probe import CapturedProbe, other_observes
from pipeline_core.domain.use_cases import (
    UseCaseEdge,
    asserted_edges,
    edges_to_ask,
    input_types,
    result_types,
    use_case_edges,
)
from pipeline_core.prompts.behaviour_synthesis import build_behaviour_synthesis_prompt

GLOSSARY = (
    GlossaryEntry("Report", "struct"),
    GlossaryEntry("Tally", "struct"),
    GlossaryEntry("Report::build", "fn", signature="pub fn build<M>(message: M) -> Self where M: Into<String>"),
    GlossaryEntry("Report::wrap", "fn", signature="pub fn wrap(self, note: &str) -> Report"),
    GlossaryEntry("Report::trace", "fn", signature="pub fn trace(&self) -> &std::backtrace::Backtrace"),
    GlossaryEntry("Report::lines", "fn", signature="pub fn lines(&mut self) -> Vec<String>"),
    GlossaryEntry("Tally::new", "fn", signature="pub fn new() -> Self"),
    GlossaryEntry("Tally::add", "fn", signature="pub fn add<F: Fn(u8) -> u8>(&mut self, r: &Report, f: F)"),
    GlossaryEntry("parse", "fn", signature="pub fn parse(text: &str) -> Result<Report, String>"),
    GlossaryEntry("Render::render", "fn", signature="fn render(&self) -> String"),
    GlossaryEntry("Render", "trait"),
    GlossaryEntry("report!", "macro"),
)
TYPES = {"Report", "Tally"}


def _by(name: str) -> GlossaryEntry:
    return next(g for g in GLOSSARY if g.name == name)


def _spec(bid: str, setup: str, when: str) -> BehaviourSpec:
    return BehaviourSpec(bid, setup, when, ObservableOutcome.display("x"),
                         subkind=BehaviourSubKind.OUTCOME)


def test_self_and_the_receiver_are_resolved_from_the_item_s_own_type() -> None:
    assert result_types(_by("Report::build"), TYPES) == {"Report"}          # `-> Self`
    assert result_types(_by("parse"), TYPES) == {"Report"}                  # inside a wrapper
    assert result_types(_by("Report::trace"), TYPES) == set()               # a std type
    assert input_types(_by("Report::trace"), TYPES) == {"Report"}           # `&self`
    assert input_types(_by("Report::lines"), TYPES) == {"Report"}           # `&mut self`
    assert input_types(_by("Tally::add"), TYPES) == {"Tally", "Report"}     # receiver + parameter
    assert input_types(_by("Report::build"), TYPES) == set()                # no receiver
    # a trait's method takes its implementor, which the declaration does not name
    assert input_types(_by("Render::render"), TYPES) == set()
    edges = use_case_edges(GLOSSARY)
    assert UseCaseEdge("Report::trace", "Report::build", "Report") in edges
    assert UseCaseEdge("Report::trace", "parse", "Report") in edges
    assert UseCaseEdge("Tally::add", "Tally::new", "Tally") in edges
    assert UseCaseEdge("Tally::add", "Report::wrap", "Report") in edges
    assert not any(e.consumer == e.producer for e in edges)
    assert not any("report!" in (e.consumer, e.producer) for e in edges)    # no declaration

    # NEGATIVE CONTROL: type names alone (no `Self`, no `self`) connect a
    # constructor to nothing — only parameters that spell the type remain.
    spelled = {UseCaseEdge(c.name, p.name, t)
               for p in GLOSSARY for c in GLOSSARY
               if p.kind == c.kind == "fn" and p.name != c.name
               for t in TYPES
               if t in p.signature.split("->")[-1] and t in c.signature.split("->")[0]}
    assert UseCaseEdge("Report::trace", "Report::build", "Report") not in spelled
    assert len(spelled) < len(edges)


def test_an_edge_is_asserted_only_when_an_example_observes_the_consumer_s_result() -> None:
    edge = UseCaseEdge("Report::trace", "Report::build", "Report")
    called = _spec("a", 'let r = Report::build("x"); let _ = r.trace();', "r.to_string()")
    observed = _spec("b", 'let r = Report::build("x");', "r.trace().to_string()")
    assert asserted_edges([edge], [called]) == frozenset()
    assert asserted_edges([edge], [observed]) == frozenset({edge})

    # NEGATIVE CONTROL: "both names occur in one example's code" marks the
    # composition exercised although nothing reads what `trace` returned.
    from pipeline_core.domain.behaviour import example_code
    code = example_code(called)
    assert "build" in code and "trace" in code


def test_the_asks_spread_over_consumers_and_producers_under_the_cap() -> None:
    specs = [_spec("b", 'let r = Report::build("x");', "r.wrap(\"n\").to_string()")]
    ask, asserted, total = edges_to_ask(GLOSSARY, specs, max_edges=4)
    assert total == len(use_case_edges(GLOSSARY)) and asserted == 1
    assert len(ask) == 4 and len({e.consumer for e in ask}) == 4          # one each first
    assert len({e.producer for e in ask}) > 1                              # not one constructor
    assert UseCaseEdge("Report::wrap", "Report::build", "Report") not in ask
    assert edges_to_ask(GLOSSARY, specs, max_edges=0)[0] == ()
    everything, _a, _t = edges_to_ask(GLOSSARY, specs, max_edges=999)
    assert len(everything) == total - asserted and len(set(everything)) == len(everything)


def test_the_composition_ask_names_both_items_and_no_value() -> None:
    prompt = build_behaviour_synthesis_prompt(
        module_name="krate", cfg_cluster="stable", target_units=(), public_surface="pub fn parse();",
        symbol_inventory="", target_edges=(
            ("Report::build", _by("Report::build").signature,
             "Report::trace", _by("Report::trace").signature),))
    assert "build with `Report::build`" in prompt and "then observe `Report::trace`" in prompt
    assert "<OBSERVE>display|debug|alt_debug</OBSERVE>" in prompt and "PUBLIC_API_SURFACE" in prompt
    assert other_observes(CapturedProbe("n", "", "r.trace()", "display")) == ("debug", "alt_debug")
    assert other_observes(CapturedProbe("n", "", "r.is::<T>()", "display")) == ()


def test_a_composition_round_is_asked_in_t0d_and_observed_under_every_rendering(tmp_path: Path) -> None:
    from pipeline_core.domain.models import BuildCluster, FnRecord, TypeRecord
    from tests.test_task0d import _cluster, _World

    assert isinstance(_cluster(), BuildCluster)
    src = ("pub struct Report;\nimpl Report {\n    pub fn build() -> Self { Report }\n"
           "    pub fn trace(&self) -> u8 { 1 }\n}\n")
    enc = src.encode()
    fns = [FnRecord(file="lib.rs", fn_name="build", container="impl Report", is_pub=True,
                    is_exactly_pub=True, sig_text="pub fn build() -> Self",
                    sig_byte_start=enc.index(b"pub fn build")),
           FnRecord(file="lib.rs", fn_name="trace", container="impl Report", is_pub=True,
                    is_exactly_pub=True, sig_text="pub fn trace(&self) -> u8",
                    sig_byte_start=enc.index(b"pub fn trace"))]
    types = [TypeRecord(file="lib.rs", kind="struct", name="Report", is_pub=True, is_exactly_pub=True)]
    reply = ('<BEHAVIOUR name="build_then_trace">\n<GIVEN>\n    let r = Report::build();\n</GIVEN>\n'
             "<WHEN>r.trace()</WHEN>\n<OBSERVE>display</OBSERVE>\n</BEHAVIOUR>")
    w = _World(reply=reply)

    def asker(world: _World):  # type: ignore[no-untyped-def]
        def ask(prompt: str) -> str:
            world.prompts.append(prompt)
            return reply if "compositions of two public items" in prompt else ""
        return ask

    over: dict[str, object] = {
        "ask": asker(w),
        "read_sources": lambda sd: [("lib.rs", src)],
        "enumerate_fns": lambda sd: fns, "enumerate_types": lambda sd: types,
        "PROBE_EDGES_PER_CLUSTER": 4, "PROBE_CLASSES_PER_CLUSTER": 0,
    }
    from pipeline_core.application.tasks.task0d import run_task0d
    run_task0d(w.deps(**over), tmp_path, "krate", [_cluster()], tmp_path / "src")
    edge_prompts = [p for p in w.prompts if "compositions of two public items" in p]
    assert len(edge_prompts) == 1
    assert "build with `Report::build` (`pub fn build() -> Self`)" in edge_prompts[0]
    assert "then observe `Report::trace` (`pub fn trace(&self) -> u8`)" in edge_prompts[0]
    captured = [s for s in w.saved["stable"].specs if "build_then_trace" in s.behaviour_id]
    assert sorted(s.then.kind.name for s in captured) == [  # type: ignore[union-attr]
        "ALT_DEBUG_STRING", "DEBUG_STRING", "DISPLAY_STRING"]
    assert len({s.behaviour_id for s in captured}) == 3
    line = next(ln for ln in w.logs if "behaviours —" in ln)
    assert "0 of 1 public-API edge(s) asserted by an example" in line

    # NEGATIVE CONTROL: with no composition round (the pipeline before it),
    # nothing asks about the pair and nothing observes `trace`.
    w0 = _World(reply=reply)
    run_task0d(w0.deps(**{**over, "ask": asker(w0), "PROBE_EDGES_PER_CLUSTER": 0}), tmp_path, "krate",
               [_cluster()], tmp_path / "src")
    assert not [p for p in w0.prompts if "compositions of two public items" in p]
    assert "public-API edge(s)" not in next(ln for ln in w0.logs if "behaviours —" in ln)
    assert not [s for s in w0.saved["stable"].specs if "trace" in s.when]
