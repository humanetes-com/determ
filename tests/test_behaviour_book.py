"""Pins for the behaviour book (Feature -> Rule -> Example) on the recorded
anyhow test files in tests/fixtures/behaviours/, each claim with its negative
control.

The claims are the plan's, as its addendum (2026-09-29) corrected them:

- an example's setup is the def-use slice its `when` needs, never the whole
  test item, and a test-file helper is stored once per suite (control: the
  pre-change harvester stored the item per assertion);
- grouping puts every example under exactly one feature, the `ensure!` cases
  whose `when` is the bare binding `test` included (control: the plan's
  "public item the when calls last" puts them under nothing);
- a rule is kept only when the checker needs it (control: the plan's check,
  "predicts every cited Then with the rule", keeps a restatement);
- a rule reaches the unit that declares its feature and not the units that
  merely executed its examples (control: aiming by coverage `exercises`);
- every fill site takes the book's instruction through one function (control:
  a call site that builds the identity half alone).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from pipeline_core.application.behaviour_book_build import BookBudget, build_behaviour_book, feature_files
from pipeline_core.application.extract_behaviours import harvest_behaviour_suite
from pipeline_core.domain.behaviour import (
    BehaviourSpec,
    BehaviourSubKind,
    BehaviourSuite,
    Feature,
    GlossaryEntry,
    ObservableOutcome,
    Rule,
    example_code,
    merge_suites,
    outcome_matches,
)
from pipeline_core.domain.behaviour_book import (
    CRATE_FEATURE,
    AimedUnit,
    RuleDraft,
    draft_refusal,
    feature_spellings,
    glossary_entries,
    group_features,
    judge_draft,
    parse_predictions,
    parse_rule_drafts,
    private_names,
    render_feature,
    rules_for_units,
)
from pipeline_core.domain.example_setup import setup_slice, top_level_items
from pipeline_core.domain.models import FnRecord, TypeRecord
from pipeline_core.prompts.fix_prompts import BlockFillPlan, _plan_line
from pipeline_core.prompts.layout_authoring import BEHAVIOUR_MUST_MAX_CHARS, behaviour_must_instruction

FIXTURES = Path(__file__).parent / "fixtures" / "behaviours"
REPO = Path(__file__).resolve().parents[1]

# tests/common/mod.rs of the same crate, which test_downcast.rs declares with
# `mod common;` — the helpers its assertions call.
COMMON = '''use anyhow::{bail, Result};
use std::io;

pub fn bail_literal() -> Result<()> {
    bail!("oh no!");
}

pub fn bail_fmt() -> Result<()> {
    bail!("{} {}!", "oh", "no");
}

pub fn bail_error() -> Result<()> {
    Err(io::Error::new(io::ErrorKind::Other, "oh no!"))?;
    unreachable!();
}
'''

# The vocabulary the recorded test files use, as glossary entries (what
# `glossary_entries` produces from the crate's records; built here so the
# grouping pins do not depend on an extractor binary).
GLOSSARY = (
    GlossaryEntry("Error", "struct", "The Error type."),
    GlossaryEntry("Error::msg", "fn", "Create a new error object from a printable error message."),
    GlossaryEntry("Error::new", "fn", "Create a new error object from any error type."),
    GlossaryEntry("Error::downcast", "fn", ""),
    GlossaryEntry("Error::downcast_ref", "fn", ""),
    GlossaryEntry("Error::downcast_mut", "fn", ""),
    GlossaryEntry("Error::is", "fn", ""),
    GlossaryEntry("Error::chain", "fn", ""),
    GlossaryEntry("Error::context", "fn", ""),
    GlossaryEntry("Chain::new", "fn", ""),
    GlossaryEntry("anyhow!", "macro", ""),
    GlossaryEntry("bail!", "macro", "Return early with an error."),
    GlossaryEntry("ensure!", "macro", ""),
)


def _tests() -> list[tuple[str, str]]:
    out = [(f"tests/{n}", (FIXTURES / n).read_text(encoding="utf-8"))
           for n in ("test_fmt.rs", "test_ensure.rs", "test_downcast.rs")]
    out.append(("tests/common/mod.rs", COMMON))
    return out


def _suite() -> BehaviourSuite:
    return harvest_behaviour_suite([], cfg_cluster="stable", test_sources=_tests(),
                                   default_cluster="stable")


def _spec(bid: str, setup: str, when: str, then: ObservableOutcome, **kw: object) -> BehaviourSpec:
    return BehaviourSpec(bid, setup, when, then, subkind=BehaviourSubKind.OUTCOME, **kw)  # type: ignore[arg-type]


# --- Stage 1: the example model ------------------------------------------------

def test_the_store_holds_setups_and_helpers_once_not_a_test_item_per_assertion() -> None:
    suite = _suite()
    tests = dict(_tests())
    asserts = [s for s in suite.specs if s.behaviour_id.startswith("testassert::")]
    # NEGATIVE CONTROL: the pre-change store — every assertion's `given` was
    # its whole test item.
    from pipeline_core.domain.refill_evidence import test_item_source
    old = sum(len(test_item_source(tests[s.origin.file], s.origin.name))  # type: ignore[union-attr]
              for s in asserts)
    new = sum(len(s.setup) for s in asserts) + sum(len(t) for _, t in suite.helpers)
    assert new * 5 < old, (new, old)
    assert all("#[test]" not in s.setup for s in asserts)
    # a helper called by 18 examples is one table row
    d = suite.to_dict()
    assert list(d["helpers"]).count("tests/common/mod.rs::bail_literal") == 1
    users = [s for s in asserts if "tests/common/mod.rs::bail_literal" in s.helper_keys]
    assert len(users) > 3
    assert all('bail!("oh no!")' in example_code(s) for s in users)


def test_the_store_round_trips_and_a_legacy_store_is_read_but_never_merged() -> None:
    suite = _suite()
    back = BehaviourSuite.from_dict(json.loads(json.dumps(suite.to_dict())))
    assert back == suite
    assert [s.helpers for s in back.specs] == [s.helpers for s in suite.specs]
    legacy = json.loads((FIXTURES / "behaviours_stable_2026-08-27.json").read_text())
    old = BehaviourSuite.from_dict(legacy)
    assert old.is_legacy
    first = next(s for s in old.specs if s.behaviour_id.startswith("testassert::"))
    assert first.setup.startswith("#[test]")          # read as it was written
    assert first.origin is not None and first.origin.name
    # The merge re-harvests every legacy row it can (existing-wins would
    # keep the old whole-item setups) and keeps only the probe rows, whose
    # Thens the original printed before strip and no later harvest can
    # re-measure. Run `273278-22529e44` (2026-09-29) lost four of them to
    # the whole-store discard (`231 already on disk -> 227`).
    merged = merge_suites(old, suite)
    by_id = {s.behaviour_id: s for s in merged.specs}
    assert all(by_id[s.behaviour_id] == s for s in suite.specs)
    assert all("#[test]" not in s.setup for s in merged.specs
               if s.behaviour_id.startswith("testassert::"))
    old_probes = {s.behaviour_id for s in old.specs
                  if s.behaviour_id.startswith("probe::")}
    assert old_probes and old_probes <= set(by_id)
    extra = set(by_id) - {s.behaviour_id for s in suite.specs}
    assert extra == old_probes - {s.behaviour_id for s in suite.specs}
    # Negative control: a legacy store with no probe rows is the plain
    # discard it always was.
    no_probes = BehaviourSuite(cfg_cluster=old.cfg_cluster, format_version=1,
                               specs=tuple(s for s in old.specs
                                           if s.behaviour_id not in old_probes))
    assert merge_suites(no_probes, suite) == suite


def test_setup_follows_the_bindings_the_when_reads() -> None:
    item = """#[test]
