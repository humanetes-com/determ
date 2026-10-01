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
- a chunk's drafts are judged as one set and each is kept for the examples
  it is necessary for: a base and a suffix sentence that predict only
  together are both kept (control: each checked alone keeps neither), two
  drafts saying the same thing keep one, a set miss is recorded on the rule
  and not a veto, and a set that gains nothing spends no leave-one-out call
  (run `72627-751534fe`, 2026-09-30: `ensure!` held 103 of 197 checkable
  examples and kept 0 rules in both books);
- a rule reaches the unit that declares its feature and not the units that
  merely executed its examples (control: aiming by coverage `exercises`);
- every fill site takes the book's instruction through one function (control:
  a call site that builds the identity half alone).
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from dataclasses import replace
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
    ChunkJudgment,
    CreditedRule,
    RuleDraft,
    SiblingVisit,
    draft_refusal,
    feature_spellings,
    glossary_entries,
    group_features,
    judge_rule_set,
    judge_visit,
    parse_predictions,
    parse_rule_drafts,
    private_names,
    render_feature,
    rules_for_units,
    sentence_breaks,
    sibling_visits,
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


class _Checker:
    """A fake predict collaborator: `answer(rules, id)` gives the predicted
    Then for one example under one rule set (None = the reply omits it);
    every call is recorded as (rules, ids)."""

    def __init__(self, answer: Callable[[frozenset[str], str], str | None]) -> None:
        self.answer = answer
        self.calls: list[tuple[tuple[str, ...], tuple[str, ...]]] = []

    def __call__(self, rules: Sequence[str], ids: Sequence[str]) -> dict[str, str]:
        self.calls.append((tuple(rules), tuple(ids)))
        out: dict[str, str] = {}
        for i in ids:
            v = self.answer(frozenset(rules), i)
            if v is not None:
                out[i] = v
        return out


def test_a_rule_the_checker_can_ignore_is_dropped_and_one_it_needs_is_kept() -> None:
    pool = _pool()
    msg = pool["e1"].then.value  # type: ignore[union-attr]
    informative = RuleDraft("`ensure!` on a false comparison reports the condition as written and both sides.",
                            ("e1",))
    vacuous = RuleDraft("`Error::msg` builds an error.", ("e2",))

    def answer(rules: frozenset[str], i: str) -> str:
        if i == "e2":
            return "true"                     # the when alone decides the identity
        return msg if informative.sentence in rules else "Condition failed: v + v == 1"

    verdict = judge_rule_set([informative, vacuous], pool, predict=_Checker(answer))
    assert [c.draft for c in verdict.kept] == [informative]
    assert verdict.kept[0].credited == ("e1",)
    assert [r for r in verdict.dropped if r.startswith("adds nothing")] == [verdict.dropped[0]]

    # NEGATIVE CONTROL: the plan's check ("predicts with the rule") keeps it.
    def plan_check(d: RuleDraft, with_rule: dict[str, str]) -> bool:
        return all(outcome_matches(with_rule.get(i, ""), pool[i].then) for i in d.covers)  # type: ignore[arg-type]
    assert plan_check(vacuous, {"e2": "true"})

    # An ablation that did not answer is not a miss: the set matched, and
    # reading the missing id as "predicted wrong" keeps the draft.
    silent_ablation = _Checker(lambda rules, i: msg if rules else None)
    unanswered = judge_rule_set([informative], pool, predict=silent_ablation)
    assert not unanswered.kept and unanswered.dropped[0].startswith("ablation did not answer")

    def absence_counts_as_a_miss(with_set: dict[str, str], ablation: dict[str, str]) -> bool:
        return any(outcome_matches(with_set.get(i, "\0"), pool[i].then)  # type: ignore[arg-type]
                   and not outcome_matches(ablation.get(i, "\0"), pool[i].then)  # type: ignore[arg-type]
                   for i in informative.covers)

    assert absence_counts_as_a_miss({"e1": msg}, {})


