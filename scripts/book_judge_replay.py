#!/usr/bin/env python3
"""Replay a run's behaviour-book drafts through the set judge, beside the
per-draft judge that ran live, before any relaunch pays for a bake.

WHY THIS EXISTS
  On run `72627-751534fe` (2026-09-30) the book judge checked each draft
  alone and all-or-nothing. Stable kept 2 of 31 drafts (15/197 checkable
  examples covered), nightly 3 of 38 (4/197), and `ensure!`, holding 103 of
  the 197 checkable examples, kept 0 in both books although its drafts
  were right: the model split the message into a base sentence and a
  suffix sentence that each predict nothing alone. `judge_rule_set`
  (`pipeline_core/domain/behaviour_book.py`) judges a chunk's drafts as one
  set and keeps each for the examples it is necessary for. This script
  computes that judge's verdicts on the SAME recorded drafts and ablations,
  so the claims of the change are computed, not assumed, and a bake (2h48m
  a book on that run) is spent only after they hold.

  On run `3198683-b5e39b06` (2026-11-06, hardcrate, Qwen3.8, 4 lanes, one
  book over the worlds extra_cap+stable) the prediction prompt gained an
  answer form, `<THEN id="…" undetermined/>`, after the checker spent
  28.1 min / 198,977 reasoning chars on one ablation guessing values no
  prompt states. `--reask-ablation` asks each chunk's ablation again with
  the current builder, so that answer form can show on recorded drafts.
  The loader files every prediction by the example ids it asks (four
  lanes interleave authors and checks), reads the set judge's `RULES`
  header, ignores the rule-probe predictions made after the book event,
  and rebuilds a multi-world book from its per-world stores.

USAGE
  .venv/bin/python scripts/book_judge_replay.py [--run 72627-751534fe]
      [--cluster stable|nightly|both] [--lanes 4] [--judge-temp T]
      [--no-agreement] [--dry] [--private a,b] [--reask-ablation] [--out DIR]

  Inputs: `logs/events.jsonl` (the run's llm.request/llm.response and
  `task0d.behaviour_book` rows) and `logs/prompts/<sha>.txt` (prompts and
  replies), and the run's suite stores
  `tmp/<crate>/.skeleton/_behaviours_<cluster>.json` (the examples and the
  glossary the prompts were built from; the script says so when the
  store's mtime is not the book event's second).
  Calls are grouped into chunks the way the per-cluster build made them:
  one author call, its ablation, then its per-draft checks; the clusters
  are split at their `task0d.behaviour_book` events. A book over several
  worlds (event cluster `a+b`) is read from `_behaviours_a.json` and
  `_behaviours_b.json` and unioned as the build unions it.
  --cluster      a book's cluster as its event names it (`stable`,
                 `extra_cap+stable`), or `both` (default) for every book.
  --reask-ablation  ask every chunk's ablation again with the current
                 builder (one call per chunk) and judge with that reply;
                 the old judge and the recorded-set control keep the
                 recorded one. Without it the recorded ablation is reused.
  --dry          no model call: prints the old judge's reproduction only
                 (the negative control) and the chunk inventory.
  --judge-temp   temperature of the set / leave-one-out calls (default: the
                 server's, i.e. none sent).
  --no-agreement skip the 2x server-default + 2x temperature-0 repeats of
                 each set call (4 extra calls per judged chunk).
  --feature F    only the chunks of feature F (e.g. `ensure!`); chunk numbers
                 in reply file names stay the cluster's build order.
  --show-wrong N print up to N (expected, predicted) pairs per chunk that
                 the SET call got wrong. Every live call's prompt and raw
                 reply are saved under <out>/replies/<timestamp>/ as
                 <cluster>_<chunk>_<call>.{prompt,reply}.txt (call 01 is the
                 set call), and chunks.jsonl carries per call the drafts
                 left out and every wrong (expected, predicted) pair.
  --sentence-cap N  negative control: also refuse a draft longer than N
                 chars, the length bound `draft_refusal` used as "one
                 sentence" until 2026-10-01 (N was 400). On run
                 `72627-751534fe` it refused five single sentences of
                 413-471 chars, four of them one half of the ensure! base +
                 suffix pair; the pipeline now counts sentence ends instead
                 (`sentence_breaks`). With --dry the `refused` column under
                 400 against the column without it is that fixture.
  --no-siblings  skip the second pass. By default, after the per-chunk
                 judgments, each feature's kept sentences are judged on every
                 chunk's still-uncovered examples when that chunk's set never
                 carried one of them (`sibling_visits`, what the build does;
                 1 set call + at most one leave-one-out call per kept
                 sentence, per visited chunk).
  --private      names `draft_refusal` refuses; default none. Run
                 `72627-751534fe` refused no draft for naming a private
                 item, so the default reproduces its histogram.
  Preconditions and cost: vLLM on :8000 serving MODEL (the calls go
  through `model_interact.ask`, the pipeline's own transport, with the
  run's recorded system prompt). Per judged chunk: 1 set call + at most
  |S| leave-one-out calls, + 4 agreement calls; the recorded ablation is
  reused unless --reask-ablation (+1 call per chunk). About 16 chunks a cluster; with 4 lanes a
  cluster is ~20-60 min on the 30B model. It may run beside a pipeline
  only if the GPU has the lanes to spare; it changes no pipeline state.

HOW TO INTERPRET THE OUTPUT
  `store: ...` lines name the store each cluster's examples came from and
  whether its mtime matches the book event (a mismatch means the store was
  rewritten after the run: the ids may no longer resolve).
  `unfiled prediction <ts> <sha>: <why>` is a prediction call inside a
  book's window the loader could not give to a chunk (none expected).
  `chunk <cluster> <feature>: N example(s), ablation recorded (R reasoning
  chars, M min), K with-rules check(s)` is what the loader filed per chunk;
  `ablation NONE` means the chunk has no recorded ablation.
  `ablation prompt reproduced X/N (Y before the undetermined slot)`: Y == N
  for a run recorded before the slot existed, X == N after; less means the
  stores no longer describe the run.
  `old judge reproduced: kept K, covered C/197 (logged K', C')` is the
  negative control for a run judged by the per-draft judge (before
  2026-09-30): K == K' and C == C' or the replay is not the run.
  `set judge on the recorded replies: kept K, covered C (logged K', C'; U
  call(s) the run did not record)` is the negative control for a run judged
  by the set judge: K == K', C == C' and U == 0, or the replay is not the run.
  Per chunk, `ablation recorded R reasoning chars M min {grades}` and, with
  --reask-ablation, `ablation re-asked R' … {grades'}` plus one line per
  example the re-asked ablation marked undetermined. Grades are right /
  wrong / undetermined / unanswered (`behaviour_book.predicted`). The
  summary lines `ablation recorded:` / `ablation re-asked:` total the
  minutes, the largest reasoning length and the grades. On-theory for the
  undetermined slot: the re-asked ablation's undetermined examples are ones
  no glossary line or rule states (constants, private computations), its
  max reasoning is far below the recorded one, and `new judge` covered is
  no lower than the recorded-set line. Off-theory: determinable examples
  come back undetermined (right falls, covered drops) — the slot's wording
  is the defect.
  Per feature: `drafts` (after the per-reply cap), `refused` (by
  draft_refusal), `old` kept by the per-draft judge, `new` kept by the set
  judge, `credited` examples, `refuted` examples recorded on kept rules,
  `calls` live calls (set + leave-one-out), `bound` = |S| + 1.
  Then each kept rule of the largest feature (sentence, credited count),
  every draft the new judge keeps that the old one dropped (read these:
  the two legitimate drops of that run, the Context Debug sentence without
  the blank line and the vague alternate-Debug sentence, must NOT appear),
  and the new drop histogram.
  `sibling visit F chunk N: S sentence(s) over U uncovered example(s),
  credited C, calls K` is one visited chunk; `sibling chunks: V visit(s),
  +E example(s), covered X/197` is what the pass added over `new judge`.
  On run `72627-751534fe` the chunks to read are the ensure! chunks whose
  own drafts were wrong (a base sentence without the backticks, one
  without the opening parenthesis): credited 0 there with the pair kept in
  a sibling chunk is off-theory. --no-siblings is the negative control.
  `agreement server a/b (p%)  temp0 c/d (q%)`: over the set calls' examples,
  how often two identical calls give the same verdict (right / wrong /
  silent). Decision rule for BEHAVIOUR_BOOK_PREDICT_TEMPERATURE: set 0
  when temp0 agrees clearly more than the server default (a sampled judge
  gives two verdicts on one sentence); leave unset when they are equal.
  CLAIMS: each line ends PASS or FAIL.
    largest-feature-kept   the largest feature keeps >= 2 rules with
                           credited > 0 (the base + suffix pair)
    no-empty-credit        no kept rule has an empty credited set
    calls-bound            live calls per chunk <= |S| + 1
  Relaunch (pre_run.sh, then redo.sh --full --force) only when all PASS.
  If the largest feature still keeps nothing because the predictor drops a
  character from every value, that is the finding: the book's checkable-
  example definition, not the judge, is next.
  Raw per-chunk records: <out>/chunks.jsonl.
"""
from __future__ import annotations