fn t() {
    let a = make();
    let b = a.clone();
    let mut it = b.iter();
    assert_eq!("1", it.next().unwrap().to_string());
    let a = other();
    assert_eq!("2", it.next().unwrap().to_string());
}"""
    pos = item.index('assert_eq!("2"')
    got = setup_slice(item, pos)
    # shadowed `a` (the second binding) is not read by the second case; the
    # first assertion advanced the `mut` iterator the second reads
    assert got == ('let a = make();\nlet b = a.clone();\nlet mut it = b.iter();\n'
                   'assert_eq!("1", it.next().unwrap().to_string());')
    assert "other()" not in got


def test_a_blanket_impl_over_its_own_parameter_is_not_a_helper_of_that_name() -> None:
    items = top_level_items("struct S;\nimpl<T> Trait for T {}\nimpl Add for S {}\n")
    assert [(i.name, i.is_impl) for i in items] == [("S", False), ("S", True)]


# --- Stage 2: grouping ------------------------------------------------------------

def _last_called(when: str, glossary: tuple[GlossaryEntry, ...]) -> str | None:
    """The plan's grouping rule, for the negative control: the public item
    `when` calls last at top level."""
    last = None
    for m in re.finditer(r"([A-Za-z_][A-Za-z0-9_]*)\s*(!?)\s*[(\[{]", when):
        word = m.group(1) + m.group(2)
        if any(g.name == word or g.leaf == word.rstrip("!") for g in glossary):
            last = word
    return last


def test_every_example_lands_under_one_feature_including_the_bare_binding_cases() -> None:
    suite = _suite()
    features = group_features(suite.specs, GLOSSARY, crate="anyhow")
    placed = [i for f in features for i in f.example_ids]
    assert sorted(placed) == sorted(s.behaviour_id for s in suite.specs)
    bare = [s for s in suite.specs if s.when == "test" and s.via]
    assert len(bare) > 50
    ensure = next(f for f in features if f.item == "ensure!")
    assert {s.behaviour_id for s in bare} <= set(ensure.example_ids)
    # NEGATIVE CONTROL: "last public call in `when`" groups none of them.
    assert all(_last_called(s.when, GLOSSARY) is None for s in bare)


def test_a_foreign_path_does_not_name_the_crates_item() -> None:
    specs = (_spec("a", "", "io::Error::new(k, \"x\").to_string()", ObservableOutcome.display("x")),
             _spec("b", "", "Error::new(e).to_string()", ObservableOutcome.display("x")),
             _spec("c", "", "anyhow::Error::new(e).to_string()", ObservableOutcome.display("x")))
    by = {i: f.item for f in group_features(specs, GLOSSARY, crate="anyhow") for i in f.example_ids}
    assert by == {"a": CRATE_FEATURE, "b": "Error::new", "c": "Error::new"}


def test_glossary_spells_public_names_as_a_user_does_and_drops_the_std_vocabulary() -> None:
    src = ('/// The error.\npub struct Error;\n'
           '/// Makes one. Second sentence.\n'
           'impl Error { pub fn msg() {} fn hidden() {} }\n'
           'impl Display for Error { fn fmt() {} }\n'
           '/// Adds context.\npub trait Context { fn context(self); }\n'
           '#[macro_export]\n/// Bails.\nmacro_rules! bail { () => {} }\n'
           '#[doc(hidden)]\n#[macro_export]\nmacro_rules! __private { () => {} }\n')
    enc = src.encode()
    fns = [FnRecord(file="lib.rs", fn_name="msg", container="impl Error", is_pub=True,
                    sig_byte_start=enc.index(b"pub fn msg")),
           FnRecord(file="lib.rs", fn_name="hidden", container="impl Error"),
           FnRecord(file="lib.rs", fn_name="fmt", container="impl Display for Error"),
           FnRecord(file="lib.rs", fn_name="context", container="impl Context for Result"),
           FnRecord(file="lib.rs", fn_name="construct", container="impl Error")]
    types = [TypeRecord(file="lib.rs", kind="struct", name="Error", is_pub=True),
             TypeRecord(file="lib.rs", kind="trait", name="Context", is_pub=True),
             TypeRecord(file="lib.rs", kind="struct", name="ErrorImpl")]
    g = glossary_entries(fns, types, [("lib.rs", src)])
    assert [(e.name, e.kind) for e in g] == [
        ("Context", "trait"), ("Context::context", "fn"), ("Error", "struct"),
        ("Error::msg", "fn"), ("bail!", "macro")]
    assert next(e for e in g if e.name == "Error::msg").summary == "Makes one."
    specs = (_spec("x", "", "e.fmt()", ObservableOutcome.display("x")),)
    priv = private_names(fns, types, g, specs)
    assert {"construct", "ErrorImpl", "hidden"} <= priv and "fmt" not in priv


# --- Stage 3: the check -------------------------------------------------------------

def _pool() -> dict[str, BehaviourSpec]:
    msg = "Condition failed: `v + v == 1` (2 vs 1)"
    return {
        "e1": _spec("e1", "let test = || Ok(ensure!(v + v == 1));", "test",
                    ObservableOutcome.display(msg), via="assert_err"),
        "e2": _spec("e2", "", "Error::msg(\"x\").is::<&str>()",
                    ObservableOutcome.type_identity("&str", True)),
    }


def test_a_rule_the_checker_can_ignore_is_dropped_and_one_it_needs_is_kept() -> None:
    pool = _pool()
    msg = pool["e1"].then.value  # type: ignore[union-attr]
    informative = RuleDraft("`ensure!` on a false comparison reports the condition as written and both sides.",
                            ("e1",))
    vacuous = RuleDraft("`Error::msg` builds an error.", ("e2",))
    # informative: without the rule the checker got the text wrong
    kept = judge_draft(informative, pool, with_rule={"e1": msg},
                       without_rule={"e1": "Condition failed: v + v == 1"})
    assert kept.kept
    # vacuous: the checker predicts the identity from the when alone
    dropped = judge_draft(vacuous, pool, with_rule={"e2": "true"}, without_rule={"e2": "true"})
    assert not dropped.kept and dropped.reason.startswith("adds nothing")

    # NEGATIVE CONTROL: the plan's check ("predicts with the rule") keeps it.
    def plan_check(d: RuleDraft, with_rule: dict[str, str]) -> bool:
        return all(outcome_matches(with_rule.get(i, ""), pool[i].then) for i in d.covers)  # type: ignore[arg-type]
    assert plan_check(vacuous, {"e2": "true"})

    # An ablation that did not answer is not a miss. The with-rule checker
    # matched, and the old predicate counted the missing id as "predicted
    # wrong", which keeps the draft.
    unanswered = judge_draft(informative, pool, with_rule={"e1": msg}, without_rule={})
    assert not unanswered.kept and unanswered.reason.startswith("ablation did not answer")
    partial = judge_draft(
        RuleDraft(informative.sentence, ("e1", "e2")), pool,
        with_rule={"e1": msg, "e2": "true"}, without_rule={"e2": "true"})
    assert not partial.kept and partial.reason.startswith("ablation did not answer")

    def absence_counts_as_a_miss(d: RuleDraft, with_rule: dict[str, str],
                                 without_rule: dict[str, str]) -> bool:
        cited = [pool[i] for i in d.covers]
        if any(s.then is None or not outcome_matches(with_rule.get(s.behaviour_id, "\0"), s.then)
               for s in cited):
            return False
        return any(s.then is not None
                   and not outcome_matches(without_rule.get(s.behaviour_id, "\0"), s.then)
                   for s in cited)

    assert absence_counts_as_a_miss(
        RuleDraft(informative.sentence, ("e1", "e2")),
        {"e1": msg, "e2": "true"}, {"e2": "true"})


def test_a_rule_naming_a_private_item_or_quoting_a_then_is_refused_before_the_check() -> None:
    pool = _pool()
    msg = pool["e1"].then.value  # type: ignore[union-attr]
    assert draft_refusal(RuleDraft("It calls `construct` first.", ("e1",)), pool, {"construct"}) \
        == "names the private item `construct`"
    assert draft_refusal(RuleDraft(f"It says {msg}.", ("e1",)), pool, set()).startswith("quotes e1")
    assert draft_refusal(RuleDraft("Constructs a value.", ("e1",)), pool, {"construct"}) is None
    assert draft_refusal(RuleDraft("x", ("nope",)), pool, set()) == "cites no checkable example of its feature"


def test_the_prefix_rule_is_the_replays() -> None:
    then = ObservableOutcome(ObservableOutcome.debug("x").kind, value="Error {", prefix=True)
    assert outcome_matches("Error {\n  frames", then)
    assert not outcome_matches("Error {", then)        # the replay asserts strictly longer
    assert outcome_matches("true", ObservableOutcome.type_identity("&str", True))
    assert not outcome_matches("true", ObservableOutcome.type_identity("&str", False))


def test_parsers_read_the_reply_shapes() -> None:
    assert parse_rule_drafts('<think>x</think><RULE covers="a, b">One\n  sentence.</RULE>') == [
        RuleDraft("One sentence.", ("a", "b"))]
    assert parse_predictions('<THEN id="a">\nline1\nline2\n</THEN><THEN id="b">x</THEN>') == {
        "a": "line1\nline2", "b": "x"}


def test_t0d_book_build_keeps_the_needed_rule_counts_the_rest_and_writes_the_view() -> None:
    suite = _suite()
    ensure_ids = [s.behaviour_id for s in suite.specs if s.via == "assert_err"][:3]
    pool = {s.behaviour_id: s for s in suite.specs}
    good = "`ensure!` on a false comparison reports the condition as written and both operand values."
    calls: list[str] = []

    def ask(prompt: str) -> str:
        calls.append(prompt)
        if "TASK: state the observable RULES" in prompt:
            if f'<EXAMPLE id="{ensure_ids[0]}">' in prompt:
                return (f'<RULE covers="{",".join(ensure_ids)}">{good}</RULE>'
                        f'<RULE covers="{ensure_ids[0]}">It is a macro.</RULE>'
                        f'<RULE covers="{ensure_ids[0]}">It calls `construct`.</RULE>')
            return ""
        ids = re.findall(r'<EXAMPLE id="([^"]+)">', prompt)
        right = "RULE (holds" in prompt and good in prompt
        return "".join(
            f'<THEN id="{i}">{pool[i].then.value if right else "wrong"}</THEN>'  # type: ignore[union-attr]
            for i in ids)

    book, reading = build_behaviour_book(
        suite, crate="anyhow", glossary=GLOSSARY, private={"construct"}, ask=ask,
        budget=BookBudget(max_features=1))
    ensure = next(f for f in book.features if f.item == "ensure!")
    assert [r.sentence for r in ensure.rules] == [good]
    assert reading.kept == 1 and reading.features_asked == 1
    assert reading.dropped == {"does not predict": 1, "names the private item": 1}
    assert "behaviour-book:" in reading.line() and "1 kept" in reading.line().replace("(", "")
    # the rule rides on the examples it cites, for every reader of the specs
    assert all(pool_s.rules and pool_s.rules[0].sentence == good
               for pool_s in book.specs if pool_s.behaviour_id in ensure_ids)
    # it survives the store
    back = BehaviourSuite.from_dict(json.loads(json.dumps(book.to_dict())))
    assert [r.sentence for r in back.rules] == [good]
    # the Gherkin view is generated from the book
    files = feature_files(book)
    text = files["ensure_macro.feature"]
    assert text.startswith("# Generated from the behaviour book")
    assert f"Rule: {good}" in text and "Rule: (no checked rule states these yet)" in text
    # rules off: the book is built, no model call
    calls.clear()
    plain, r2 = build_behaviour_book(suite, crate="anyhow", glossary=GLOSSARY, private=set(),
                                     ask=ask, budget=BookBudget(rules=False))
    assert not calls and plain.features and r2.kept == 0


# --- Stage 5: aiming and the one instruction path -----------------------------------

def _ruled(feature: str, sentence: str, *, terms: tuple[str, ...] = (),
           exercises: frozenset[str] = frozenset()) -> BehaviourSuite:
    spec = _spec("x1", "", "Error::msg(\"a\").to_string()", ObservableOutcome.display("a"),
                 exercises=exercises)
    rule = Rule("rule::" + feature, sentence, ("x1",), feature, terms)
    return BehaviourSuite("stable", (spec,), features=(Feature(feature, "fn", ("x1",), (rule,)),))


def test_a_rule_reaches_the_unit_that_declares_its_feature_not_those_that_ran() -> None:
    flood = frozenset({"src/error.rs::object_drop", "src/error.rs::msg", "src/ptr.rs::new"})
    suite = _ruled("msg", "`Error::msg` displays as its message.", terms=("Error::msg",),
                   exercises=flood)
    specs = suite.specs
    assert rules_for_units(specs, [AimedUnit("msg", "impl Error")])
    assert not rules_for_units(specs, [AimedUnit("object_drop", "impl ErrorImpl")])
    assert not rules_for_units(specs, [AimedUnit("new", "impl < T > Own < T >")])

    # NEGATIVE CONTROL: aiming by coverage hands it to every unit that ran.
    def by_coverage(unit: str) -> bool:
        return any(e.rsplit("::", 1)[-1] == unit for s in specs for e in s.exercises)
    assert by_coverage("object_drop") and by_coverage("new")


def test_a_qualified_call_is_its_own_feature_and_a_shared_leaf_is_not_every_spelling() -> None:
    """Plan sentence: `Error::new` and `Chain::new` are different features,
    and a rule kept for one does not aim at the other. A method call whose
    leaf belongs to one fn still aims at that fn. Negative control: every
    glossary fn whose leaf is `new`."""
    glossary = (
        GlossaryEntry("Error::new", "fn"),
        GlossaryEntry("Chain::new", "fn"),
        GlossaryEntry("Error::msg", "fn"),
    )
    specs = (
        _spec("q", "", "Error::new(e)", ObservableOutcome.display("x")),
        _spec("m", "", "e.new()", ObservableOutcome.display("x")),
        _spec("u", "", "e.msg()", ObservableOutcome.display("x")),
    )
    by = {i: f.item for f in group_features(specs, glossary) for i in f.example_ids}
    assert by == {"q": "Error::new", "m": "new", "u": "msg"}
    assert feature_spellings("Error::new", glossary) == ("Error::new",)
    assert feature_spellings("new", glossary) == ()
    assert feature_spellings("msg", glossary) == ("Error::msg",)
    assert {g.name for g in glossary if g.leaf == "new"} == {"Error::new", "Chain::new"}

    sentence = "`Error::new` wraps its cause."

    def ask(prompt: str) -> str:
        if "TASK: state the observable RULES" in prompt:
            return f'<RULE covers="q">{sentence}</RULE>'
        if "RULE (holds" in prompt:
            return '<THEN id="q">x</THEN>'
        return '<THEN id="q">no</THEN>'

    book, reading = build_behaviour_book(
        BehaviourSuite("c", specs[:1]), crate="krate", glossary=glossary,
        private=set(), ask=ask, budget=BookBudget(max_features=4))
    assert reading.kept == 1
    rule = book.rules[0]
    assert "Error::new" in rule.terms and "Chain::new" not in rule.terms
    assert rules_for_units(book.specs, [AimedUnit("new", "impl Error")])
    assert not rules_for_units(book.specs, [AimedUnit("new", "impl Chain")])
    sprayed = _ruled("new", sentence, terms=("Error::new", "Chain::new"))
    assert rules_for_units(sprayed.specs, [AimedUnit("new", "impl Chain")])


def test_a_qualified_term_does_not_reach_a_same_named_unit_of_another_type() -> None:
    suite = _ruled("new", "`Chain::new` yields the error then its sources.", terms=("Chain::new",))
    assert rules_for_units(suite.specs, [AimedUnit("new", "impl < 'a > Chain < 'a >")])
    assert not rules_for_units(suite.specs, [AimedUnit("new", "impl Error")])


def test_the_instruction_carries_the_rules_is_bounded_and_fits_the_plan_line() -> None:
    suite = _ruled("msg", "`Error::msg` displays as its message.", terms=("Error::msg",))
    from pipeline_core.domain.behaviour_select import IdentityMustUnit
    text = behaviour_must_instruction(suite.specs, units=(IdentityMustUnit("msg", "impl Error", "fn msg<M>(m: M) -> Self"),))
    assert "RULES" in text and "`Error::msg` displays as its message." in text
    long = _ruled("msg", "`Error::msg` " + "x" * 3990, terms=("Error::msg",))
    assert len(behaviour_must_instruction(long.specs, units=(IdentityMustUnit("msg", "impl Error", ""),))) \
        <= BEHAVIOUR_MUST_MAX_CHARS
    line = _plan_line(BlockFillPlan(header="impl Error", stubs=("msg",), behaviour_must=text))
    assert "\n      - `Error::msg`" in line


def test_the_slice_session_turn_carries_the_instruction() -> None:
    from pipeline_core.domain.slice_session import SessionSlice
    from pipeline_core.prompts.slice_session import build_slice_session_prompt
    kw: dict[str, object] = {
        "module_name": "m", "cluster_name": "c", "slice_": SessionSlice(tests=(), units=()),
        "records": {}, "all_records": [], "file_texts": {}, "target_evidence": {},
        "test_blocks": [], "types_section": ""}
    assert "<BEHAVIOUR_RULES>" in build_slice_session_prompt(**kw, behaviour_must="RULES: x")  # type: ignore[arg-type]
    assert "<BEHAVIOUR_RULES>" not in build_slice_session_prompt(**kw)  # type: ignore[arg-type]


_HALF_ONLY = re.compile(r"\bidentity_must_instruction\s*\(")


def test_no_fill_site_builds_the_identity_half_alone() -> None:
    """Every fill site goes through `behaviour_must_instruction`; one that
    called the identity half directly would carry no rule."""
    offenders = []
    for path in sorted((REPO / "pipeline_core" / "application").rglob("*.py")):
        if _HALF_ONLY.search(path.read_text(encoding="utf-8")):
            offenders.append(str(path.relative_to(REPO)))
    assert offenders == []
    # NEGATIVE CONTROL: the pre-change per-fn call site's text.
    assert _HALF_ONLY.search("behaviour_must=identity_must_instruction(\n    ctx.behaviour_specs,")


def test_render_feature_lists_uncovered_examples_under_a_rule_less_heading() -> None:
    suite = _suite()
    f = group_features(suite.specs, GLOSSARY, crate="anyhow")[0]
    text = render_feature(f, GLOSSARY, {s.behaviour_id: s for s in suite.specs})
    assert text.count("Example:") == len(f.example_ids)
    assert "Rule: (no checked rule states these yet)" in text


# --- branch pins on invented shapes -------------------------------------------------

def test_origins_key_and_parse_every_id_form() -> None:
    from pipeline_core.domain.behaviour import ExampleOrigin, OriginKind
    cases = {
        "testassert::tests/a.rs::t::3": (OriginKind.TEST, "tests/a.rs::t"),
        "doctest::lib.rs::40": (OriginKind.DOCTEST, "lib.rs:40"),
        "static::tests/r.rs::1": (OriginKind.STATIC_PIN, "tests/r.rs"),
        "static::lib.rs::12::0": (OriginKind.STATIC_PIN, "lib.rs:12"),
        "probe::stable::unit::0": (OriginKind.PROBE, "probe::unit"),
    }
    for bid, (kind, key) in cases.items():
        o = ExampleOrigin.from_behaviour_id(bid)
        assert o is not None and o.kind is kind and o.key == key
        assert ExampleOrigin.from_dict(o.to_dict()) == o
    assert ExampleOrigin.from_behaviour_id("testassert::a::b::x") is None
    assert ExampleOrigin.from_behaviour_id("other::x") is None


def test_then_comparison_and_text_per_kind() -> None:
    from pipeline_core.domain.behaviour import StaticAssertKind, then_value_text
    down = ObservableOutcome.downcast_ok(False)
    assert outcome_matches("None", down) and not outcome_matches("maybe", down)
    chain = ObservableOutcome.chain_seq(("a", "b"))
    assert outcome_matches("a\nb\n", chain) and then_value_text(chain) == "a\nb"
    pin = ObservableOutcome.static_assertion(StaticAssertKind.SIZE, "size_of::<E>() == 8")
    assert not outcome_matches("true", pin) and then_value_text(pin) == "size_of::<E>() == 8"
    assert then_value_text(down) == "false"
    assert outcome_matches("x\n", ObservableOutcome.display("x"))


def test_glossary_reads_trait_declarations_and_finds_a_file_by_suffix() -> None:
    src = "/// T.\npub trait Ext {\n    /// Adds.\n    fn add(self);\n}\n"
    fns = [FnRecord(file="src/lib.rs", fn_name="add", container="trait Ext",
                    sig_byte_start=src.index("fn add"))]
    types = [TypeRecord(file="src/lib.rs", kind="trait", name="Ext", is_pub=True)]
    g = glossary_entries(fns, types, [("lib.rs", src)])
    assert [(e.name, e.summary) for e in g] == [("Ext", "T."), ("Ext::add", "Adds.")]
    none = glossary_entries([FnRecord(file="x.rs", fn_name="f", container="trait Hidden")], [], [])
    assert none == ()


def test_aiming_reads_trait_containers_and_refuses_what_it_cannot_name() -> None:
    suite = _ruled("add", "`Ext::add` appends.", terms=("Ext::add", "ensure!"))
    specs = suite.specs
    assert rules_for_units(specs, [AimedUnit("add", "trait Ext")])
    assert rules_for_units(specs, [AimedUnit("add", "impl Ext for Vec < u8 >")])
    assert not rules_for_units(specs, [])
    assert not rules_for_units(specs, [AimedUnit("add", "impl Ext for Vec < u8 >")], cap=0)
    free = _ruled("helper", "`helper` returns one.", terms=("helper",))
    assert rules_for_units(free.specs, [AimedUnit("helper")])
    assert not rules_for_units(free.specs, [AimedUnit("helper", "impl Other")])
    ident = _ruled("zzz", "Unrelated.")
    assert rules_for_units(ident.specs, [AimedUnit("construct", "impl Error")], identity_ids={"x1"})


def test_gherkin_steps_for_every_kind_and_a_missing_example() -> None:
    from pipeline_core.domain.behaviour import StaticAssertKind
    thens = [ObservableOutcome.downcast_ok(True),
             ObservableOutcome.static_assertion(StaticAssertKind.SIZE, "size_of::<E>() == 8"),
             ObservableOutcome.chain_seq(("a", "b")),
             ObservableOutcome.type_identity("&str", False),
             ObservableOutcome.alt_debug("E {\n}"),
             ObservableOutcome(ObservableOutcome.debug("x").kind, value="E", prefix=True)]
    specs = {f"k{i}": _spec(f"k{i}", "let e = 1;", "e", t) for i, t in enumerate(thens)}
    doc = BehaviourSpec("d", "", "assert!(true);")
    specs["d"] = doc
    f = Feature("e", "fn", (*specs, "missing"))
    text = render_feature(f, (GlossaryEntry("e", "fn", "An e."),), specs)
    for needle in ("finds a value", "holds at compile time", "chain yields", "is not a `&str`",
                   "alternate Debug render is:", "text starting with", "own assertions hold",
                   "e — An e."):
        assert needle in text, needle
    assert "missing" not in text


def test_small_contracts() -> None:
    from pipeline_core.domain.behaviour_book import BookReading, drop_reason_class, feature_file_name
    assert drop_reason_class("something new") == "something new"
    assert feature_file_name(Feature("(crate)", "crate", ())) == "crate.feature"
    pool = _pool()
    assert draft_refusal(RuleDraft("x" * 500, ("e1",)), pool, set()) == "longer than one sentence"
    miss = judge_draft(RuleDraft("s", ("e1",)), pool, with_rule={}, without_rule={})
    assert not miss.kept and miss.reason.startswith("does not predict")
    assert BookReading().line().endswith("dropped: none")
