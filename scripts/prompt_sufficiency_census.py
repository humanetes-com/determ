#!/usr/bin/env python3
"""Does a fill prompt lack what the model needs? Three readings from the record.

WHY THIS EXISTS
  "The prompt lacks something the model needs" was the working explanation
  for fix loops that stop on a repeated compile error, and for proposing to
  turn the single-shot fill routes into multi-turn transcripts. On
  2026-09-24 the record said otherwise, and this script is how it was read:
  since 2026-09-01, of 524 compile errors quoted to a retry that name a
  crate item, 2 named an item that exists in the crate and was absent from
  the prompt; 143 named a function that does not exist (mostly fn-pointer
  table slots written as if a fn of the slot's name existed) and 81 a trait
  impl the crate does not have — 13 of those on 2026-09-22/23, with the
  crate's COMPLETE trait-impl list in the prompt and the thinking stating the
  impl is absent (reply 56f3d2ea…, line 36) before re-emitting the call.
  Meanwhile the model asked for context mid-call 690 times, and half of the
  per-fn asks were for a file or type the prompt already rendered in full.
  Only 15 of 2,249 attempts repeated the previous attempt's error set once
  request round-trips are folded into their attempt; the ~940 "identical
  errors" stops in the logs are mostly July/August history.

USAGE
  python scripts/prompt_sufficiency_census.py [--since YYYY-MM-DD]

  Reads logs/events.jsonl (fill start/end, llm.request/response,
  pull.answer), logs/prompts/<sha>.txt (prompts AND replies by sha) and the
  current skeleton trees under tmp/anyhow/.skeleton*/ (to tell "exists in
  the crate" from "invented"). Offline, no GPU, ~5 s. Fills are joined to
  their calls by (run_id, thread id), so a thread that interleaves two fills
  would misattribute; the fill routes do not.

HOW TO INTERPRET THE OUTPUT
  [1] requests — the model's own statement that the prompt lacks something.
      `fills with >=1 request` per route is the share of fills whose first
      reply was a request instead of a body. Each request is then classed:
        own-file/ALREADY IN PROMPT (whole), other-file/ALREADY IN PROMPT
        (whole), type_def/shown
                         the prompt rendered it; the model did not find it.
                         Without "(whole)" the prompt predates the view
                         declaration and the rendering may have been a part.
        .../part in prompt  a declared part (API slice, condensed session
                         file, signatures): asking for the file is fair.
                         A present-but-not-found problem (prompt size,
                         ordering, no index) — adding content cannot fix it.
        .../not in prompt, type_def/absent
                         a real gap in THAT builder; the route column names
                         the builder that omits it.
      Decision rule: a builder whose not-in-prompt share is high omits a
      section its units keep needing — add it to that builder. A route whose
      ALREADY share is high needs findability (an index, a pointer answer),
      not more text.
  [2] quoted compile errors on retries, by what the error names:
        IN prompt            the named item was rendered; the model misused it.
        ABSENT (exists...)   the crate has it, the prompt did not: a context gap.
        INVENTED             no such fn/type/field in the crate: the model
                             wrote a name that should exist. Look at WHICH
                             names — a family (table slots, consts) is a
                             missing negative fact ("these are the only
                             candidates"), or a write the route cannot express.
        NO SUCH IMPL         the crate has no impl of that trait for that
                             type: either the model did not know (check the
                             prompt for the trait-impl list) or knew and had
                             no other body it was allowed to write.
        n/a                  errors naming no item (E0308, E0133, ...):
                             reasoning, not context; not decidable here.
  [3] attempts — request round-trips folded into the attempt they belong to.
      `repeat` = an attempt whose quoted errors equal the previous attempt's.
      This is the population a cross-attempt transcript could shorten; if it
      is small, converting the fix loop to a transcript is not the lever.
"""
from __future__ import annotations

import argparse
import collections
import glob
import json
import os
import re

PR = "logs/prompts"
START = {"phase_c.fn.fill.start": "fn", "phase_c.impl.fill.start": "impl",
         "phase_c.file.fill.start": "file", "phase_c.session.start": "session"}
END = {"phase_c.fn.fill.done", "phase_c.fn.fill.fail", "phase_c.impl.fill.done",
       "phase_c.impl.fill.fail", "phase_c.file.fill.done",
       "phase_c.file.fill.revert", "phase_c.session.end"}
ERR_TAG = re.compile(r"<(LAST_ERROR|COMPILE_ERRORS)>(.*?)</\1>", re.S)
ERR_LINE = re.compile(r"^error\[(E\d{4})\]: (.*)$", re.M)
FN_TAG = re.compile(r"<FN\b[^>]*>.*?</FN>|<BODY>.*?</BODY>", re.S)

_text: dict[str, str] = {}