import argparse
import functools
import json
import re
import sys
import time
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from pipeline_core.domain.behaviour import (  # noqa: E402
    BehaviourSpec,
    BehaviourSuite,
    then_value_text,
)
from pipeline_core.domain.behaviour_book import (  # noqa: E402
    ChunkJudgment,
    CreditedRule,
    Grade,
    Prediction,
    RuleDraft,
    draft_refusal,
    drop_reason_class,
    judge_rule_set,
    judge_visit,
    parse_predictions,
    parse_rule_drafts,
    predicted,
    sibling_visits,
)
from pipeline_core.domain.reasoning_cap import observe_call  # noqa: E402
from pipeline_core.domain.world_book import union_glossary, union_worlds  # noqa: E402
from pipeline_core.prompts.behaviour_rules import (  # noqa: E402
    UNDETERMINED_SLOT,
    build_then_prediction_prompt,
)

DRAFTS_PER_REPLY = 6
# The per-draft judge's header (until 2026-09-30): one sentence.
_OLD_RULE = re.compile(r"\nRULE \(holds for every example below; rely on it\):\n(.*?)\n\n", re.S)
# The set judge's header: the sentences numbered, all at once.
_RULES = re.compile(r"\nRULES \(hold together for the examples below; rely on them\):\n(.*?)\n\n",
                    re.S)