def _ensure_pool() -> dict[str, BehaviourSpec]:
    """Invented `ensure!`-shaped examples: two binary comparisons (message
    carries ` (<left> vs <right>)`) and one plain condition."""
    out = {}
    for bid, cond, suffix in (("b1", "a == b", " (1 vs 2)"), ("b2", "x < y", " (3 vs 0)"),
                              ("p1", "flag", "")):
        out[bid] = _spec(bid, f"let test = || Ok(ensure!({cond}));", "test",
                         ObservableOutcome.display(f"Condition failed: `{cond}`{suffix}"),
                         via="assert_err")
    return out


BASE = RuleDraft("The message is `Condition failed: ` then the condition in backticks.",
                 ("b1", "b2", "p1"))
SUFFIX = RuleDraft("For a binary comparison the base message above is followed by both "
                   "operand values in parentheses, separated by `vs`.", ("b1", "b2"))


def _base_suffix_answer(pool: dict[str, BehaviourSpec]) -> Callable[[frozenset[str], str], str]:
    def answer(rules: frozenset[str], i: str) -> str:
        full = pool[i].then.value  # type: ignore[union-attr]
        base = full.split(" (", 1)[0]
        if BASE.sentence in rules and SUFFIX.sentence in rules:
            return full
        if BASE.sentence in rules:
            return base
        return "assertion failed: " + base.split("`")[1]
    return answer


def test_a_base_and_a_suffix_rule_that_predict_only_together_are_both_kept() -> None:
    pool = _ensure_pool()
    checker = _Checker(_base_suffix_answer(pool))
    verdict = judge_rule_set([BASE, SUFFIX], pool, predict=checker)
    kept = {c.draft.sentence: c.credited for c in verdict.kept}
    assert kept == {BASE.sentence: ("b1", "b2", "p1"), SUFFIX.sentence: ("b1", "b2")}
    assert not verdict.dropped and all(not c.refuted_on for c in verdict.kept)
    # ablation + set + one leave-one-out per draft: <= |S| + 2
    assert verdict.calls == len(checker.calls) == 4

    # NEGATIVE CONTROL: each draft checked alone (the per-draft judge) keeps
    # neither — the base alone misses both comparisons, the suffix alone
    # predicts nothing.
    alone = [judge_rule_set([d], pool, predict=_Checker(_base_suffix_answer(pool)))
             for d in (BASE, SUFFIX)]
    all_or_nothing_base = all(
        outcome_matches(_base_suffix_answer(pool)(frozenset({BASE.sentence}), i), pool[i].then)  # type: ignore[arg-type]
        for i in BASE.covers)
    assert not all_or_nothing_base
    assert not alone[1].kept
    assert [c.credited for c in alone[0].kept] == [("p1",)]  # credit, not a veto
    assert alone[0].kept[0].refuted_on == ("b1", "b2")


def test_two_drafts_saying_the_same_thing_keep_one_copy() -> None:
    pool = _ensure_pool()
    twin = RuleDraft(BASE.sentence.replace("The message", "Its message"), BASE.covers)
    verdict = judge_rule_set(
        [BASE, twin], pool,
        predict=_Checker(lambda rules, i: pool[i].then.value if rules else "no"))  # type: ignore[union-attr]
    assert [c.draft for c in verdict.kept] == [BASE]
    assert verdict.dropped == ("not necessary for its 3 gained example(s)",)

    # A leave-one-out that did not answer credits nobody: silence is not a
    # "matched without it" either.
    def silent_loo(rules: frozenset[str], i: str) -> str | None:
        if len(rules) == 1:
            return None
        return pool[i].then.value if rules else "no"  # type: ignore[union-attr]
    silent = judge_rule_set([BASE, twin], pool, predict=_Checker(silent_loo))
    assert not silent.kept
    assert all(r.startswith("leave-one-out did not answer") for r in silent.dropped)


def test_a_set_miss_is_recorded_on_the_rule_not_a_veto_and_projected_to_local_ids() -> None:
    pool = _ensure_pool()

    def slip(rules: frozenset[str], i: str) -> str:
        full = pool[i].then.value  # type: ignore[union-attr]
        if not rules:
            return "no"
        return full.replace("(", "") if i == "b2" else full   # the predictor drops a `(`
    verdict = judge_rule_set([BASE], pool, predict=_Checker(slip))
    assert [(c.credited, c.refuted_on) for c in verdict.kept] == [(("b1", "p1"), ("b2",))]
    rule = Rule("r", BASE.sentence, ("b1", "p1"), "ensure!", refuted_on=("b2",))
    assert Rule.from_dict(json.loads(json.dumps(rule.to_dict()))) == rule
    assert "refuted_on" not in replace(rule, refuted_on=()).to_dict()


