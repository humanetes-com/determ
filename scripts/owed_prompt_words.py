#!/usr/bin/env python3
"""Did the prompt an owed function was sent carry its red test's words?

WHY THIS EXISTS
  The owed-tiers mechanism (domain/candidate_tiers) promises that a red
  test's owed function is asked WITH the evidence that decides it: the
  failing assertion and the observed value beside the expected one (and,
  where the book has one, the kept rule aimed at the function). The driver
  hands the frontier those words (tests/test_tdd_owed_tiers.py) and the
  prompt builders print a frontier test's words (tests/test_refill_evidence.py),
  but only a live run shows the two halves meet in the prompt actually sent.
  This is that reading, over the event log and the prompt store.

USAGE
  python3 scripts/owed_prompt_words.py logs/events.jsonl RUN_ID [PROMPT_DIR]
  (PROMPT_DIR defaults to logs/prompts)

HOW TO INTERPRET
  One line per (cluster, iter, owed function, test):
    CARRIED   a prompt sent between that sweep's `phase_c.tdd.owed` and its
              `phase_c.tdd.asked` names the function and contains the test's
              words (its panic message, whitespace-folded)
    MISSING   a prompt names the function and does NOT contain the words:
              the evidence promise is broken for this ask
    UNSENT    no prompt in the window names the function (a miss: the route
              did not plan it, or the batch cut it)
    CUT       the fill did not offer the function (the batch cap took other
              units first; `phase_c.tdd.asked` lists it neither asked nor
              missed). Not judged: a prompt naming it would be another
              route's file context, not an ask
    STUB      the test is held in a still-stubbed function (the owed function
              is that stub): its words are the stub's own `not implemented`
              panic, which is no evidence about a body, so they are not
              looked for; `rule` is the evidence such an ask can carry.
              Not counted as MISSING (run 39786-fd38e72f fill 2 counted 127
              held asks MISSING before this class existed)
  `rule` says whether that prompt carried a kept-rules block. The summary
  counts each class. The claim "every owed function is asked with its test's
  words" holds on the run when MISSING is 0 and CARRIED > 0. Needs a run whose
  `phase_c.tdd.owed` events carry `words` (shipped with the owed tiers'
  review fixes, 2026-10-09).
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

_RULES_MARK = "checked against the examples it cites"
_PANIC_HEAD = re.compile(r"^thread '[^']*'( \(\d+\))? panicked at \S+")
# `test_results.world_failure_line`: since 2026-10-25 (`89610e0`) a joined
# capture's failure opens with the cfg world it failed in. Left in, the core
# below was that line plus the panic head, which no prompt quotes verbatim, so
# every row of run `2355041-eae39441` read MISSING whatever the prompt held.
_WORLD_LINE = re.compile(r"^\(failed in cfg world `[^`]*`\)\s*")


def _fold(text: str) -> str:
    return " ".join(text.split())


# A test that failed in more than one binary or world records each failure
# after this marker. The prompt renderers quote the failing source line between
# the message and the next failure, so a core that runs past the first
# failure's message is text no prompt holds (run `2355041-eae39441`: 13 of 14
# MISSING rows were the prompt quoting the failure in full).
_NEXT_FAILURE = re.compile(r"\(another test named ")


def _core(words: str) -> str:
    """The first failure's message, minus the world line and libtest's
    `thread ... panicked at <site>` head: the message and the observed
    values, the part a prompt must quote."""
    body = _PANIC_HEAD.sub("", _WORLD_LINE.sub("", _fold(words))).strip()
    return _NEXT_FAILURE.split(body, maxsplit=1)[0].strip()[:80]


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__)
        return 2
    events, run = Path(argv[1]), argv[2]
    prompts = Path(argv[3]) if len(argv) > 3 else Path("logs/prompts")
    open_: dict[tuple[str, int], dict] = {}
    window: dict[tuple[str, int], list[str]] = {}
    rows: list[tuple[str, int, str, str, str, bool]] = []
    current: tuple[str, int] | None = None
    with events.open(encoding="utf-8") as fh:
        for line in fh:
            if run not in line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("run_id") != run:
                continue
            name, data = ev.get("event"), ev.get("data") or {}
            if name == "phase_c.tdd.owed" and data.get("owed"):
                current = (str(data.get("cluster")), int(data.get("iter", 0)))
                open_[current] = data
                window[current] = []
            elif name == "llm.request" and current is not None:
                sha = data.get("prompt_sha") or (data.get("prompt") or {}).get("sha256")
                if sha:
                    window[current].append(str(sha))
            elif name == "phase_c.tdd.asked" and current is not None:
                key = (str(data.get("cluster")), int(data.get("iter", 0)))
                if key == current:
                    offered = set(data.get("asked") or ()) | set(data.get("missed") or ())
                    rows.extend(_judge(open_[key], window[key], prompts, key,
                                       offered if "missed" in data else None))
                    current = None
    counts = Counter(r[4] for r in rows)
    for cluster, it, unit, test, verdict, rule in rows:
        print(f"{cluster} iter {it}: {unit} for {test}: {verdict}"
              + (" (rule)" if rule else ""))
    print(f"summary: CARRIED {counts['CARRIED']}, MISSING {counts['MISSING']}, "
          f"UNSENT {counts['UNSENT']}, CUT {counts['CUT']}, STUB {counts['STUB']} "
          f"(with a rule: {sum(1 for r in rows if r[4] == 'STUB' and r[5])})")
    return 0


def _judge(owed: dict, shas: list[str], prompts: Path, key: tuple[str, int],
           offered: set[str] | None = None,
           ) -> list[tuple[str, int, str, str, str, bool]]:
    texts: list[str] = []
    for sha in shas:
        p = prompts / f"{sha}.txt"
        if p.exists():
            texts.append(_fold(p.read_text(encoding="utf-8", errors="replace")))
    words = owed.get("words") or {}
    out: list[tuple[str, int, str, str, str, bool]] = []
    for test, reading in sorted((owed.get("tests") or {}).items()):
        for unit in sorted(reading.get("owed") or {}):
            fn = unit.rpartition("::")[2]
            naming = [t for t in texts if re.search(rf"\bfn {re.escape(fn)}\b", t)]
            core = _core(words.get(test, ""))
            if offered is not None and unit not in offered:
                verdict, rule = "CUT", False
            elif not naming:
                verdict, rule = "UNSENT", False
            elif reading.get("held_by"):
                verdict = "STUB"
                rule = any(_RULES_MARK in t for t in naming)
            elif core and any(core in t for t in naming):
                verdict = "CARRIED"
                rule = any(_RULES_MARK in t for t in naming if core in t)
            else:
                verdict, rule = "MISSING", any(_RULES_MARK in t for t in naming)
            out.append((key[0], key[1], unit, test, verdict, rule))
    return out


if __name__ == "__main__":
    sys.exit(main(sys.argv))
