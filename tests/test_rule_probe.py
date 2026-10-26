"""Rule probes: a kept rule tested on cases it was not drafted from
(`domain/rule_probe.py`, `task0d.run_rule_probes`). Invented shapes.

The class: a sentence the cited Thens cannot falsify is kept as written
(a kept rule said a fn returning a `Result` "yields Some"; its cited Thens
were booleans).
"""
from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from pathlib import Path

from pipeline_core.application.emit_behaviour_tests import render_behaviour_test_file
from pipeline_core.application.tasks.task0d import run_rule_probes
from pipeline_core.domain.behaviour import (
    BehaviourSpec,
    BehaviourSubKind,
    BehaviourSuite,
    Feature,
    GlossaryEntry,
    ObservableOutcome,
    Rule,
)
from pipeline_core.domain.behaviour_probe import ProbeResult
from pipeline_core.domain.held_out import is_held_out
from pipeline_core.domain.rule_probe import (
    RuleTrial,
    apply_trials,
    judge_rule_on_captures,
    rule_probe_asks,
    without_examples,
)
from pipeline_core.prompts.behaviour_rules import build_rule_probe_prompt
from tests.test_task0d import _cluster, _World

BASE = "The message is `failed: ` then the condition."
SUFFIX = "For a comparison the base message above is followed by both values in parentheses."
WRONG = "`Report::take` yields Some of the value."


def _cap(n: int, when: str, value: str, unit: str = "u") -> BehaviourSpec:
    return BehaviourSpec(f"probe::stable::{unit}::{n}", "let r = make();", when,
                         ObservableOutcome.display(value), subkind=BehaviourSubKind.OUTCOME)


class _Checker:
    def __init__(self, answer: Callable[[frozenset[str], str], str | None]) -> None:
        self.answer = answer
        self.calls: list[tuple[tuple[str, ...], tuple[str, ...]]] = []

    def __call__(self, rules: Sequence[str], ids: Sequence[str]) -> dict[str, str]:
        self.calls.append((tuple(rules), tuple(ids)))
        return {i: v for i in ids if (v := self.answer(frozenset(rules), i)) is not None}


def test_a_rule_is_confirmed_with_its_companions_and_refuted_only_when_wrong_alone_too() -> None:
    caps = {"c1": _cap(1, "a()", "failed: x (1 vs 2)"), "c2": _cap(2, "b()", "failed: y (3 vs 4)")}

    def pair(rules: frozenset[str], i: str) -> str:
        full = caps[i].then.value  # type: ignore[union-attr]
        if BASE in rules and SUFFIX in rules:
            return full
        return full.split(" (")[0] if BASE in rules else "assertion failed"

    # A suffix sentence predicts only together with its base: confirmed, not refuted.
    trial = judge_rule_on_captures(SUFFIX, [BASE], caps, predict=_Checker(pair))
    assert trial == RuleTrial(confirmed=("c1", "c2"), refuted=(), judged=2, calls=2)
    assert not trial.drops

    # NEGATIVE CONTROL: the same sentence predicted alone is wrong on every
    # capture — judged one sentence with no companion, a correct half is dropped.
    alone = judge_rule_on_captures(SUFFIX, [], caps, predict=_Checker(pair))
    assert alone.refuted == ("c1", "c2") and alone.drops

    # A set that misses because of a COMPANION is not this rule's refutation:
    # the rule alone answers right.
    def poisoned(rules: frozenset[str], i: str) -> str:
        if not rules:
            return "no"
        return "poisoned" if WRONG in rules else caps[i].then.value  # type: ignore[union-attr]
    checker = _Checker(poisoned)
    spared = judge_rule_on_captures(BASE, [WRONG], caps, predict=checker)
    assert spared.refuted == () and spared.confirmed == () and not spared.drops
    assert spared.calls == len(checker.calls) == 3 and checker.calls[2] == ((BASE,), ("c1", "c2"))

    # ... and a rule wrong with its set AND alone is refuted on that capture only.
    def one_wrong(rules: frozenset[str], i: str) -> str | None:
        if not rules:
            return "no"
        return "Some(1)" if i == "c1" else caps[i].then.value  # type: ignore[union-attr]
    mixed = judge_rule_on_captures(WRONG, [BASE], caps, predict=_Checker(one_wrong))
    assert (mixed.confirmed, mixed.refuted, mixed.drops) == (("c2",), ("c1",), False)

    # Silence confirms nothing and refutes nothing.
    silent = judge_rule_on_captures(WRONG, [], caps, predict=_Checker(lambda r, i: None))
    assert silent == RuleTrial(judged=2, calls=2) and not silent.drops
    assert judge_rule_on_captures(WRONG, [], {}, predict=_Checker(lambda r, i: "x")) == RuleTrial()


def _book() -> tuple[Feature, ...]:
    good = Rule("rule::f::0", BASE, ("e1", "e2"), "f")
    bad = Rule("rule::f::1", WRONG, ("e3",), "f", refuted_on=("e2",))
    return (Feature("f", "fn", ("e1", "e2", "e3"), (good, bad)),)


