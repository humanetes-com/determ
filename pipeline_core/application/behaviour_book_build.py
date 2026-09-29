"""Build the behaviour book at t0d: glossary → features → checked rules.

Runs after the suite's examples are final for the pass (harvest, probes,
exercises, emission). Per feature with checkable examples, in chunks: one
model call drafts rules, one predicts every Then WITHOUT a rule (the
ablation), and one per surviving draft predicts its cited Thens WITH the
sentence; `behaviour_book.judge_draft` keeps the draft only when the second
is right on every cited example and the ablation missed one. Every draft that
is refused or fails the check is counted by reason.

Cost is bounded by three knobs read by the caller: features asked (most
checkable examples first), examples per prompt, drafts honoured per reply.
With rules disabled the book still carries the glossary and the features —
the grouping is deterministic and free; only the sentences cost calls.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, replace

from pipeline_core.domain.behaviour import BehaviourSpec, BehaviourSuite, Feature, GlossaryEntry, Rule
from pipeline_core.domain.behaviour_book import (
    BookReading,
    RuleDraft,
    draft_refusal,
    drop_reason_class,
    feature_file_name,
    feature_spellings,
    group_features,
    judge_draft,
    parse_predictions,
    parse_rule_drafts,
    render_feature,
    rule_checkable,
    rule_terms,
)
from pipeline_core.prompts.behaviour_rules import (
    build_rule_authoring_prompt,
    build_then_prediction_prompt,
)


@dataclass(frozen=True)
class BookBudget:
    """The three bounds on t0d's rule calls. `rules=False` builds the book
    without sentences (glossary and features only)."""

    rules: bool = True
    max_features: int = 24
    examples_per_prompt: int = 24
    drafts_per_reply: int = 6


def _chunks(items: Sequence[BehaviourSpec], size: int) -> list[Sequence[BehaviourSpec]]:
    size = max(1, size)
    return [items[i:i + size] for i in range(0, len(items), size)]


def build_behaviour_book(
    suite: BehaviourSuite,
    *,
    crate: str,
    glossary: Sequence[GlossaryEntry],
    private: Collection[str],
    ask: Callable[[str], str],
    budget: BookBudget,
    log: Callable[[str], None] = lambda _m: None,
) -> tuple[BehaviourSuite, BookReading]:
    """`suite` with its features, rules and glossary replaced by a fresh
    build, and the reading of that build."""
    features = group_features(suite.specs, glossary, crate=crate)
    by_id = {s.behaviour_id: s for s in suite.specs}
    checkable_of = {f.item: [by_id[i] for i in f.example_ids if rule_checkable(by_id[i])]
                    for f in features}
    asked_order = sorted((f for f in features if checkable_of[f.item]),
                         key=lambda f: (-len(checkable_of[f.item]), f.item))
    asked = {f.item for f in asked_order[:budget.max_features]} if budget.rules else set()
    dropped: Counter[str] = Counter()
    proposed = kept = 0
    built: list[Feature] = []
    for f in features:
        if f.item not in asked:
            built.append(f)
            continue
        lines = [g for g in glossary if g.leaf == f.item.rstrip("!") or g.name == f.item]
        rules: list[Rule] = []
        for chunk in _chunks(checkable_of[f.item], budget.examples_per_prompt):
            pool = {s.behaviour_id: s for s in chunk}
            drafts = parse_rule_drafts(ask(build_rule_authoring_prompt(
                crate=crate, feature=f.item, feature_lines=lines, glossary=glossary,
                examples=chunk)))
            over = len(drafts) - budget.drafts_per_reply
            if over > 0:
                dropped["over the drafts-per-reply cap"] += over
                drafts = drafts[:budget.drafts_per_reply]
            proposed += len(drafts) + max(0, over)
            if not drafts:
                continue
            ablation = parse_predictions(ask(build_then_prediction_prompt(
                crate=crate, glossary=glossary, examples=chunk)))
            for d in drafts:
                rule = _checked(d, pool, private, ablation, crate=crate, glossary=glossary,
                                ask=ask, dropped=dropped, feature=f.item, n=len(rules))
                if rule is not None:
                    rules.append(rule)
        kept += len(rules)
        built.append(replace(f, rules=tuple(rules)))
        log(f"    [behaviour-book] {f.item}: {len(checkable_of[f.item])} checkable "
            f"example(s), {len(rules)} rule(s) kept")
    checkable = sum(len(v) for v in checkable_of.values())
    covered = {i for f in built for r in f.rules for i in r.example_ids}
    reading = BookReading(
        features=len(features), features_asked=len(asked),
        skipped_features=max(0, len(asked_order) - budget.max_features) if budget.rules else 0,
        checkable_examples=checkable, covered_examples=len(covered),
        proposed=proposed, kept=kept, dropped=dict(dropped))
    out = BehaviourSuite(
        cfg_cluster=suite.cfg_cluster,
        specs=tuple(replace(s, rules=()) for s in suite.specs),
        helpers=suite.helpers, features=tuple(built), glossary=tuple(glossary))
    return out, reading


def _checked(
    draft: RuleDraft,
    pool: dict[str, BehaviourSpec],
    private: Collection[str],
    ablation: dict[str, str],
    *,
    crate: str,
    glossary: Sequence[GlossaryEntry],
    ask: Callable[[str], str],
    dropped: Counter[str],
    feature: str,
    n: int,
) -> Rule | None:
    why = draft_refusal(draft, pool, private)
    if why is not None:
        dropped[drop_reason_class(why)] += 1
        return None
    cited = [pool[i] for i in draft.covers if i in pool]
    with_rule = parse_predictions(ask(build_then_prediction_prompt(
        crate=crate, glossary=glossary, examples=cited, rule=draft.sentence)))
    verdict = judge_draft(draft, pool, with_rule=with_rule, without_rule=ablation)
    if not verdict.kept:
        dropped[drop_reason_class(verdict.reason)] += 1
        return None
    return Rule(
        rule_id=f"rule::{feature}::{n}",
        sentence=draft.sentence,
        example_ids=tuple(s.behaviour_id for s in cited),
        feature=feature,
        # Aiming matches a unit by a glossary spelling. The feature
        # contributes its own spelling, or the one qualified fn when the
        # feature is a leaf only that fn uses — never every fn that shares
        # the leaf.
        terms=tuple(sorted(set(rule_terms(draft.sentence, glossary))
                           | set(feature_spellings(feature, glossary)))))


def feature_files(suite: BehaviourSuite) -> dict[str, str]:
    """`file name → text` of the Gherkin view of `suite`'s book."""
    by_id = {s.behaviour_id: s for s in suite.specs}
    return {feature_file_name(f): render_feature(f, suite.glossary, by_id) for f in suite.features}
