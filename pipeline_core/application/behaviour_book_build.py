"""Build the behaviour book at t0d: glossary → features → checked rules.

Runs after the suite's examples are final for the pass (harvest, probes,
exercises, emission). Per feature with checkable examples, in chunks: one
model call drafts rules; the drafts that survive `draft_refusal` are judged
as ONE set by `behaviour_book.judge_rule_set` — an ablation call (no rule), a
set call (every draft) and one leave-one-out call per draft citing a gained
example — and each is kept for the examples it is necessary for. Every draft
that is refused or earns no credit is counted by reason. Then a feature's
kept sentences are judged together on each of its chunks' still-uncovered
examples (`behaviour_book.sibling_visits`): a sentence is drafted from one
chunk's examples and holds for the feature.

The author calls go through `ask` (sampled: diversity in drafting is the
point); the judge's calls go through `predict`, which the composition binds
with `BEHAVIOUR_BOOK_PREDICT_TEMPERATURE` (a sampled judge gives two verdicts
on one sentence: the two books of run `72627-751534fe` kept disjoint rules
over identical examples).

Cost is bounded by three knobs read by the caller: features asked (most
checkable examples first), examples per prompt, drafts honoured per reply.
Wall time by a fourth: the chunks run on `lanes` threads (run
`72627-751534fe`, 2026-09-30: 62 calls one at a time, 10,090 s).
With rules disabled the book still carries the glossary and the features —
the grouping is deterministic and free; only the sentences cost calls.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Collection, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace

from pipeline_core.domain.behaviour import (
    BehaviourSpec,
    BehaviourSuite,
    Feature,
    GlossaryEntry,
    Rule,
    outcome_matches,
)
from pipeline_core.domain.behaviour_book import (
    BookReading,
    ChunkJudgment,
    CreditedRule,
    Predict,
    RuleDraft,
    draft_refusal,
    drop_reason_class,
    feature_file_name,
    feature_spellings,
    group_features,
    judge_rule_set,
    judge_visit,
    parse_predictions,
    parse_rule_drafts,
    render_feature,
    rule_checkable,
    rule_terms,
    sibling_visits,
)
from pipeline_core.prompts.behaviour_rules import (
    build_rule_authoring_prompt,
    build_then_prediction_prompt,
)


@dataclass(frozen=True)
class BookBudget:
    """The bounds on t0d's rule calls. `rules=False` builds the book without
    sentences (glossary and features only). `lanes` is how many chunks are
    drafted and checked at once (the pipeline's parallelism setting).
    `predict_temperature` is the temperature the composition binds the
    judge's `predict` with, carried here only so the reading prints it
    (None: the server default). `sibling_chunks=False` leaves every chunk
    with the rules drafted from its own examples (the build before
    2026-10-01, and the negative control of the sibling judgment)."""

    rules: bool = True
    max_features: int = 24
    examples_per_prompt: int = 24
    drafts_per_reply: int = 6
    lanes: int = 1
    predict_temperature: float | None = None
    sibling_chunks: bool = True


def _chunks(items: Sequence[BehaviourSpec], size: int) -> list[Sequence[BehaviourSpec]]:
    size = max(1, size)
    return [items[i:i + size] for i in range(0, len(items), size)]


@dataclass(frozen=True)
class _ChunkResult:
    """One chunk's draft → ablation → checks: the drafts it proposed, the
    kept rules in draft order (numbered at assembly), the refusals; and what
    a sibling judgment of the chunk needs — its examples, the sentences its
    set carried, and its checker, which holds the chunk's ablation."""

    proposed: int
    kept: tuple[Rule, ...]
    dropped: Counter[str]
    pool: Mapping[str, BehaviourSpec]
    judged: tuple[str, ...]
    check: Predict
    # The examples the ablation answered right (empty when no draft
    # survived: the ablation was then never asked).
    unaided: tuple[str, ...] = ()

    def judgment(self) -> ChunkJudgment:
        return ChunkJudgment(tuple(self.pool), self.judged, tuple(
            CreditedRule(RuleDraft(r.sentence, r.example_ids), r.example_ids) for r in self.kept))


def build_behaviour_book(
    suite: BehaviourSuite,
    *,
    crate: str,
    glossary: Sequence[GlossaryEntry],
    private: Collection[str],
    ask: Callable[[str], str],
    budget: BookBudget,
    log: Callable[[str], None] = lambda _m: None,
    worlds: Mapping[str, Sequence[str]] | None = None,
    predict: Callable[[str], str] | None = None,
) -> tuple[BehaviourSuite, BookReading]:
    """`suite` with its features, rules and glossary replaced by a fresh
    build, and the reading of that build. `worlds` marks, by example id,
    the examples only some build worlds compile. `predict` answers the
    judge's calls; None sends them through `ask`.

    Every chunk of every asked feature is independent — its ablation and
    its checks read only that chunk's examples — so the chunks run on
    `budget.lanes` threads; the sibling judgments, one per chunk at most
    and each on its own chunk's examples, run on the same lanes after them.
    The book is assembled in feature order and chunk order afterwards, rules
    numbered then, the per-feature lines logged then: given the same replies
    the result and the log are the serial build's."""
    features = group_features(suite.specs, glossary, crate=crate)
    by_id = {s.behaviour_id: s for s in suite.specs}
    checkable_of = {f.item: [by_id[i] for i in f.example_ids if rule_checkable(by_id[i])]
                    for f in features}
    asked_order = sorted((f for f in features if checkable_of[f.item]),
                         key=lambda f: (-len(checkable_of[f.item]), f.item))
    asked = {f.item for f in asked_order[:budget.max_features]} if budget.rules else set()
    tasks = [(f, chunk) for f in features if f.item in asked
             for chunk in _chunks(checkable_of[f.item], budget.examples_per_prompt)]

    def run(task: tuple[Feature, Sequence[BehaviourSpec]]) -> _ChunkResult:
        f, chunk = task
        return _chunk(f, chunk, glossary=glossary, private=private, crate=crate, ask=ask,
                      predict=predict or ask, budget=budget, worlds=worlds)

    lanes = max(1, min(budget.lanes, len(tasks) or 1))
    results = _on_lanes(run, tasks, lanes)
    per_feature: dict[str, list[_ChunkResult]] = {}
    for (f, _chunk_specs), r in zip(tasks, results, strict=True):
        per_feature.setdefault(f.item, []).append(r)
    visits = [(item, rs[v.chunk], v) for item, rs in per_feature.items()
              for v in sibling_visits([r.judgment() for r in rs])] if budget.sibling_chunks else []
    travelled: dict[str, dict[str, list[str]]] = {}
    for (item, _r, _v), credit in zip(
            visits, _on_lanes(lambda t: judge_visit(t[2], t[1].pool, predict=t[1].check)[0],
                              visits, lanes), strict=True):
        for sentence, ids in credit.items():
            travelled.setdefault(item, {}).setdefault(sentence, []).extend(ids)
    dropped: Counter[str] = Counter()
    proposed = kept = 0
    sibling: set[str] = set()
    unaided = 0
    built: list[Feature] = []
    for f in features:
        if f.item not in asked:
            built.append(f)
            continue
        rules: list[Rule] = []
        for r in per_feature.get(f.item, []):
            proposed += r.proposed
            dropped.update(r.dropped)
            rules.extend(replace(rule, rule_id=f"rule::{f.item}::{len(rules) + i}")
                         for i, rule in enumerate(r.kept))
        # A sibling chunk's credit goes to the first rule with that sentence.
        abroad = dict(travelled.get(f.item, {}))
        order = [s.behaviour_id for s in checkable_of[f.item]]
        for n, rule in enumerate(rules):
            more = set(abroad.pop(rule.sentence, ()))
            if more:
                sibling |= more
                rules[n] = replace(rule, example_ids=tuple(
                    i for i in order if i in more or i in rule.example_ids))
        kept += len(rules)
        plain = tuple(i for r in per_feature.get(f.item, []) for i in r.unaided)
        unaided += len(plain)
        built.append(replace(f, rules=tuple(rules), unaided_ids=plain))
        log(f"    [behaviour-book] {f.item}: {len(checkable_of[f.item])} checkable "
            f"example(s), {len(rules)} rule(s) kept")
    checkable = sum(len(v) for v in checkable_of.values())
    covered = {i for f in built for r in f.rules for i in r.example_ids}
    reading = BookReading(
        features=len(features), features_asked=len(asked),
        skipped_features=max(0, len(asked_order) - budget.max_features) if budget.rules else 0,
        checkable_examples=checkable, covered_examples=len(covered),
        proposed=proposed, kept=kept, dropped=dict(dropped), lanes=lanes,
        predict_temperature=budget.predict_temperature, sibling_examples=len(sibling),
        unaided_examples=unaided)
    out = BehaviourSuite(
        cfg_cluster=suite.cfg_cluster,
        specs=tuple(replace(s, rules=()) for s in suite.specs),
        helpers=suite.helpers, features=tuple(built), glossary=tuple(glossary))
    return out, reading


def _on_lanes[T, R](fn: Callable[[T], R], items: Sequence[T], lanes: int) -> list[R]:
    """`fn` over `items`, results in item order, on up to `lanes` threads."""
    if lanes > 1 and len(items) > 1:
        with ThreadPoolExecutor(max_workers=lanes, thread_name_prefix="behaviour-book") as pool:
            return list(pool.map(fn, items))
    return [fn(t) for t in items]


def _chunk(
    f: Feature,
    chunk: Sequence[BehaviourSpec],
    *,
    glossary: Sequence[GlossaryEntry],
    private: Collection[str],
    crate: str,
    ask: Callable[[str], str],
    predict: Callable[[str], str],
    budget: BookBudget,
    worlds: Mapping[str, Sequence[str]] | None,
) -> _ChunkResult:
    dropped: Counter[str] = Counter()
    lines = [g for g in glossary if g.leaf == f.item.rstrip("!") or g.name == f.item]
    pool = {s.behaviour_id: s for s in chunk}
    drafts = parse_rule_drafts(ask(build_rule_authoring_prompt(
        crate=crate, feature=f.item, feature_lines=lines, glossary=glossary,
        examples=chunk, worlds=worlds)))
    over = len(drafts) - budget.drafts_per_reply
    if over > 0:
        dropped["over the drafts-per-reply cap"] += over
        drafts = drafts[:budget.drafts_per_reply]
    proposed = len(drafts) + max(0, over)
    surviving: list[RuleDraft] = []
    for d in drafts:
        why = draft_refusal(d, pool, private)
        if why is None:
            surviving.append(d)
        else:
            dropped[drop_reason_class(why)] += 1
    ablation: list[dict[str, str]] = []

    def check(rules: Sequence[str], ids: Sequence[str]) -> Mapping[str, str]:
        """One checker call. The ablation (no rule) is asked once, over the
        whole chunk, and answered from that reply afterwards: a sibling
        judgment of this chunk measures gain against the same reading."""
        if not rules:
            if not ablation:
                ablation.append(parse_predictions(predict(build_then_prediction_prompt(
                    crate=crate, glossary=glossary, examples=list(pool.values()),
                    worlds=worlds))))
            return {i: ablation[0][i] for i in ids if i in ablation[0]}
        return parse_predictions(predict(build_then_prediction_prompt(
            crate=crate, glossary=glossary, examples=[pool[i] for i in ids], rules=rules,
            worlds=worlds)))

    judged = tuple(d.sentence for d in surviving)
    if not surviving:
        return _ChunkResult(proposed, (), dropped, pool, judged, check)

    verdict = judge_rule_set(surviving, pool, predict=check)
    dropped.update(drop_reason_class(why) for why in verdict.dropped)
    kept = tuple(
        Rule(
            rule_id=f"rule::{f.item}::{n}",
            sentence=c.draft.sentence,
            example_ids=c.credited,
            feature=f.item,
            # Aiming matches a unit by a glossary spelling. The feature
            # contributes its own spelling, or the one qualified fn when the
            # feature is a leaf only that fn uses — never every fn that shares
            # the leaf.
            terms=tuple(sorted(set(rule_terms(c.draft.sentence, glossary))
                               | set(feature_spellings(f.item, glossary)))),
            refuted_on=c.refuted_on)
        for n, c in enumerate(verdict.kept))
    unaided = tuple(
        i for i, spec in pool.items()
        if spec.then is not None and i in ablation[0] and outcome_matches(ablation[0][i], spec.then)
    ) if ablation else ()
    return _ChunkResult(proposed, kept, dropped, pool, judged, check, unaided)


def feature_files(suite: BehaviourSuite) -> dict[str, str]:
    """`file name → text` of the Gherkin view of `suite`'s book."""
    by_id = {s.behaviour_id: s for s in suite.specs}
    return {feature_file_name(f): render_feature(f, suite.glossary, by_id) for f in suite.features}