_NUMBERED = re.compile(r"^\d+\. (.*)$", re.M)
_IDS = re.compile(r'<EXAMPLE id="([^"]+)">')


@dataclass
class Chunk:
    cluster: str
    feature: str
    crate: str
    author_reply: str
    ids: list[str]
    system: str | None
    ablation_prompt: str = ""
    ablation_reply: str = ""
    # The recorded ablation call's reasoning length and minutes
    # (`llm.stream_meta` / `llm.response` of its prompt sha), None when the
    # run recorded none.
    ablation_reasoning: int | None = None
    ablation_minutes: float | None = None
    # `rules carried, in order -> recorded reply`. A per-draft check (the
    # old judge) is a one-sentence key.
    checks: dict[tuple[str, ...], str] = field(default_factory=dict)
    index: int = 0  # the chunk's position in its cluster's build
    # The ablation re-asked with the current builder (`--reask-ablation`).
    fresh_ablation_reply: str | None = None


@dataclass
class Loaded:
    """What `load_chunks` read: the chunks, the book events by cluster, and
    the prediction requests it could not file (`(ts, sha, why)`)."""

    chunks: list[Chunk]
    books: dict[str, dict[str, object]]
    unfiled: list[tuple[str, str, str]] = field(default_factory=list)


def _read(sha: str | None, prompts: Path | None = None) -> str:
    if not sha:
        return ""
    p = (prompts or REPO / "logs" / "prompts") / f"{sha}.txt"
    return p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""


def rules_of(text: str) -> tuple[str, ...]:
    """The sentences a prediction prompt carries, in order: the set
    judge's numbered `RULES` block, or the per-draft judge's one `RULE`.
    Empty for an ablation."""
    m = _RULES.search(text)
    if m:
        return tuple(" ".join(x.split()) for x in _NUMBERED.findall(m.group(1)))
    m = _OLD_RULE.search(text)
    return (" ".join(m.group(1).split()),) if m else ()


def load_chunks(run: str, *, events: Path | None = None,
                prompts: Path | None = None) -> Loaded:
    """The run's book chunks: each author call, and the prediction calls
    filed under the chunk whose examples they ask.

    A prediction is filed by its example ids, never by its position: with
    several lanes the author calls of different chunks interleave with
    each other's checks (run `3198683-b5e39b06`, 2026-11-06, 4 lanes), so
    "the latest author" files a check on the wrong chunk. It goes to the
    latest author before it whose examples contain every id it asks; an
    ablation is the no-rule call over exactly that chunk's examples. A
    request after the last book event of its run is not a book call (the
    rule probes ask the same prediction prompt over new captures after the
    book is written) and is not filed."""
    reqs: list[tuple[str, str, str | None]] = []
    replies: dict[str, str] = {}
    reasoning: dict[str, int] = {}
    minutes: dict[str, float] = {}
    books: list[tuple[str, str, dict[str, object]]] = []
    with (events or REPO / "logs" / "events.jsonl").open(encoding="utf-8") as f:
        for line in f:
            if f'"{run}"' not in line:
                continue
            e = json.loads(line)
            d = e.get("data") or {}
            if e["event"] == "llm.request":
                reqs.append((e["ts"], d["prompt_sha"], (d.get("system") or {}).get("sha256")))
            elif e["event"] == "llm.response":
                replies[d["prompt_sha"]] = d["response"]["sha256"]
                if e.get("duration_ms") is not None:
                    minutes[d["prompt_sha"]] = float(e["duration_ms"]) / 60000
            elif e["event"] == "llm.stream_meta" and not d.get("followup"):
                reasoning[d["prompt_sha"]] = int(d.get("reasoning_chars") or 0)
            elif e["event"] == "task0d.behaviour_book":
                books.append((e["ts"], str(e.get("cluster") or d.get("cluster")),
                              {**d, **{k: v for k, v in e.items() if k not in ("data",)}}))
    books.sort()
    out = Loaded([], {c: r for _ts, c, r in books})
    for ts, sha, system in sorted(reqs):
        cluster = next((c for bts, c, _ in books if ts <= bts), None)
        if cluster is None:
            continue  # after the last book: a rule probe, not the book
        text = _read(sha, prompts)
        if text.startswith("You are writing the specification"):
            feature = re.search(r"FEATURE \(the public item these examples exercise\):\n- `([^`]+)`", text)
            crate = re.search(r"Rust crate `([^`]+)`", text)
            out.chunks.append(Chunk(cluster, feature.group(1) if feature else "?",
                                    crate.group(1) if crate else "",
                                    _read(replies.get(sha), prompts), _IDS.findall(text), system))
            continue
        if not text.startswith("Predict what"):
            continue
        asked = _IDS.findall(text)
        rules = rules_of(text)
        owner = next((c for c in reversed(out.chunks)
                      if c.cluster == cluster and asked and set(asked) <= set(c.ids)), None)
        if owner is None:
            out.unfiled.append((ts, sha, "no author of this book asked these examples"))
            continue
        reply = _read(replies.get(sha), prompts)
        if rules:
            owner.checks.setdefault(rules, reply)
        elif set(asked) == set(owner.ids) and not owner.ablation_prompt:
            owner.ablation_prompt, owner.ablation_reply = text, reply
            owner.ablation_reasoning = reasoning.get(sha)
            owner.ablation_minutes = minutes.get(sha)
        else:
            out.unfiled.append((ts, sha, "a no-rule call that is not its chunk's ablation"))
    return out