def test_a_set_that_gains_nothing_spends_no_leave_one_out_call() -> None:
    pool = _ensure_pool()
    checker = _Checker(lambda rules, i: pool[i].then.value)  # type: ignore[union-attr]
    verdict = judge_rule_set([BASE, SUFFIX], pool, predict=checker)
    assert not verdict.kept and verdict.calls == len(checker.calls) == 2
    assert all(r.startswith("adds nothing") for r in verdict.dropped)
    refuting = judge_rule_set([BASE], pool, predict=_Checker(lambda rules, i: "no"))
    assert refuting.dropped == ("refuted on 3 of its 3 example(s)",) and refuting.calls == 2


def test_a_rule_naming_a_private_item_or_quoting_a_then_is_refused_before_the_check() -> None:
    pool = _pool()
    msg = pool["e1"].then.value  # type: ignore[union-attr]
    assert draft_refusal(RuleDraft("It calls `construct` first.", ("e1",)), pool, {"construct"}) \
        == "names the private item `construct`"
    assert draft_refusal(RuleDraft(f"It says {msg}.", ("e1",)), pool, set()).startswith("quotes e1")
    assert draft_refusal(RuleDraft("Constructs a value.", ("e1",)), pool, {"construct"}) is None
    assert draft_refusal(RuleDraft("x", ("nope",)), pool, set()) == "cites no checkable example of its feature"


def test_one_sentence_is_judged_whatever_its_length_and_two_are_refused() -> None:
    pool = _pool()
    # One sentence stating a whole format: code spans holding `.`, a macro
    # spelled `name!` before a capital, a dotted abbreviation before one.
    whole = ("When the condition of bail! Or `ensure!` is a binary comparison, e.g. Equality as "
             "in `a == b`, the message is `Condition failed: ` followed by the condition as "
             "written in backticks, a space, an opening parenthesis, the `Debug` rendering of "
             "the left operand (for `std::fmt::Arguments` that is `the text. As printed`), a "
             "space, `vs`, a space, the `Debug` rendering of the right operand, and a closing "
             "parenthesis, with nothing after the closing parenthesis and no trailing period "
             "or newline in any build.")
    two = RuleDraft("It builds an error. The message is the condition.", ("e1",))
    assert sentence_breaks(whole) == 0 and sentence_breaks(two.sentence) == 1
    assert draft_refusal(RuleDraft(whole, ("e1",)), pool, set()) is None
    assert draft_refusal(two, pool, set()) == "more than one sentence"
    assert draft_refusal(RuleDraft('It says "no." Then it stops? Yes.', ("e1",)), pool, set()) \
        == "more than one sentence"

    # NEGATIVE CONTROL: the 400-character bound this replaced refuses the one
    # sentence and admits the two.
    def length_bound(d: RuleDraft) -> bool:
        return len(d.sentence) > 400
    assert length_bound(RuleDraft(whole, ("e1",))) and not length_bound(two)
    from pipeline_core.domain.behaviour_book import drop_reason_class
    assert drop_reason_class("more than one sentence") == "more than one sentence"


WRONG = RuleDraft("The message is `Condition failed` then a space then the condition.", ("c1",))


