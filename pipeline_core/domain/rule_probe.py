"""Rule probes: test a kept rule of the behaviour book on cases it was not
drafted from.

A rule is kept by `behaviour_book.judge_rule_set` for the examples it
predicts, and every one of those examples was in front of the model that
wrote the sentence, or is one of the crate's own tests. Nothing tests the
sentence on a case chosen BECAUSE the sentence covers it. A sentence the
cited Thens cannot falsify is kept as written: on run `72627-751534fe`
(2026-09-30) a kept rule said `Error::downcast::<T>` "yields Some" for a fn
that returns a `Result`, and its cited Thens were booleans.

Per kept rule the model is asked for programs the sentence covers that
differ from its examples; each runs against the ORIGINAL pre-strip and its
stdout is the Then (the probe contract: no value is ever guessed). The rule
is then judged on those captures, ONE rule at a time:

- CONFIRMED on a capture when the feature's kept sentences together predict
  it and the checker with no rule does not. It joins the rule's examples.
- REFUTED on a capture when the kept sentences together answered it wrong
  AND this sentence alone answered it wrong too. It joins `refuted_on`.
  The second call is what makes the verdict the rule's own: a set that
  misses because of a companion is not this rule's refutation, and without
  it one wrong Then would be stamped on every sentence of the feature. The
  companions are in the first call because a rule can state part of an
  observable another completes (a base and a suffix sentence predict only
  together), and judged alone such a rule would be refuted on everything.
- Silence is neither. An abstention (`Grade.UNDETERMINED`) by the
  checker with no rule counts as not predicted, like a wrong value, so a
  rule that decides a capture nothing else decides is confirmed on it; by
  the kept sentences it is neither a confirmation nor a refutation. Both
  readings are `behaviour_book.predicted`, the grade the set judge uses.

A rule refuted on EVERY new capture, with at least one, is dropped. Its
captures stay: the crate printed them, and they are true whatever the
sentence said.
"""
from __future__ import annotations

from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, replace

from pipeline_core.domain.behaviour import BehaviourSpec, Feature, Rule
from pipeline_core.domain.behaviour_book import Grade, Predict, Prediction, predicted

DROP_REASON = "refuted on every new capture"


@dataclass(frozen=True, slots=True)
class RuleAsk:
    """One rule's probe request: its sentence, the credited examples shown
    as what the rule already covers, and the examples it was refuted on
    (`near`: programs are asked around those too)."""

    feature: str
    rule_id: str
    sentence: str
    shown: tuple[str, ...]
    near: tuple[str, ...] = ()


def rule_probe_asks(features: Sequence[Feature], *, max_rules: int,
                    shown: int = 3) -> tuple[RuleAsk, ...]:
    """The rules to probe: most credited examples first (the rules the most
    prompts carry), then by id; `max_rules` of them."""
    rules = sorted((r for f in features for r in f.rules),
                   key=lambda r: (-len(r.example_ids), r.rule_id))
    return tuple(RuleAsk(r.feature, r.rule_id, r.sentence, r.example_ids[:shown],
                         r.refuted_on[:shown])
                 for r in rules[:max(0, max_rules)])


@dataclass(frozen=True, slots=True)
class RuleTrial:
    """One rule's verdict on its new captures, and the checker calls spent."""

    confirmed: tuple[str, ...] = ()
    refuted: tuple[str, ...] = ()
    judged: int = 0
    calls: int = 0

    @property
    def drops(self) -> bool:
        """Refuted on every new capture, and there was one."""
        return self.judged > 0 and len(self.refuted) == self.judged


def judge_rule_on_captures(
    sentence: str,
    companions: Sequence[str],
    examples: Mapping[str, BehaviourSpec],
    *,
    predict: Predict,
    both: Callable[[Callable[[], Mapping[str, Prediction]], Callable[[], Mapping[str, Prediction]]],
                   tuple[Mapping[str, Prediction], Mapping[str, Prediction]]] | None = None,
) -> RuleTrial:
    """Judge `sentence` on `examples` (its new captures). `companions` are
    the other kept sentences of its feature.

    The ablation and the together call read nothing of each other; `both`,
    when given, runs the two and returns their replies in that order (the
    caller's lanes). The `alone` call reads together's misses, so it is
    asked after both. Absent, the two run one after the other."""
    ids = list(examples)
    if not ids:
        return RuleTrial()

    def grade(reply: Mapping[str, Prediction], bid: str) -> Grade:
        return predicted(reply, bid, examples[bid])

    everyone = tuple(dict.fromkeys((sentence, *companions)))
    if both is None:
        ablation = predict((), ids)
        together = predict(everyone, ids)
    else:
        ablation, together = both(lambda: predict((), ids), lambda: predict(everyone, ids))
    calls = 2
    confirmed = tuple(i for i in ids
                      if grade(together, i) is Grade.RIGHT and grade(ablation, i).not_predicted)
    missed = [i for i in ids if grade(together, i) is Grade.WRONG]
    if missed and len(everyone) > 1:
        alone = predict((sentence,), missed)
        calls += 1
    else:
        alone = together
    refuted = tuple(i for i in missed if grade(alone, i) is Grade.WRONG)
    return RuleTrial(confirmed, refuted, len(ids), calls)


def apply_trials(
    features: Sequence[Feature],
    captures: Mapping[str, Sequence[str]],
    trials: Mapping[str, RuleTrial],
) -> tuple[tuple[Feature, ...], tuple[str, ...]]:
    """`features` after the trials, and the ids of the rules dropped.
    `captures` is `rule id -> its new capture ids`: every one joins its
    rule's feature as an example, whatever the verdict."""
    out: list[Feature] = []
    dropped: list[str] = []
    for f in features:
        rules: list[Rule] = []
        added: list[str] = []
        for r in f.rules:
            added.extend(captures.get(r.rule_id, ()))
            t = trials.get(r.rule_id)
            if t is None:
                rules.append(r)
            elif t.drops:
                dropped.append(r.rule_id)
            else:
                rules.append(replace(
                    r, example_ids=tuple(dict.fromkeys((*r.example_ids, *t.confirmed))),
                    refuted_on=tuple(dict.fromkeys((*r.refuted_on, *t.refuted)))))
        out.append(replace(f, example_ids=tuple(dict.fromkeys((*f.example_ids, *added))),
                           rules=tuple(rules)))
    return tuple(out), tuple(dropped)


def without_examples(features: Sequence[Feature], gone: Collection[str]) -> tuple[Feature, ...]:
    """`features` with the examples in `gone` removed everywhere (a capture
    the emitter withdrew is not a specification). A rule left crediting
    nothing is dropped, as a rule with no example always is."""
    if not gone:
        return tuple(features)
    out: list[Feature] = []
    for f in features:
        rules = [replace(r, example_ids=tuple(i for i in r.example_ids if i not in gone),
                         refuted_on=tuple(i for i in r.refuted_on if i not in gone))
                 for r in f.rules]
        out.append(replace(f, example_ids=tuple(i for i in f.example_ids if i not in gone),
                           rules=tuple(r for r in rules if r.example_ids)))
    return tuple(out)
