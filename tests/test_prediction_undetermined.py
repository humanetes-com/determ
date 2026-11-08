"""The prediction prompt's abstention, and the one grade every judge of a
parsed prediction reads (`behaviour_book.predicted`). Invented shapes.

The class: a prediction reply had no way to say "nothing in the prompt
determines this value", so an underdetermined example could only be
guessed. On run `3198683-b5e39b06` (2026-11-06, hardcrate, Qwen3.8) three
book prediction calls took 28.1, 10.1 and 17.1 minutes; the 17.1-minute
one reasoned 161,572 chars over two examples whose value no glossary line
or rule states and guessed 33 / 32 for captures 255 / 64. The guess was
scored like any wrong answer, and a lucky one on the no-rule side would
have marked a correct rule "adds nothing".

What the change must hold, and what each test's negative control is:
- the tag the prompt offers is the tag the parser stores (the value
  pattern alone drops it, and the id becomes silence);
- an abstention on the no-rule side is "not predicted", so a rule that
  decides the example is gained; silence is not (the control);
- an abstention with rules is neither a refutation nor "adds nothing";
- the rule-probe trial reads the same grade;
- no reader compares a parsed prediction as text outside `predicted`;
- the replay files each prediction by the examples it asks.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import re
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from types import ModuleType

from pipeline_core.application.behaviour_book_build import BookBudget, build_behaviour_book
from pipeline_core.domain import behaviour_book
from pipeline_core.domain.behaviour import (
    BehaviourSpec,
    BehaviourSubKind,
    BehaviourSuite,
    GlossaryEntry,
    ObservableOutcome,
)
from pipeline_core.domain.behaviour_book import (
    UNDETERMINED,
    Grade,
    Prediction,
    RuleDraft,
    judge_rule_set,
    parse_predictions,
    predicted,
)
from pipeline_core.domain.rule_probe import judge_rule_on_captures
from pipeline_core.prompts.behaviour_rules import UNDETERMINED_SLOT, build_then_prediction_prompt

REPO = Path(__file__).resolve().parents[1]
RULE = RuleDraft("`limit()` returns 64 in the default build.", ("x1",))
OTHER = RuleDraft("`limit()` renders as its decimal digits.", ("x1",))


def _spec(bid: str, value: str) -> BehaviourSpec:
    return BehaviourSpec(bid, "", "limit()", ObservableOutcome.display(value),
                         subkind=BehaviourSubKind.OUTCOME)


def _pool() -> dict[str, BehaviourSpec]:
    return {"x1": _spec("x1", "64")}


class _Checker:
    """`answer(rules, id)` → the parsed prediction for one example under one
    rule set: a value, `UNDETERMINED`, or None (the reply omits the id)."""

    def __init__(self, answer: Callable[[frozenset[str], str], Prediction | None]) -> None:
        self.answer = answer
        self.calls: list[tuple[tuple[str, ...], tuple[str, ...]]] = []

    def __call__(self, rules: Sequence[str], ids: Sequence[str]) -> dict[str, Prediction]:
        self.calls.append((tuple(rules), tuple(ids)))
        return {i: v for i in ids if (v := self.answer(frozenset(rules), i)) is not None}


# --- the slot and the parser ----------------------------------------------------

def test_the_tag_the_prompt_offers_is_the_tag_the_parser_stores() -> None:
    prompt = build_then_prediction_prompt(crate="c", glossary=(), examples=[_spec("x1", "64")])
    assert UNDETERMINED_SLOT in prompt
    offered = re.search(r'<THEN id="<example id>" undetermined/>', prompt)
    assert offered is not None
    reply = offered.group(0).replace("<example id>", "x1")
    assert parse_predictions(reply) == {"x1": UNDETERMINED}
    # NEGATIVE CONTROL: the value pattern alone (the parser before the
    # slot) reads nothing from the tag, so the abstention became silence.
    assert behaviour_book._THEN_RE.findall(reply) == []


def test_the_parser_lets_the_tag_win_and_keeps_the_word_as_a_value() -> None:
    assert parse_predictions('<THEN id="a">7</THEN><THEN id="a" undetermined/>') == {"a": UNDETERMINED}
    assert parse_predictions('<THEN id="a" undetermined/><THEN id="a">7</THEN>') == {"a": UNDETERMINED}
    assert parse_predictions('<THEN id="a" undetermined></THEN>') == {"a": UNDETERMINED}
    assert parse_predictions('<THEN id="a">undetermined</THEN>') == {"a": "undetermined"}
    assert parse_predictions('<think><THEN id="a" undetermined/></think><THEN id="a">7</THEN>') == {
        "a": "7"}
    assert "b" not in parse_predictions('<THEN id="a">7</THEN>')


def test_one_grade_names_the_four_readings() -> None:
    spec = _spec("x1", "64")
    assert predicted({"x1": "64"}, "x1", spec) is Grade.RIGHT
    assert predicted({"x1": "33"}, "x1", spec) is Grade.WRONG
    assert predicted({"x1": UNDETERMINED}, "x1", spec) is Grade.UNDETERMINED
    assert predicted({}, "x1", spec) is Grade.UNANSWERED
    no_then = BehaviourSpec("x1", "", "assert!(limit() > 0)", None,
                            subkind=BehaviourSubKind.ASSERTION)
    assert predicted({"x1": "64"}, "x1", no_then) is Grade.UNANSWERED
    assert [g for g in Grade if g.not_predicted] == [Grade.WRONG, Grade.UNDETERMINED]


# --- the set judge --------------------------------------------------------------

def test_a_rule_that_decides_an_undetermined_example_is_gained_and_silence_is_not() -> None:
    pool = _pool()
    abstains = _Checker(lambda rules, _i: "64" if rules else UNDETERMINED)
    verdict = judge_rule_set([RULE], pool, predict=abstains)
    assert [c.credited for c in verdict.kept] == [("x1",)]
    # NEGATIVE CONTROL: the same reply with the id dropped (what the slot
    # without the parser produced) is silence: not gained, nothing kept.
    silent = judge_rule_set([RULE], pool, predict=_Checker(lambda rules, _i: "64" if rules else None))
    assert not silent.kept and silent.dropped[0].startswith("ablation did not answer")


def test_a_leave_one_out_that_abstains_makes_the_draft_necessary() -> None:
    pool = _pool()

    def answer(rules: frozenset[str], _i: str) -> Prediction | None:
        if not rules:
            return "33"
        return "64" if RULE.sentence in rules else UNDETERMINED

    verdict = judge_rule_set([RULE, OTHER], pool, predict=_Checker(answer))
    assert [c.draft for c in verdict.kept] == [RULE]
    # NEGATIVE CONTROL: a leave-one-out that says nothing leaves the gain
    # with no necessary draft and credits nobody.
    def silent_loo(rules: frozenset[str], _i: str) -> Prediction | None:
        if not rules:
            return "33"
        return "64" if RULE.sentence in rules else None

    assert not judge_rule_set([RULE, OTHER], pool, predict=_Checker(silent_loo)).kept


def test_an_abstention_with_rules_is_no_refutation_and_not_adds_nothing() -> None:
    pool = _pool()
    verdict = judge_rule_set([RULE], pool, predict=_Checker(
        lambda rules, _i: UNDETERMINED if rules else "33"))
    assert not verdict.kept
    assert verdict.dropped == ("set left 1 of its 1 example(s) undetermined",)
    assert behaviour_book.drop_reason_class(verdict.dropped[0]) == "set left"
    # NEGATIVE CONTROL: a wrong guess in the same slot is a refutation.
    guessed = judge_rule_set([RULE], pool, predict=_Checker(lambda rules, _i: "32" if rules else "33"))
    assert guessed.dropped[0].startswith("refuted on")


# --- the rule-probe trial ---------------------------------------------------------

def test_the_rule_probe_trial_reads_the_same_grade() -> None:
    caps = _pool()
    confirmed = judge_rule_on_captures(RULE.sentence, [], caps, predict=_Checker(
        lambda rules, _i: "64" if rules else UNDETERMINED))
    assert confirmed.confirmed == ("x1",) and not confirmed.refuted
    # NEGATIVE CONTROL: an ablation that said nothing confirms nothing
    # ("not RIGHT" would have confirmed it).
    silent = judge_rule_on_captures(RULE.sentence, [], caps, predict=_Checker(
        lambda rules, _i: "64" if rules else None))
    assert silent.confirmed == ()
    abstained = judge_rule_on_captures(RULE.sentence, [OTHER.sentence], caps, predict=_Checker(
        lambda rules, _i: UNDETERMINED if rules else "33"))
    assert abstained.refuted == () and not abstained.drops
    # NEGATIVE CONTROL: wrong together and wrong alone is the refutation.
    wrong = judge_rule_on_captures(RULE.sentence, [OTHER.sentence], caps, predict=_Checker(
        lambda rules, _i: "32" if rules else "33"))
    assert wrong.refuted == ("x1",) and wrong.drops


# --- the book build ---------------------------------------------------------------

def test_the_build_counts_unaided_as_right_only_and_counts_the_undetermined() -> None:
    specs = (_spec("x1", "64"), _spec("x2", "7"), _spec("x3", "9"))
    suite = BehaviourSuite("stable", specs)
    glossary = (GlossaryEntry("limit", "fn"),)
    rule = "`limit()` returns 64."

    def ask(prompt: str) -> str:
        ids = re.findall(r'<EXAMPLE id="([^"]+)">', prompt)
        if "TASK: state the observable RULES" in prompt:
            return f'<RULE covers="x1">{rule}</RULE>'
        with_rule = rule in prompt
        out = []
        for i in ids:
            if i == "x1":
                out.append('<THEN id="x1">64</THEN>' if with_rule else '<THEN id="x1" undetermined/>')
            elif i == "x2":
                out.append('<THEN id="x2">7</THEN>')
            else:
                out.append('<THEN id="x3" undetermined/>')
        return "".join(out)

    book, reading = build_behaviour_book(suite, crate="c", glossary=glossary, private=set(),
                                         ask=ask, budget=BookBudget(given_phrases=False))
    assert reading.unaided_examples == 1 and reading.undetermined_examples == 2
    assert "1 predicted with no rule, 2 undetermined with no rule" in reading.line()
    assert reading.payload()["undetermined_examples"] == 2
    assert {i for f in book.features for r in f.rules for i in r.example_ids} == {"x1"}


# --- no reader compares a parsed prediction as text ------------------------------

def _text_compares(source: str, allowed: frozenset[str] = frozenset()) -> list[str]:
    """The functions in `source` that call `outcome_matches`, less `allowed`."""
    tree = ast.parse(source)
    found: list[str] = []

    def visit(node: ast.AST, owner: str) -> None:
        for child in ast.iter_child_nodes(node):
            name = owner
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = child.name if owner == "<module>" else owner
            if isinstance(child, ast.Call):
                f = child.func
                called = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""
                if called == "outcome_matches" and name not in allowed:
                    found.append(name)
            visit(child, name)

    visit(tree, "<module>")
    return found


def test_no_reader_outside_predicted_compares_a_parsed_prediction_as_text() -> None:
    offenders: dict[str, list[str]] = {}
    for path in sorted((REPO / "pipeline_core").rglob("*.py")):
        rel = str(path.relative_to(REPO))
        if rel == "pipeline_core/domain/behaviour.py":
            continue  # the comparison's definition and its capture-side use
        allowed = frozenset({"predicted"}) if rel == "pipeline_core/domain/behaviour_book.py" \
            else frozenset()
        hits = _text_compares(path.read_text(encoding="utf-8"), allowed)
        if hits:
            offenders[rel] = hits
    replay = REPO / "scripts" / "book_judge_replay.py"
    if hits := _text_compares(replay.read_text(encoding="utf-8")):
        offenders[str(replay.relative_to(REPO))] = hits
    assert offenders == {}
    # NEGATIVE CONTROL: the unaided count as it was written before the grade.
    before = (
        "def _chunk(pool, ablation):\n"
        "    return tuple(i for i, spec in pool.items()\n"
        "        if spec.then is not None and i in ablation[0]"
        " and outcome_matches(ablation[0][i], spec.then))\n")
    assert _text_compares(before) == ["_chunk"]


# --- the replay files each prediction by its examples ----------------------------

def _replay() -> ModuleType:
    path = REPO / "scripts" / "book_judge_replay.py"
    spec = importlib.util.spec_from_file_location("book_judge_replay_under_test", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _author(ids: Sequence[str]) -> str:
    blocks = "".join(f'<EXAMPLE id="{i}">\n</EXAMPLE>\n' for i in ids)
    return ("You are writing the specification of the Rust crate `c` from its examples.\n"
            f"FEATURE (the public item these examples exercise):\n- `f{ids[0]}`\n{blocks}")


def _predict(ids: Sequence[str], rules: Sequence[str] = ()) -> str:
    specs = [_spec(i, "1") for i in ids]
    return build_then_prediction_prompt(crate="c", glossary=(), examples=specs, rules=rules)


def _write_run(tmp: Path, calls: Sequence[tuple[str, str, str]], book_ts: str) -> tuple[Path, Path]:
    prompts = tmp / "prompts"
    prompts.mkdir()
    lines: list[dict[str, object]] = []
    for n, (ts, prompt, reply) in enumerate(calls):
        sha, rsha = f"p{n:02d}", f"r{n:02d}"
        (prompts / f"{sha}.txt").write_text(prompt, encoding="utf-8")
        (prompts / f"{rsha}.txt").write_text(reply, encoding="utf-8")
        lines.append({"ts": ts, "run_id": "run-1", "event": "llm.request", "data": {"prompt_sha": sha}})
        lines.append({"ts": ts, "run_id": "run-1", "event": "llm.response", "duration_ms": 60000,
                      "data": {"prompt_sha": sha, "response": {"sha256": rsha, "char_count": 1}}})
        lines.append({"ts": ts, "run_id": "run-1", "event": "llm.stream_meta",
                      "data": {"prompt_sha": sha, "reasoning_chars": 100 + n}})
    lines.append({"ts": book_ts, "run_id": "run-1", "event": "task0d.behaviour_book",
                  "cluster": "a+b", "data": {"kept": 0}})
    events = tmp / "events.jsonl"
    events.write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")
    return events, prompts


def test_the_replay_files_each_prediction_by_the_examples_it_asks(tmp_path: Path) -> None:
    r = _replay()
    calls = [
        ("t01", _author(["a1", "a2"]), "<RULE covers=\"a1\">S.</RULE>"),
        ("t02", _author(["b1"]), "<RULE covers=\"b1\">T.</RULE>"),
        ("t03", _predict(["a1", "a2"]), "ablation A"),   # lanes: A's ablation after B's author
        ("t04", _predict(["a1", "a2"], ["S."]), "set A"),
        ("t05", _predict(["b1"]), "ablation B"),
        ("t06", _predict(["b1"], ["T."]), "set B"),
        ("t08", _predict(["c1"]), "rule probe"),          # after the book event
    ]
    events, prompts = _write_run(tmp_path, calls, book_ts="t07")
    loaded = r.load_chunks("run-1", events=events, prompts=prompts)
    a, b = loaded.chunks
    assert (a.ids, a.ablation_reply, a.checks) == (["a1", "a2"], "ablation A", {("S.",): "set A"})
    assert (b.ids, b.ablation_reply, b.checks) == (["b1"], "ablation B", {("T.",): "set B"})
    assert a.ablation_reasoning == 102 and a.ablation_minutes == 1.0
    assert loaded.unfiled == []
    # NEGATIVE CONTROL: the filing rule before (the latest author takes every
    # prediction; only the one-sentence RULE header is a check, so every
    # set call overwrites the ablation; nothing stops at the book event)
    # gives A nothing and leaves the rule probe as B's ablation.
    latest: dict[str, str] = {}
    cur = ""
    for _ts, prompt, reply in calls:
        if prompt.startswith("You are writing"):
            cur = re.findall(r'<EXAMPLE id="([^"]+)">', prompt)[0]
        elif r._OLD_RULE.search(prompt) is None:
            latest[cur] = reply
    assert latest == {"b1": "rule probe"}


def test_the_replay_reads_the_set_judges_numbered_rules() -> None:
    r = _replay()
    prompt = _predict(["a1"], ["One `x`.", "Two  spaced\twords."])
    assert r.rules_of(prompt) == ("One `x`.", "Two spaced words.")
    assert r.rules_of(_predict(["a1"])) == ()
    assert r.rules_of(prompt.replace(UNDETERMINED_SLOT, "")) == ("One `x`.", "Two spaced words.")