def load_store(crate: str, cluster: str, book_ts: str) -> tuple[BehaviourSuite, dict[str, tuple[str, ...]] | None]:
    """The examples and glossary a book was built over, and the prompt's
    world marks (None for a one-world book).

    A book over several worlds is logged under `a+b` and stored per world
    (`_behaviours_a.json`, `_behaviours_b.json`, each the book projected
    into it). The union is rebuilt from those stores the way the build
    made it (`union_worlds`, `union_glossary`), so ids and the `BUILT ONLY
    IN` lines match the prompts that ran; `ablation prompt reproduced`
    says whether they do."""
    worlds = [w for w in cluster.split("+") if w]
    event = datetime.fromisoformat(book_ts) if book_ts else None
    stores: dict[str, BehaviourSuite] = {}
    for w in worlds:
        path = REPO / "tmp" / crate / ".skeleton" / f"_behaviours_{w}.json"
        stores[w] = BehaviourSuite.from_dict(json.loads(path.read_text(encoding="utf-8")))
        stamp = datetime.fromtimestamp(path.stat().st_mtime).astimezone()
        same = event is not None and abs((stamp - event).total_seconds()) < 2
        print(f"store: {path.relative_to(REPO)} mtime {stamp.isoformat(timespec='seconds')} "
              f"{'= the book event' if same else '!= the book event ' + str(book_ts)}")
    if len(stores) == 1:
        return stores[worlds[0]], None
    union = union_worlds(stores)
    glossary = union_glossary({w: s.glossary for w, s in stores.items()})
    suite = BehaviourSuite(cfg_cluster=union.suite.cfg_cluster, specs=union.suite.specs,
                           helpers=union.suite.helpers, glossary=glossary)
    return suite, union.marks()


def old_verdict(d: RuleDraft, pool: Mapping[str, BehaviourSpec],
                with_rule: Mapping[str, Prediction], ablation: Mapping[str, Prediction]) -> str:
    """The per-draft, all-or-nothing judge that ran live until 2026-09-30
    (negative control). It read a cited id the check did not answer as a
    wrong answer; `is not Grade.RIGHT` keeps that mapping."""
    cited = [pool[i] for i in d.covers if i in pool]
    if any(predicted(with_rule, s.behaviour_id, s) is not Grade.RIGHT for s in cited):
        return "does not predict"
    if any(s.behaviour_id not in ablation for s in cited):
        return "ablation did not answer"
    if not any(predicted(ablation, s.behaviour_id, s).not_predicted for s in cited):
        return "adds nothing"
    return "kept"


def _verdicts(reply: Mapping[str, Prediction], pool: Mapping[str, BehaviourSpec]) -> dict[str, str]:
    return {i: predicted(reply, i, s).value for i, s in pool.items()}


def _grades(reply: Mapping[str, Prediction], pool: Mapping[str, BehaviourSpec]) -> dict[str, int]:
    return dict(Counter(predicted(reply, i, s).value for i, s in pool.items()))


def _observed_ask(ask, prompt: str, **kw: Any) -> tuple[str, int | None, float]:
    """One live call, with the reasoning length the transport recorded
    for it (`observe_call`) and its minutes."""
    t0 = time.monotonic()
    with observe_call() as rows:
        raw = ask(prompt, **kw)
    main = [r for r in rows if r.get("kind") != "followup"]
    chars = int(main[-1].get("reasoning_chars") or 0) if main else None
    return raw, chars, (time.monotonic() - t0) / 60


def _expected(spec: BehaviourSpec) -> str:
    assert spec.then is not None
    text = then_value_text(spec.then)
    return f"{text} [prefix]" if spec.then.prefix else text