def test_a_feature_s_kept_sentences_visit_the_chunk_that_never_judged_them() -> None:
    home = ChunkJudgment(("b1", "b2", "p1"), (BASE.sentence, SUFFIX.sentence), (
        CreditedRule(BASE, ("b1", "b2", "p1")), CreditedRule(SUFFIX, ("b1", "b2"))))
    flat = ChunkJudgment(("c1", "c2"), (WRONG.sentence,), ())
    assert sibling_visits([home, flat]) == [
        SiblingVisit(1, (BASE.sentence, SUFFIX.sentence), ("c1", "c2"))]
    # a chunk whose set already carried every kept sentence is not asked again,
    # whatever it left uncovered; nor is anything asked when nothing was kept
    partial = ChunkJudgment(("b1", "b2", "p1"), home.judged, (CreditedRule(BASE, ("p1",)),))
    assert sibling_visits([partial]) == []
    assert sibling_visits([flat, flat]) == []
    assert sibling_visits([home, ChunkJudgment(("c1",), (), ())])[0].example_ids == ("c1",)

    # the visit's judgment is the set judge's: both halves necessary, both credited
    pool = _ensure_pool()
    checker = _Checker(_base_suffix_answer(pool))
    credit, calls = judge_visit(SiblingVisit(0, (BASE.sentence, SUFFIX.sentence), ("b1", "p1")),
                                pool, predict=checker)
    assert credit == {BASE.sentence: ("b1", "p1"), SUFFIX.sentence: ("b1",)}
    assert calls == len(checker.calls) and all(set(ids) <= {"b1", "p1"} for _r, ids in checker.calls)


def _two_chunk_ensure_suite() -> tuple[BehaviourSuite, dict[str, BehaviourSpec]]:
    specs = []
    for n, (cond, suffix) in enumerate((("a == b", " (1 vs 2)"), ("x < y", " (3 vs 0)"), ("flag", ""),
                                        ("c != d", " (4 vs 4)"), ("m > n", " (0 vs 9)"), ("ok", ""))):
        specs.append(_spec(f"s{n}", f"let test = || Ok(ensure!({cond}));", "test",
                           ObservableOutcome.display(f"Condition failed: `{cond}`{suffix}"),
                           via="assert_err"))
    return BehaviourSuite("stable", tuple(specs)), {s.behaviour_id: s for s in specs}