def text(sha: str | None) -> str:
    if not sha:
        return ""
    if sha not in _text:
        try:
            with open(os.path.join(PR, sha + ".txt"), errors="replace") as f:
                _text[sha] = f.read()
        except OSError:
            _text[sha] = ""
    return _text[sha]


def load_fills(since: str) -> list[dict]:
    fills: list[dict] = []
    open_: dict[tuple, dict] = {}
    last_prompt: dict[tuple, str] = {}
    with open("logs/events.jsonl", errors="replace") as f:
        for line in f:
            if line[8:18] < since:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            ev = d["event"]
            key = (d.get("run_id"), d.get("tid"))
            data = d.get("data") or {}
            if ev in START:
                fl = {"route": START[ev], "run": key[0], "ts": d["ts"],
                      "unit": f'{data.get("file")}::{data.get("fn") or data.get("container") or ""}',
                      "prompts": [], "resp": {}, "pulls": []}
                open_[key] = fl
                fills.append(fl)
            elif ev == "llm.request":
                sha = (data.get("prompt") or {}).get("sha256")
                last_prompt[key] = sha
                if key in open_:
                    open_[key]["prompts"].append(sha)
            elif ev == "llm.response" and key in open_:
                open_[key]["resp"][data.get("prompt_sha")] = (data.get("response") or {}).get("sha256")
            elif ev == "pull.answer" and key in open_:
                open_[key]["pulls"].append((data.get("kind"), data.get("arg") or "", last_prompt.get(key)))
                if data.get("held_whole"):
                    open_[key]["held_whole"] = open_[key].get("held_whole", 0) + 1
            elif ev in END and key in open_:
                del open_[key]
    return fills


def crate_text() -> str:
    files = [p for p in glob.glob("tmp/anyhow/.skeleton/*/src/**/*.rs", recursive=True)
             + glob.glob("tmp/anyhow/.skeleton-filled/*/src/**/*.rs", recursive=True)
             if ".gen_tests_" not in p]
    out = []
    for p in files:
        with open(p, errors="replace") as f:
            out.append(f.read())
    return "\n".join(out)


def defined(kind: str, name: str, hay: str) -> bool:
    n = re.escape(name.split("::")[-1].split("<")[0].strip("&* "))
    pats = {"value": rf"\b(fn|const|static|macro_rules!)\s+{n}\b",
            "type": rf"\b(struct|enum|type|trait|union|mod)\s+{n}\b",
            "method": rf"\bfn\s+{n}\b",
            "field": rf"\b{n}\s*:"}
    return bool(n) and re.search(pats[kind], hay) is not None


def impl_present(head: str, trait: str, hay: str) -> bool:
    return re.search(rf"impl\b[^\n{{;]*\b{re.escape(trait)}\b[^\n{{;]*\bfor\b[^\n{{;]*\b{re.escape(head)}\b",
                     hay) is not None


def classify(msg: str, rest: str, crate: str) -> tuple[str, str, str]:
    """(kind, named item, verdict) for one quoted rustc error."""
    for pat, kind in ((r"cannot find (?:value|function|macro|tuple struct or tuple variant) `([^`]+)`", "value"),
                      (r"cannot find (?:type|trait|struct, variant or union type) `([^`]+)`", "type"),
                      (r"failed to resolve: use of (?:undeclared|unresolved) (?:type|crate|module)[^`]*`([^`]+)`", "type"),
                      (r"no (?:method|function or associated item|variant or associated item|associated item) named `([^`]+)`", "method"),
                      (r"no field `([^`]+)`", "field"),
                      (r"has no field named `([^`]+)`", "field")):
        m = re.search(pat, msg)
        if m:
            name = m.group(1)
            if defined(kind, name, rest):
                return kind, name, "IN prompt"
            if defined(kind, name, crate):
                return kind, name, "ABSENT (exists in crate)"
            return kind, name, "INVENTED"
    m = re.search(r"the trait bound `(.+?): ([^`]+)` is not satisfied", msg)
    if m:
        head = re.sub(r"^[&*\s]*(mut\s+)?(dyn\s+)?", "", m.group(1)).split("<")[0].split("::")[-1]
        trait = m.group(2).split("<")[0].split("::")[-1]
        name = f"{head}: {trait}"
        if impl_present(head, trait, rest):
            return "impl", name, "IN prompt"
        if impl_present(head, trait, crate):
            return "impl", name, "ABSENT (exists in crate)"
        return "impl", name, "NO SUCH IMPL"
    return "other", "", "n/a"


def quoted_errors(sha: str, crate: str, cache: dict) -> tuple:
    if sha in cache:
        return cache[sha]
    t = text(sha)
    blocks = ERR_TAG.findall(t)
    if not blocks:
        cache[sha] = ()
        return ()
    rest = ERR_TAG.sub("", t)
    out = []
    for _, b in blocks:
        for code, msg in ERR_LINE.findall(b):
            kind, name, verdict = classify(msg, rest, crate)
            out.append((code, re.sub(r"\d+", "N", msg)[:160], kind, name, verdict))
    cache[sha] = tuple(out)
    return cache[sha]