def judge_chunk(c: Chunk, suite: BehaviourSuite, *, private: set[str], ask, temp: float | None,
                agreement: bool, replies_dir: Path | None = None,
                length_bound: int = 0, worlds: Mapping[str, Sequence[str]] | None = None,
                reask_ablation: bool = False) -> dict[str, Any]:
    by_id = {s.behaviour_id: s for s in suite.specs}
    pool = {i: by_id[i] for i in c.ids if i in by_id}
    drafts = parse_rule_drafts(c.author_reply)
    over = max(0, len(drafts) - DRAFTS_PER_REPLY)
    drafts = drafts[:DRAFTS_PER_REPLY]
    glossary = suite.glossary
    rebuilt = build_then_prediction_prompt(crate=c.crate, glossary=glossary,
                                           examples=list(pool.values()), worlds=worlds)
    recorded = parse_predictions(c.ablation_reply)
    ablation = recorded
    fresh: dict[str, Any] = {}
    if reask_ablation and ask is not None and pool:
        # The recorded ablation was asked by the builder of its day; an
        # answer form the builder offers now (the undetermined tag) can only
        # show in a re-asked one. The old judge keeps the recorded reply.
        raw, chars, mins = _observed_ask(ask, rebuilt, system=_read(c.system) or None,
                                         temperature=temp)
        c.fresh_ablation_reply = raw
        ablation = parse_predictions(raw)
        fresh = {"reasoning_chars": chars, "minutes": round(mins, 1),
                 "grades": _grades(ablation, pool),
                 "undetermined": [i for i in pool
                                  if predicted(ablation, i, pool[i]) is Grade.UNDETERMINED]}
        if replies_dir is not None:
            name = f"{c.cluster}_{c.index:02d}_00_ablation"
            (replies_dir / f"{name}.prompt.txt").write_text(rebuilt, encoding="utf-8")
            (replies_dir / f"{name}.reply.txt").write_text(raw, encoding="utf-8")
    old: Counter[str] = Counter()
    old_kept: list[RuleDraft] = []
    surviving: list[RuleDraft] = []
    refused: Counter[str] = Counter()
    for d in drafts:
        why = draft_refusal(d, pool, private)
        if why is None and length_bound and len(d.sentence) > length_bound:
            why = "longer than the length bound"
        if why is not None:
            refused[drop_reason_class(why)] += 1
            old[drop_reason_class(why)] += 1
            continue
        surviving.append(d)
        v = old_verdict(d, pool, parse_predictions(c.checks.get((d.sentence,), "")), recorded)
        old[v] += 1
        if v == "kept":
            old_kept.append(d)
    rec: dict[str, Any] = {
        "cluster": c.cluster, "feature": c.feature, "ids": list(pool), "missing_ids": len(c.ids) - len(pool),
        "drafts": len(drafts), "over_cap": over, "refused": dict(refused), "old": dict(old),
        "old_kept": [d.sentence for d in old_kept],
        "old_covered": sorted({i for d in old_kept for i in d.covers if i in pool}),
        "ablation_prompt_reproduced": rebuilt == c.ablation_prompt,
        # The same prompt but for the answer form the builder offers since
        # 2026-11-06: a run recorded before it reproduces only this way.
        "ablation_prompt_reproduced_before_slot":
            rebuilt.replace(UNDETERMINED_SLOT, "", 1) == c.ablation_prompt,
        "bound": len(surviving) + 1,
        "surviving": [d.sentence for d in surviving],
        "recorded_ablation": {"reasoning_chars": c.ablation_reasoning,
                              "minutes": None if c.ablation_minutes is None
                              else round(c.ablation_minutes, 1),
                              "grades": _grades(recorded, pool)},
        "fresh_ablation": fresh,
    }
    # The set judge on the recorded replies alone: what the run's own judge
    # decided, when the run used it (negative control of the live replay).
    # A call the run never made answers nothing and is counted.
    unrecorded = [0]

    def recorded_predict(rules: Sequence[str], ids: Sequence[str]) -> dict[str, Prediction]:
        if not rules:
            return {i: v for i, v in recorded.items() if i in ids}
        reply = c.checks.get(tuple(rules))
        if reply is None:
            unrecorded[0] += 1
            return {}
        return {i: v for i, v in parse_predictions(reply).items() if i in ids}

    on_record = judge_rule_set(surviving, pool, predict=recorded_predict) if surviving else None
    rec["recorded_set_kept"] = [] if on_record is None else [
        {"sentence": k.draft.sentence, "credited": list(k.credited)} for k in on_record.kept]
    rec["recorded_set_unrecorded_calls"] = unrecorded[0]
    if ask is None or not surviving:
        rec.update(new_kept=[], new_dropped=[], calls=0, agreement={})
        return rec
    live = [0]
    calls: list[dict[str, Any]] = []
    everyone = [d.sentence for d in surviving]

    def predict(rules: Sequence[str], ids: Sequence[str]) -> dict[str, Prediction]:
        if not rules:
            return {i: v for i, v in ablation.items() if i in ids}
        live[0] += 1
        prompt = build_then_prediction_prompt(crate=c.crate, glossary=glossary,
                                              examples=[pool[i] for i in ids], rules=rules,
                                              worlds=worlds)
        raw, chars, mins = _observed_ask(ask, prompt, system=_read(c.system) or None,
                                         temperature=temp)
        parsed = parse_predictions(raw)
        call: dict[str, Any] = {
            "left_out": [x for x in everyone if x not in rules], "ids": list(ids),
            "answered": len(parsed), "reasoning_chars": chars, "minutes": round(mins, 1),
            "grades": _grades(parsed, {i: pool[i] for i in ids}),
            "wrong": {
                i: {"expected": _expected(pool[i]), "predicted": parsed[i]}
                for i in ids if predicted(parsed, i, pool[i]) is Grade.WRONG}}
        if replies_dir is not None:
            name = f"{c.cluster}_{c.index:02d}_{live[0]:02d}"
            (replies_dir / f"{name}.prompt.txt").write_text(prompt, encoding="utf-8")
            (replies_dir / f"{name}.reply.txt").write_text(raw, encoding="utf-8")
            call["files"] = name
        calls.append(call)
        return parsed

    verdict = judge_rule_set(surviving, pool, predict=predict)
    rec.update(
        new_kept=[{"sentence": k.draft.sentence, "credited": list(k.credited),
                   "refuted_on": list(k.refuted_on)} for k in verdict.kept],
        new_dropped=list(verdict.dropped), calls=live[0], call_log=calls,
        ablation_wrong={i: {"expected": _expected(pool[i]), "predicted": ablation[i]}
                        for i in pool if predicted(ablation, i, pool[i]) is Grade.WRONG})
    if agreement:
        set_prompt = build_then_prediction_prompt(crate=c.crate, glossary=glossary,
                                                  examples=list(pool.values()),
                                                  rules=[d.sentence for d in surviving],
                                                  worlds=worlds)
        agree: dict[str, list[int]] = {}
        for label, t in (("server", None), ("temp0", 0.0)):
            a, b = (_verdicts(parse_predictions(ask(set_prompt, system=_read(c.system) or None,
                                                    temperature=t)), pool) for _ in range(2))
            agree[label] = [sum(1 for i in pool if a[i] == b[i]), len(pool)]
        rec["agreement"] = agree
    return rec


