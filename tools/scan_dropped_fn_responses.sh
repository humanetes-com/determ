#!/usr/bin/env bash
# Replies that CONTAIN `<FN` tags from which the production extractor recovers
# FEWER RECORDS THAN TAGS — an answer the model was paid for and the pipeline
# reported as "no FN blocks" (or, worse, as a partial success).
#
# WHY. `N tags in with 0 records out is ALWAYS the reader, never the model` is
# a standing rule here, and six distinct reader shapes have been found since
# 2026-08-04 (the empty `block`, the `&lt;`-escaped header, the `&gt;`
# terminator, the self-closed tag, the `)`-closed generic list, the
# unterminated open tag). Each cost its own investigation because the drop was
# only ever a log line nobody aggregated. Measured 2026-08-22 over the retained
# store: 57 replies across 44 runs lost 234 bodies, one of them six complete
# bodies for `impl Error` in the run read as eight hours at a flat 92.
#
# Two gaps closed on 2026-08-22. It used to count a reply as recovered when the
# extractor returned ANYTHING, so a 7-tag reply yielding 1 record scored as a
# success — partial loss was invisible and partial loss is most of the record.
# And it had no control, so a reader change could not be measured before it
# shipped.
#
# USAGE
#   tools/scan_dropped_fn_responses.sh [SINCE] [DIR] [--blind NAME]
#     SINCE  ISO datetime, default 2026-07-31 00:00 (store mtime)
#     DIR    reply store, default logs/prompts
#     --blind NAME  also parse with `llm_output.NAME` stubbed out, and report
#                   what that recovery is worth. Use it as the NEGATIVE CONTROL
#                   for a reader change: `--blind _unterminated_open_tag_value`
#                   must show the bodies coming back, or the change recovered
#                   nothing real.
#   No GPU, no network, reads only the store on disk.
#
# HOW TO READ IT
#   "tags in / records out / LOST"  — LOST is the number of answered bodies
#       discarded. Anything but 0 on a recent run is a live parser defect and
#       is never a reason to re-dispatch the unit.
#   per-month / per-run rows        — where the loss is concentrated; a class
#       present in every run has no per-run mechanism owning it.
#   "recovered by NAME"             — with --blind, how many bodies that one
#       recovery is responsible for. This is the number a reader change has to
#       justify itself with.
#   "REGRESSED by NAME"             — replies where the recovery CHANGED or
#       LOST a record the blinded reader already produced. Must be 0. A
#       nonzero here means the change is not additive and must not ship.
set -u
SINCE="${1:-2026-07-31 00:00}"
DIR="${2:-logs/prompts}"
BLIND=""
if [ "${3:-}" = "--blind" ]; then BLIND="${4:-}"; fi
.venv/bin/python - "$SINCE" "$DIR" "$BLIND" <<'PY'
import sys, os, datetime
sys.path.insert(0, os.getcwd())
import pipeline_core.domain.llm_output as L

since = datetime.datetime.fromisoformat(sys.argv[1]).timestamp()
d, blind_name = sys.argv[2], sys.argv[3]

# Prompt artefacts share the store with replies and carry the tag TEMPLATE, so
# they would every one of them read as a total loss. Excluded by the markers
# only a prompt has.
PROMPT_MARKS = ("FUNCTION_NAME", "You are ", "Return ONLY", "<TASK",
                "OUTPUT FORMAT", "Output format", "RULES", "CONTRACT")

real = getattr(L, blind_name) if blind_name else None
if blind_name and real is None:
    print(f"no such recovery: llm_output.{blind_name}"); raise SystemExit(2)

rows, scanned, regressed = [], 0, []
for name in sorted(os.listdir(d)):
    p = os.path.join(d, name)
    try:
        mt = os.path.getmtime(p)
        if mt < since:
            continue
        txt = open(p, encoding="utf-8", errors="replace").read()
    except OSError:
        continue
    tags = txt.count("<FN ")
    if tags == 0 or len(txt) > 20000 or any(m in txt for m in PROMPT_MARKS):
        continue
    scanned += 1
    recs = L.extract_fn_bodies(txt)
    before = None
    if blind_name:
        setattr(L, blind_name, lambda *a, **k: None)
        before = L.extract_fn_bodies(txt)
        setattr(L, blind_name, real)
        old = {(r["fn_name"], r["body"]) for r in before}
        if not old <= {(r["fn_name"], r["body"]) for r in recs}:
            regressed.append(name[:12])
    if len(recs) < tags or (before is not None and len(recs) > len(before)):
        rows.append((mt, name, tags, len(recs),
                     len(before) if before is not None else None))

lost = sum(t - g for _, _, t, g, _ in rows if t > g)
print(f"replies scanned: {scanned}   with a shortfall: "
      f"{sum(1 for r in rows if r[2] > r[3])}   BODIES LOST: {lost}")
if blind_name:
    gained = sum(g - b for _, _, _, g, b in rows if b is not None and g > b)
    print(f"recovered by {blind_name}: {gained} body(ies) across "
          f"{sum(1 for r in rows if r[4] is not None and r[3] > r[4])} reply(ies)")
    print(f"REGRESSED by {blind_name}: {len(regressed)} reply(ies)"
          + (f" — {', '.join(regressed[:6])}" if regressed else " (must be 0)"))

by_month = {}
for mt, _, t, g, _ in rows:
    if t <= g: continue
    k = datetime.datetime.fromtimestamp(mt).strftime("%Y-%m")
    a, b = by_month.get(k, (0, 0)); by_month[k] = (a + 1, b + t - g)
if by_month:
    print("\nper month: replies / bodies lost")
    for k in sorted(by_month):
        print(f"  {k}: {by_month[k][0]:>4} / {by_month[k][1]:>4}")

print("\nreply by reply (sha is the store key — open it directly):")
for mt, name, t, g, b in rows:
    ts = datetime.datetime.fromtimestamp(mt).strftime("%m-%d %H:%M")
    extra = f"  (blinded: {b})" if b is not None else ""
    flag = "LOST" if t > g else "ok  "
    print(f"  {flag} {ts} {name[:16]}  tags={t} records={g}{extra}")
PY