def test_trials_update_the_book_and_a_withdrawn_capture_leaves_it() -> None:
    features, dropped = apply_trials(
        _book(), {"rule::f::0": ["c1", "c2"], "rule::f::1": ["c3"]},
        {"rule::f::0": RuleTrial(("c1",), ("c2",), 2, 2), "rule::f::1": RuleTrial((), ("c3",), 1, 2)})
    assert dropped == ("rule::f::1",)
    (f,) = features
    assert f.example_ids == ("e1", "e2", "e3", "c1", "c2", "c3")      # captures stay, every one
    (rule,) = f.rules
    assert rule.example_ids == ("e1", "e2", "c1") and rule.refuted_on == ("c2",)
    pruned = without_examples(features, {"c1", "e1", "e2"})
    assert pruned[0].example_ids == ("e3", "c2", "c3") and pruned[0].rules == ()
    assert without_examples(features, set()) == features
    asks = rule_probe_asks(_book(), max_rules=5, shown=1)
    assert [(a.rule_id, a.shown, a.near) for a in asks] == [
        ("rule::f::0", ("e1",), ()), ("rule::f::1", ("e3",), ("e2",))]
    assert rule_probe_asks(_book(), max_rules=0) == ()


def test_the_ask_shows_code_never_a_value_and_an_emitted_capture_carries_its_rule() -> None:
    shown = _cap(0, "r.take()", "SECRET-VALUE")
    prompt = build_rule_probe_prompt(
        crate="krate", cfg_cluster="stable", sentence=WRONG,
        glossary=(GlossaryEntry("Report::take", "fn", signature="pub fn take(self) -> Result<u8, Self>"),),
        shown=[shown], near=[_cap(9, "r.take_near()", "OTHER-SECRET")])
    assert WRONG in prompt and "r.take()" in prompt and "r.take_near()" in prompt
    assert "pub fn take(self) -> Result<u8, Self>" in prompt
    assert "SECRET-VALUE" not in prompt and "OTHER-SECRET" not in prompt
    suite = BehaviourSuite("stable", (shown,), features=(
        Feature("Report::take", "fn", (shown.behaviour_id,),
                (Rule("rule::t::0", "A  two\nline rule.", (shown.behaviour_id,), "Report::take"),)),))
    text = render_behaviour_test_file(suite.specs, crate="krate")
    assert "// rule: A two line rule.\n#[test]\nfn behaviour_probe_stable_u_0()" in text
    # NEGATIVE CONTROL: a capture no rule credits is written as before.
    assert "// rule:" not in render_behaviour_test_file((_cap(1, "x()", "v"),), crate="krate")


def _stage(tmp_path: Path, *, share: int = 0, rules: int = 5) -> tuple[BehaviourSuite, _World, list[str]]:
    e1 = BehaviourSpec("testassert::tests/t.rs::t::0", "let r = make();", "r.show()",
                       ObservableOutcome.display("failed: a"), subkind=BehaviourSubKind.OUTCOME)
    e2 = BehaviourSpec("testassert::tests/t.rs::t::1", "let r = make();", "r.take()",
                       ObservableOutcome.display("true"), subkind=BehaviourSubKind.OUTCOME)
    book = BehaviourSuite("stable", (e1, e2), features=(
        Feature("show", "fn", (e1.behaviour_id,), (Rule("rule::show::0", BASE, (e1.behaviour_id,), "show"),)),
        Feature("take", "fn", (e2.behaviour_id,), (Rule("rule::take::0", WRONG, (e2.behaviour_id,), "take"),))),
        glossary=(GlossaryEntry("show", "fn"), GlossaryEntry("take", "fn")))
    w = _World()
    emitted: list[str] = []

    def ask(prompt: str) -> str:
        w.prompts.append(prompt)
        if prompt.startswith("Test a rule"):
            name, when = ("show_more", "r.show_other()") if BASE in prompt else ("take_more", "r.take_other()")
            return (f'<BEHAVIOUR name="{name}">\n<GIVEN>\n    let r = make();\n</GIVEN>\n'
                    f"<WHEN>{when}</WHEN>\n<OBSERVE>display</OBSERVE>\n</BEHAVIOUR>")
        ids = re.findall(r'<EXAMPLE id="([^"]+)">', prompt)
        with_rules = "RULES (hold together" in prompt
        # the checker follows the sentence: right for the show rule, "Some(..)" for the take rule
        return "".join(
            f'<THEN id="{i}">{("failed: b" if "show" in i else "Some(1)") if with_rules else "?"}</THEN>'
            for i in ids)

    def run_probe(pd: Path, cl: object, program: str, name: str, **_k: object) -> ProbeResult:
        return ProbeResult(True, "failed: b\n" if "show_other" in program else "Ok(1)\n", "")

    def emit_tests(pd: Path, c: str, specs: object, **_k: object) -> tuple[None, tuple[str, ...], list[dict[str, str]], tuple[()]]:
        emitted.extend(s.behaviour_id for s in specs)  # type: ignore[attr-defined]
        return None, (), [], ()

    deps = w.deps(ask=ask, run_probe=run_probe, emit_tests=emit_tests, RULE_PROBES_MAX=rules,
                  HELD_OUT_SHARE=share,
                  save_held_out=lambda pd, s: w.saved.__setitem__("held", s))
    out = run_rule_probes(deps, tmp_path, "krate", _cluster(), book, default_cluster="stable", memo={})
    return out, w, emitted