def judge_siblings(mine: Sequence[Chunk], recs: Sequence[dict[str, Any]], suite: BehaviourSuite, *,
                   ask, temp: float | None, lanes: int, replies_dir: Path,
                   worlds: Mapping[str, Sequence[str]] | None = None) -> Iterator[dict[str, Any]]:
    """The build's second pass, on the replayed judgments: per feature, its
    kept sentences judged on each chunk's uncovered examples
    (`sibling_visits`). The chunk's ablation is reused: the re-asked one
    under `--reask-ablation`, else the recorded one."""
    by_id = {s.behaviour_id: s for s in suite.specs}
    per: dict[str, list[int]] = {}
    for n, r in enumerate(recs):
        per.setdefault(str(r["feature"]), []).append(n)
    todo: list[tuple[str, int, Any]] = []
    for feature, idx in per.items():
        judgments = [ChunkJudgment(tuple(recs[n]["ids"]), tuple(recs[n]["surviving"]), tuple(
            CreditedRule(RuleDraft(k["sentence"], tuple(k["credited"])), tuple(k["credited"]))
            for k in recs[n]["new_kept"])) for n in idx]
        todo.extend((feature, idx[v.chunk], v) for v in sibling_visits(judgments))

    def run(task: tuple[str, int, Any]) -> dict[str, Any]:
        feature, n, visit = task
        c = mine[n]
        ablation = parse_predictions(c.fresh_ablation_reply if c.fresh_ablation_reply is not None
                                     else c.ablation_reply)
        live = [0]

        def predict(rules: Sequence[str], ids: Sequence[str]) -> dict[str, Prediction]:
            if not rules:
                return {i: v for i, v in ablation.items() if i in ids}
            live[0] += 1
            prompt = build_then_prediction_prompt(crate=c.crate, glossary=suite.glossary,
                                                  examples=[by_id[i] for i in ids], rules=rules,
                                                  worlds=worlds)
            raw, _chars, _mins = _observed_ask(ask, prompt, system=_read(c.system) or None,
                                               temperature=temp)
            name = f"{c.cluster}_{c.index:02d}_sibling_{live[0]:02d}"
            (replies_dir / f"{name}.prompt.txt").write_text(prompt, encoding="utf-8")
            (replies_dir / f"{name}.reply.txt").write_text(raw, encoding="utf-8")
            return parse_predictions(raw)

        credit, _calls = judge_visit(visit, by_id, predict=predict)
        return {"cluster": c.cluster, "feature": feature, "chunk": c.index, "sibling_visit": True,
                "sentences": len(visit.sentences), "open": len(visit.example_ids),
                "credit": {s: list(ids) for s, ids in credit.items()}, "calls": live[0]}

    with ThreadPoolExecutor(max_workers=max(1, lanes)) as pool:
        # Yielded as each visit lands, so a visit of several minutes a call
        # is not a silent wait for the slowest one.
        yield from pool.map(run, todo)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--run", default="72627-751534fe")
    ap.add_argument("--cluster", default="both")
    ap.add_argument("--lanes", type=int, default=4)
    ap.add_argument("--judge-temp", type=float, default=None)
    ap.add_argument("--no-agreement", action="store_true")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--private", default="")
    ap.add_argument("--feature", default="", help="only the chunks of this feature (e.g. ensure!)")
    ap.add_argument("--sentence-cap", type=int, default=0,
                    help="negative control: also refuse drafts longer than N chars (pre-fix: 400)")
    ap.add_argument("--show-wrong", type=int, default=0,
                    help="print up to N expected/predicted pairs the set call got wrong per chunk")
    ap.add_argument("--no-siblings", action="store_true",
                    help="skip the sibling-chunk pass (the build with BEHAVIOUR_BOOK_SIBLING_CHUNKS=0)")
    ap.add_argument("--reask-ablation", action="store_true",
                    help="ask each chunk's ablation again with the current builder")
    ap.add_argument("--out", type=Path, default=REPO / "logs" / "book_judge_replay")
    args = ap.parse_args()
    if args.sentence_cap:
        print(f"negative control: drafts longer than {args.sentence_cap} chars refused "
              f"(the pre-fix length bound was 400)")
    loaded = load_chunks(args.run)
    chunks, books = loaded.chunks, loaded.books
    if not chunks:
        print(f"no book author calls recorded for run {args.run}")
        return 2
    for ts, sha, why in loaded.unfiled:
        print(f"unfiled prediction {ts} {sha[:12]}: {why}")
    for ch in chunks:
        cost = ""
        if ch.ablation_prompt:
            cost = (f" ({ch.ablation_reasoning} reasoning chars, "
                    f"{ch.ablation_minutes if ch.ablation_minutes is None else round(ch.ablation_minutes, 1)} min)")
        print(f"chunk {ch.cluster} {ch.feature}: {len(ch.ids)} example(s), "
              f"ablation {'recorded' + cost if ch.ablation_prompt else 'NONE'}, "
              f"{len(ch.checks)} with-rules check(s)")
    clusters = [c for c in dict.fromkeys(ch.cluster for ch in chunks)
                if args.cluster in ("both", c)]
    ask = None
    if not args.dry:
        import model_interact
        ask = model_interact.ask
    private = {x for x in args.private.split(",") if x}
    args.out.mkdir(parents=True, exist_ok=True)
    replies = args.out / "replies" / time.strftime("%Y%m%dT%H%M%S")
    replies.mkdir(parents=True, exist_ok=True)
    sink = (args.out / "chunks.jsonl").open("a", encoding="utf-8")
    all_pass = True
    for cluster in clusters:
        mine = [ch for ch in chunks if ch.cluster == cluster]
        for n, ch in enumerate(mine, 1):
            ch.index = n
        mine = [ch for ch in mine if not args.feature or ch.feature == args.feature]
        if not mine:
            continue
        reading = books.get(cluster, {})
        suite, worlds = load_store(mine[0].crate, cluster, str(reading.get("ts", "")))
        t0 = time.monotonic()
        with ThreadPoolExecutor(max_workers=max(1, args.lanes)) as pool:
            recs = []
            # Written as each chunk lands: a stopped replay keeps every chunk
            # it finished (one chunk is up to ~10 calls of several minutes).
            for r in pool.map(functools.partial(
                    judge_chunk, suite=suite, private=private, ask=ask, temp=args.judge_temp,
                    agreement=not args.no_agreement, replies_dir=replies,
                    length_bound=args.sentence_cap, worlds=worlds,
                    reask_ablation=args.reask_ablation), mine):
                recs.append(r)
                sink.write(json.dumps({"run": args.run, **r}) + "\n")
                sink.flush()
                rec_a, new_a = r["recorded_ablation"], r["fresh_ablation"]
                fresh_txt = (f"; ablation re-asked {new_a['reasoning_chars']} reasoning chars "
                             f"{new_a['minutes']} min {new_a['grades']}" if new_a else "")
                print(f"  chunk {len(recs)}/{len(mine)} {r['feature']}: old {len(r['old_kept'])} "
                      f"new {len(r['new_kept'])} calls {r['calls']}; ablation recorded "
                      f"{rec_a['reasoning_chars']} reasoning chars {rec_a['minutes']} min "
                      f"{rec_a['grades']}{fresh_txt}", flush=True)
                for i in new_a.get("undetermined", []):
                    print(f"      undetermined with no rule: {i}", flush=True)
                for call in r.get("call_log", [])[:1]:   # the set call: nothing left out
                    for i, w in list(call["wrong"].items())[:args.show_wrong]:
                        print(f"      {i}\n        expected : {w['expected']!r}\n"
                              f"        predicted: {w['predicted']!r}", flush=True)
        checkable = reading.get("checkable_examples", "?")
        old_kept = sum(len(r["old_kept"]) for r in recs)
        old_cov = len({i for r in recs for i in r["old_covered"]})
        print(f"\n== {cluster}: {len(mine)} chunk(s), {sum(1 for r in recs if r['missing_ids'])} with "
              f"unresolved ids, ablation prompt reproduced {sum(1 for r in recs if r['ablation_prompt_reproduced'])}"
              f"/{len(recs)} ({sum(1 for r in recs if r['ablation_prompt_reproduced_before_slot'])} "
              f"before the undetermined slot), {time.monotonic() - t0:.0f}s")
        print(f"old judge reproduced: kept {old_kept}, covered {old_cov}/{checkable} "
              f"(logged {reading.get('kept')}, {reading.get('covered_examples')})")
        rec_kept = sum(len(r["recorded_set_kept"]) for r in recs)
        rec_cov = len({i for r in recs for k in r["recorded_set_kept"] for i in k["credited"]})
        print(f"set judge on the recorded replies: kept {rec_kept}, covered {rec_cov}/{checkable} "
              f"(logged {reading.get('kept')}, {reading.get('covered_examples')}; "
              f"{sum(r['recorded_set_unrecorded_calls'] for r in recs)} call(s) the run did not record)")
        rec_abl = [r["recorded_ablation"] for r in recs]
        print(f"ablation recorded: {sum(a['minutes'] or 0 for a in rec_abl):.1f} min, max "
              f"{max((a['reasoning_chars'] or 0 for a in rec_abl), default=0)} reasoning chars, "
              f"grades {dict(sum((Counter(a['grades']) for a in rec_abl), Counter()))}")
        new_abl = [r["fresh_ablation"] for r in recs if r["fresh_ablation"]]
        if new_abl:
            print(f"ablation re-asked: {sum(a['minutes'] for a in new_abl):.1f} min, max "
                  f"{max((a['reasoning_chars'] or 0 for a in new_abl), default=0)} reasoning chars, "
                  f"grades {dict(sum((Counter(a['grades']) for a in new_abl), Counter()))}")
        per: dict[str, dict[str, int]] = {}
        for r in recs:
            row = per.setdefault(str(r["feature"]), Counter())
            row["checkable"] += len(r["ids"])
            row["drafts"] += int(r["drafts"])
            row["refused"] += sum(r["refused"].values())
            row["old"] += len(r["old_kept"])
            row["new"] += len(r["new_kept"])
            row["credited"] += len({i for k in r["new_kept"] for i in k["credited"]})
            row["refuted"] += len({i for k in r["new_kept"] for i in k["refuted_on"]})
            row["calls"] += int(r["calls"])
            row["bound"] += int(r["bound"])
        for feat, row in sorted(per.items(), key=lambda kv: -kv[1]["checkable"]):
            print(f"  {feat:<28} checkable={row['checkable']:>3} drafts={row['drafts']:>2} "
                  f"refused={row['refused']} old={row['old']} new={row['new']} "
                  f"credited={row['credited']:>3} refuted={row['refuted']:>2} "
                  f"calls={row['calls']:>2} bound={row['bound']:>2}")
        if args.dry:
            continue
        new_cov = {i for r in recs for k in r["new_kept"] for i in k["credited"]}
        print(f"new judge: kept {sum(len(r['new_kept']) for r in recs)}, covered {len(new_cov)}/{checkable}")
        if not args.no_siblings:
            print("sibling pass (one line per visited chunk as it lands):", flush=True)
            visits: list[dict[str, Any]] = []
            gained: set[str] = set()
            for v in judge_siblings(mine, recs, suite, ask=ask, temp=args.judge_temp,
                                    lanes=args.lanes, replies_dir=replies, worlds=worlds):
                visits.append(v)
                sink.write(json.dumps({"run": args.run, **v}) + "\n")
                got = {i for ids in v["credit"].values() for i in ids}
                gained |= got
                print(f"  sibling visit {v['feature']} chunk {v['chunk']}: {v['sentences']} sentence(s) "
                      f"over {v['open']} uncovered example(s), credited {len(got)}, calls {v['calls']}",
                      flush=True)
            sink.flush()
            print(f"sibling chunks: {len(visits)} visit(s), +{len(gained - new_cov)} example(s), "
                  f"covered {len(new_cov | gained)}/{checkable}, "
                  f"{sum(v['calls'] for v in visits)} call(s)")
        largest = max(per, key=lambda f: per[f]["checkable"])
        print(f"largest feature {largest}:")
        for r in recs:
            if r["feature"] == largest:
                for k in r["new_kept"]:
                    print(f"    kept credited={len(k['credited'])} refuted={len(k['refuted_on'])}: "
                          f"{k['sentence']}")
        print("newly kept (old judge dropped):")
        for r in recs:
            for k in r["new_kept"]:
                if k["sentence"] not in r["old_kept"]:
                    print(f"    [{r['feature']}] credited={len(k['credited'])}: {k['sentence']}")
        hist = Counter(drop_reason_class(x) for r in recs for x in r["new_dropped"])
        hist.update({k: v for r in recs for k, v in r["refused"].items()})
        print("new drop histogram: " + (", ".join(f"{k} {v}" for k, v in sorted(hist.items())) or "none"))
        agree = {lab: [sum(r["agreement"][lab][j] for r in recs if r.get("agreement")) for j in (0, 1)]
                 for lab in ("server", "temp0")}
        if agree["server"][1]:
            print("agreement " + "  ".join(
                f"{lab} {a}/{b} ({100 * a / max(1, b):.0f}%)" for lab, (a, b) in agree.items()))
        largest_kept = sum(1 for r in recs if r["feature"] == largest
                           for k in r["new_kept"] if k["credited"])
        claims = {
            "largest-feature-kept": largest_kept >= 2,
            "no-empty-credit": all(k["credited"] for r in recs for k in r["new_kept"]),
            "calls-bound": all(int(r["calls"]) <= int(r["bound"]) for r in recs),
        }
        for name, ok in claims.items():
            print(f"CLAIM {name}: {'PASS' if ok else 'FAIL'}")
            all_pass &= ok
    sink.close()
    print(f"\nraw: {args.out / 'chunks.jsonl'}")
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