def test_a_pair_drafted_in_one_chunk_is_credited_on_a_sibling_chunk_whose_draft_is_wrong() -> None:
    suite, pool = _two_chunk_ensure_suite()
    glossary = (GlossaryEntry("ensure!", "macro"),)
    answer = _base_suffix_answer(pool)

    def model(calls: list[str]) -> Callable[[str], str]:
        def ask(prompt: str) -> str:
            calls.append(prompt)
            ids = re.findall(r'<EXAMPLE id="([^"]+)">', prompt)
            if "TASK: state the observable RULES" in prompt:
                if "s0" in ids:
                    return (f'<RULE covers="s0,s1,s2">{BASE.sentence}</RULE>'
                            f'<RULE covers="s0,s1">{SUFFIX.sentence}</RULE>')
                return f'<RULE covers="s3,s4,s5">{WRONG.sentence}</RULE>'
            rules = frozenset(re.findall(r"^\d+\. (.*)$", prompt, re.M))
            if WRONG.sentence in rules:
                return "".join(f'<THEN id="{i}">Condition failed x</THEN>' for i in ids)
            return "".join(f'<THEN id="{i}">{answer(rules, i)}</THEN>' for i in ids)
        return ask

    calls: list[str] = []
    book, reading = build_behaviour_book(
        suite, crate="krate", glossary=glossary, private=set(), ask=model(calls),
        budget=BookBudget(examples_per_prompt=3))
    rules = {r.sentence: r.example_ids for r in book.features[0].rules}
    assert rules == {BASE.sentence: ("s0", "s1", "s2", "s3", "s4", "s5"),
                     SUFFIX.sentence: ("s0", "s1", "s3", "s4")}
    assert reading.covered_examples == 6 and reading.sibling_examples == 3 and reading.kept == 2
    assert "6/6 checkable examples covered (3 by a sibling chunk's rule; 0 predicted with no rule)" \
        in reading.line()
    assert reading.unaided_examples == 0 and book.features[0].unaided_ids == ()
    assert reading.dropped == {"refuted on": 1}
    # the wrong draft does not travel, and the sibling chunk's ablation is asked once
    predicts = [p for p in calls if p.startswith("Predict what")]
    visit = [p for p in predicts if 'id="s3"' in p and BASE.sentence in p]
    assert all(WRONG.sentence not in p for p in visit)
    assert sum(1 for p in predicts if "RULES (hold" not in p and 'id="s3"' in p) == 1
    # the visit: the set, then one leave-one-out per travelling sentence
    assert len(visit) == 2 and sum(1 for p in predicts if SUFFIX.sentence in p and 'id="s3"' in p) == 2

    # NEGATIVE CONTROL: each chunk left with the drafts written from its own
    # examples — the second chunk keeps nothing and half the feature is uncovered.
    alone, flat = build_behaviour_book(
        suite, crate="krate", glossary=glossary, private=set(), ask=model([]),
        budget=BookBudget(examples_per_prompt=3, sibling_chunks=False))
    assert flat.covered_examples == 3 and flat.sibling_examples == 0
    assert {r.sentence: r.example_ids for r in alone.features[0].rules} == {
        BASE.sentence: ("s0", "s1", "s2"), SUFFIX.sentence: ("s0", "s1")}
    # lanes do not change the book
    par, par_reading = build_behaviour_book(
        suite, crate="krate", glossary=glossary, private=set(), ask=model([]),
        budget=BookBudget(examples_per_prompt=3, lanes=4))
    assert par.to_dict() == book.to_dict() and replace(par_reading, lanes=1) == replace(reading, lanes=1)


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
        right = "RULES (hold together" in prompt and good in prompt
        return "".join(
            f'<THEN id="{i}">{pool[i].then.value if right else "wrong"}</THEN>'  # type: ignore[union-attr]
            for i in ids)

    book, reading = build_behaviour_book(
        suite, crate="anyhow", glossary=GLOSSARY, private={"construct"}, ask=ask,
        budget=BookBudget(max_features=1))
    ensure = next(f for f in book.features if f.item == "ensure!")
    assert [r.sentence for r in ensure.rules] == [good]
    assert reading.kept == 1 and reading.features_asked == 1
    # "It is a macro." rides on the set but the set without it still predicts
    assert reading.dropped == {"not necessary": 1, "names the private item": 1}
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
        if "RULES (hold together" in prompt:
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
    miss = judge_rule_set([RuleDraft("s", ("e1",))], pool, predict=_Checker(lambda _r, _i: None))
    assert not miss.kept and miss.dropped[0].startswith("set did not answer")
    assert judge_rule_set([], pool, predict=_Checker(lambda _r, _i: None)).calls == 0
    assert BookReading().line().endswith("dropped: none; lanes=1; predict-temp=server")
    assert "predict-temp=0;" in BookReading(predict_temperature=0.0).line() + ";"
    for reason in ("refuted on 2 of its 3 example(s)", "not necessary for its 1 gained example(s)",
                   "leave-one-out did not answer 1 of its 1 gained example(s)"):
        assert drop_reason_class(reason) != reason


def test_an_example_the_checker_predicts_with_no_rule_is_recorded_on_its_feature() -> None:
    suite, pool = _two_chunk_ensure_suite()
    glossary = (GlossaryEntry("ensure!", "macro"),)

    def ask(prompt: str) -> str:
        ids = re.findall(r'<EXAMPLE id="([^"]+)">', prompt)
        if "TASK: state the observable RULES" in prompt:
            return f'<RULE covers="{",".join(ids)}">{BASE.sentence}</RULE>'
        rules = "RULES (hold together" in prompt
        # with no rule the checker gets the two plain conditions right and nothing else
        return "".join(
            f'<THEN id="{i}">{pool[i].then.value if rules or i in ("s2", "s5") else "no"}</THEN>'  # type: ignore[union-attr]
            for i in ids)

    book, reading = build_behaviour_book(suite, crate="krate", glossary=glossary, private=set(),
                                         ask=ask, budget=BookBudget(examples_per_prompt=3))
    (feature,) = book.features
    assert feature.unaided_ids == ("s2", "s5") and reading.unaided_examples == 2
    assert "2 predicted with no rule" in reading.line()
    assert not set(feature.unaided_ids) & {i for r in feature.rules for i in r.example_ids}
    back = BehaviourSuite.from_dict(json.loads(json.dumps(book.to_dict())))
    assert back.features[0].unaided_ids == ("s2", "s5")
    assert "unaided_ids" not in Feature("f", "fn", ("a",)).to_dict()