def request_class(kind: str, arg: str, sha: str | None, unit: str,
                  route: str = "") -> str:
    t = text(sha)
    if not t:
        return f"{kind}/no-prompt"
    if kind == "type_def":
        shown = re.search(rf"\b(struct|enum|type|trait|union)\s+{re.escape(arg)}\b", t)
        return "type_def/shown" if shown else "type_def/absent"
    if kind == "file":
        own = "own-file" if arg == unit.split("::")[0] else "other-file"
        # The file-first builder renders its target file whole but untagged
        # (prompt 950d2610…, 2026-09-23: 61,227 chars, the file's 44 stubs
        # inline, no <FILE path=…>), so its own-file asks are for content it had.
        tags = re.findall(rf'<FILE path="[^"]*{re.escape(arg)}"([^>\n]*)', t)
        # Since 2026-09-24 every fill-prompt file declares view="whole" or
        # "part"; a part is not the file. Before that no tag said so, and the
        # session's condensed file (240 of 677 code lines, 2026-09-23) read as
        # held — so older ALREADY counts include parts.
        if any('view="whole"' in a for a in tags):
            return f"{own}/ALREADY IN PROMPT (whole)"
        if tags and all('view="part"' in a for a in tags):
            return f"{own}/part in prompt"
        here = bool(tags) or (route == "file" and own == "own-file")
        return f"{own}/{'ALREADY IN PROMPT' if here else 'not in prompt'}"
    return kind


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-09-01")
    since = ap.parse_args().since
    fills = load_fills(since)
    crate = crate_text()
    cache: dict = {}

    n = collections.Counter(f["route"] for f in fills)
    with_req = collections.Counter(f["route"] for f in fills if f["pulls"])
    req = collections.Counter()
    for f in fills:
        for kind, arg, sha in f["pulls"]:
            req[(f["route"], request_class(kind, arg, sha, f["unit"], f["route"]))] += 1
    print(f"since {since}: fills per route {dict(n)}")
    print(f"[1] fills with >=1 request {dict(with_req)}")
    held = collections.Counter()
    for f in fills:
        held[f["route"]] += f.get("held_whole", 0)
    print(f"    answered by pointer to a file held whole (2026-09-24 on): {dict(held)}")
    for k, v in sorted(req.items(), key=lambda x: (x[0][0], -x[1])):
        print(f"    {v:4d}  {k[0]:8s} {k[1]}")

    errs = collections.Counter()
    names = collections.Counter()
    for f in fills:
        for sha in f["prompts"]:
            for _code, _msg, kind, name, verdict in set(quoted_errors(sha, crate, cache)):
                errs[(kind, verdict)] += 1
                if verdict in ("INVENTED", "NO SUCH IMPL"):
                    names[(verdict, name)] += 1
    print("[2] quoted compile errors on retry prompts, by (kind, verdict):")
    for k, v in errs.most_common():
        print(f"    {v:4d}  {k}")
    print("    most frequent invented / missing-impl names:")
    for k, v in names.most_common(12):
        print(f"    {v:4d}  {k}")

    attempts_total = repeats = identical = 0
    hist = collections.Counter()
    for f in fills:
        if f["route"] == "session":
            continue
        attempts: list[list] = []            # [first prompt, final reply, last prompt]
        for sha in f["prompts"]:
            t = text(sha)
            prev = text(attempts[-1][2]) if attempts else ""
            if prev and t.startswith(prev[:5000]) and len(t) > len(prev):
                attempts[-1][1] = f["resp"].get(sha)
                attempts[-1][2] = sha
            else:
                attempts.append([sha, f["resp"].get(sha), sha])
        hist[(f["route"], min(len(attempts), 6))] += 1
        prev_sig = None
        for i, (first, _reply, _last) in enumerate(attempts):
            attempts_total += 1
            q = quoted_errors(first, crate, cache)
            if not q:
                prev_sig = None
                continue
            sig = frozenset((e[0], e[1]) for e in q)
            if sig == prev_sig:
                repeats += 1
                if i >= 2:
                    a = FN_TAG.findall(text(attempts[i - 1][1]))
                    b = FN_TAG.findall(text(attempts[i - 2][1]))
                    if a and a == b:
                        identical += 1
            prev_sig = sig
    print(f"[3] attempts {attempts_total}; repeat of previous attempt's error set {repeats} "
          f"(byte-identical bodies {identical})")
    print("    attempts per fill (route, n; 6 = 6+):", sorted(hist.items()))


if __name__ == "__main__":
    main()
