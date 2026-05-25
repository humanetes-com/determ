#!/usr/bin/env python3
"""Show recent cargo errors that the per-fn fix loop has been seeing.

Walks logs/events.jsonl backwards, finds llm.request events whose stored prompt
content includes a <COMPILE_ERRORS> block (i.e. fix-loop prompts), and prints
the error excerpt + which fn it was for.

Usage:
    .venv/bin/python tools/show_recent_errors.py            # last 5
    .venv/bin/python tools/show_recent_errors.py -n 20      # last 20
"""
import argparse
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EVENTS = ROOT / "logs" / "events.jsonl"
PROMPTS = ROOT / "logs" / "prompts"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", "--num", type=int, default=5)
    args = ap.parse_args()

    if not EVENTS.exists():
        print(f"no events log at {EVENTS}")
        return

    events = []
    with EVENTS.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except Exception:
                continue

    # We pair each fix-attempt prompt with the immediately-following
    # phase_c.fn.fill.* event to label which fn it was for.
    reqs = [e for e in events if e.get("event") == "llm.request"]
    fills = [e for e in events
             if e.get("event", "").startswith("phase_c.fn.fill")]

    found = 0
    for req in reversed(reqs):
        if found >= args.num:
            break
        sha = req.get("data", {}).get("prompt", {}).get("sha256")
        if not sha:
            continue
        path = PROMPTS / f"{sha}.txt"
        if not path.exists():
            continue
        content = path.read_text(errors="replace")
        m_err = re.search(r"<COMPILE_ERRORS>(.*?)</COMPILE_ERRORS>",
                          content, re.DOTALL)
        if not m_err:
            continue  # not a fix-loop prompt
        m_fn = re.search(r"Fn:\s*(\S+)", content)
        m_file = re.search(r"File:\s*(\S+)", content)
        m_prev = re.search(r"<PREVIOUS_BODY>(.*?)</PREVIOUS_BODY>",
                           content, re.DOTALL)
        ts = req.get("ts", "?")
        print("=" * 72)
        print(f"  {ts}")
        print(f"  fn:    {m_fn.group(1) if m_fn else '?'}")
        print(f"  file:  {m_file.group(1) if m_file else '?'}")
        print("-" * 72)
        if m_prev:
            print("PREVIOUS BODY:")
            print(m_prev.group(1).strip()[:600])
            print()
        print("CARGO ERROR:")
        print(m_err.group(1).strip())
        print()
        found += 1

    if found == 0:
        print("no fix-loop prompts found yet (every fill succeeded first try, or")
        print("the run is too young to have triggered the fix loop)")


if __name__ == "__main__":
    main()
