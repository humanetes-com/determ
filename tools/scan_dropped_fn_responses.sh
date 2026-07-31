#!/usr/bin/env bash
# Responses that CONTAIN an `<FN` tag but from which the production
# extractor recovers NOTHING — i.e. an answer the model paid for and the
# pipeline reported as "no FN blocks". Classifies each drop by cause.
set -u
SINCE="${1:-2026-07-31 00:00}"
DIR="${2:-logs/prompts}"
.venv/bin/python - "$SINCE" "$DIR" <<'PY'
import sys, os, datetime
sys.path.insert(0, os.getcwd())
from pipeline_core.domain.llm_output import extract_fn_bodies

since = datetime.datetime.fromisoformat(sys.argv[1]).timestamp()
d = sys.argv[2]
drops, ok, scanned = [], 0, 0
for name in os.listdir(d):
    p = os.path.join(d, name)
    try:
        if os.path.getmtime(p) < since:
            continue
        txt = open(p, encoding='utf-8', errors='replace').read()
    except OSError:
        continue
    if "<FN" not in txt:
        continue
    scanned += 1
    if extract_fn_bodies(txt):
        ok += 1
    else:
        head = txt[txt.find("<FN"):][:160].replace("\n", "\\n")
        drops.append((os.path.getmtime(p), name, len(txt), head))

print(f"responses containing '<FN': {scanned}   recovered: {ok}   DROPPED: {len(drops)}")
for mt, name, size, head in sorted(drops):
    ts = datetime.datetime.fromtimestamp(mt).strftime("%H:%M")
    print(f"  {ts} {name[:12]} {size:6d}B  {head}")
PY