def test_the_stage_confirms_one_rule_drops_the_refuted_one_and_keeps_both_captures(tmp_path: Path) -> None:
    out, w, emitted = _stage(tmp_path)
    show, take = out.features
    new_show, new_take = "probe::stable::show_more::0", "probe::stable::take_more::0"
    assert show.rules[0].example_ids == ("testassert::tests/t.rs::t::0", new_show)
    assert take.rules == ()                                  # "yields Some" met `Ok(1)`
    assert new_take in take.example_ids                      # the capture stays an example
    assert {new_show, new_take} <= {s.behaviour_id for s in out.specs} and {new_show, new_take} <= set(emitted)
    assert next(s for s in out.specs if s.behaviour_id == new_show).rules[0].sentence == BASE
    line = next(ln for ln in w.logs if "rule-probes:" in ln)
    assert "2 rule(s) asked" in line and "2 captured" in line and "1 confirmed, 1 refuted" in line
    assert "1 rule(s) dropped (refuted on every new capture)" in line
    assert dict(w.events)["task0d.rule_probes"]["rules_dropped"] == ["rule::take::0"]
    assert "stable" in w.features                            # the view is rewritten

    # NEGATIVE CONTROL: without the stage the unfalsified sentence is kept.
    kept, w0, emitted0 = _stage(tmp_path, rules=0)
    assert kept.features[1].rules[0].sentence == WRONG and not emitted0 and not w0.prompts


def test_two_worlds_ask_each_rule_once(tmp_path: Path) -> None:
    """The shared ask joins the worlds that carry the rule and unions the
    examples. A reply handed in is not asked again."""
    from pipeline_core.application.tasks.task0d import _rule_replies

    stable = BehaviourSpec(
        "ex-s", "let v = 1;", "v", ObservableOutcome.display("1"),
        subkind=BehaviourSubKind.OUTCOME, cfg_cluster="stable")
    nightly = BehaviourSpec(
        "ex-n", "let v = 2;", "v", ObservableOutcome.display("2"),
        subkind=BehaviourSubKind.OUTCOME, cfg_cluster="nightly")

    def book(name: str, spec: BehaviourSpec, glossary: tuple[GlossaryEntry, ...]) -> BehaviourSuite:
        return BehaviourSuite(
            name, (spec,),
            features=(
                Feature(
                    "item", "fn", (spec.behaviour_id,),
                    (Rule("rule::item::0", "v is the input.", (spec.behaviour_id,), "item"),),
                ),
            ),
            glossary=glossary,
        )

    books = {
        "stable": book("stable", stable, (GlossaryEntry("show", "fn", signature="fn show()"),)),
        "nightly": book("nightly", nightly, (
            GlossaryEntry("show", "fn"), GlossaryEntry("only_nightly", "fn"))),
    }
    w = _World()
    replies = _rule_replies(
        w.deps(RULE_PROBES_MAX=1, RULE_PROBE_PROGRAMS=1), "krate",
        [_cluster("stable"), _cluster("nightly", default=False)],
        books, frozenset({"stable", "nightly"}))
    assert set(replies) == {"rule::item::0"} and len(w.prompts) == 1
    assert "Cluster: stable+nightly" in w.prompts[0]
    assert 'id="ex-s"' in w.prompts[0] and 'id="ex-n"' in w.prompts[0]
    assert "only_nightly" in w.prompts[0] and "only in the nightly build" in w.prompts[0]

    again = _World()

    def ask(prompt: str) -> str:
        again.prompts.append(prompt)
        return "<THEN id=\"ex-s\">1</THEN>"

    run_rule_probes(
        again.deps(ask=ask, RULE_PROBES_MAX=1), tmp_path, "krate", _cluster(),
        books["stable"], default_cluster="stable", memo={}, prepared=replies)
    assert not [p for p in again.prompts if p.startswith("Test a rule")]


def test_a_held_out_rule_capture_is_judged_by_nobody_and_emitted_nowhere(tmp_path: Path) -> None:
    out, w, emitted = _stage(tmp_path, share=100)
    held = w.saved["held"]
    assert {s.behaviour_id for s in held.specs} == {
        "probe::stable::show_more::0", "probe::stable::take_more::0"}
    assert all(is_held_out(s, 100) for s in held.specs)
    assert not emitted and len(out.specs) == 2
    assert out.features[1].rules[0].sentence == WRONG        # not judged: nothing was left to judge on
    assert not [p for p in w.prompts if p.startswith("Predict what")]
    assert "2 held out" in next(ln for ln in w.logs if "rule-probes:" in ln)
