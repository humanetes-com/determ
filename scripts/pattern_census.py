#!/usr/bin/env python3
"""Pattern census over the anyhow run logs — which stall-pattern classes each
run hit, whether any mechanism reacted, and what lever it took (or lacked).

WHY THIS EXISTS.  The pipeline's stall defenses were accreted one live failure
at a time, each with its own bespoke detector.  Before consolidating them into
one generic detector + a lever vocabulary (the "diagnostician" design,
2026-08-14), we need the evidence: which pattern classes actually recur across
runs, and which instances went SILENT (no mechanism printed a reaction) versus
REACTED.  The classes and their defining instances:

  P1 destroyed-baseline lock — a retry gate compares progress to a high-water
     taken before a deliberate destruction; the unlock needs the progress the
     destruction removed.  Run 259637-36db9aad (2026-08-07): `NOT re-admitting`
     against 81 for 9 hours after a re-stub dropped the tree to 21.
  P2 blocked-majority spend — an iteration spends its model calls on candidates
     whose tests are blocked by still-stubbed siblings, instead of on the
     blockers.  Run 259637-36db9aad final iters: 17 of 17 kept-blocked;
     run 298540-f792975a: 3.73 call-hours burned the same way.
  P3 futile aim — the same action is re-aimed at the same target with zero
     payoff: a component re-authored again for a crash its last re-author
     already failed to clear, or an identical producer-escalation set emitted
     sweep after sweep while the culprit is structurally invisible to the scan.
     Runs 58819-ee78a1d1 (Quoted re-fired attempt 2 on the same crash),
     635043-5f7c61f5 (frame-blind UAF, blame could never land),
     929616-ee20d540 (chain tests escalated to the same five constructors
     every sweep; the severing `source` impl in none of them).
  P4 poisoned evidence — a stored conviction/guidance whose premise the tree
     outgrew steers every later attempt into the same wrong body.  Live
     instance: `context.rs::*impl*::StdErrorforContextError<C,Error>` guidance
     "the only body-only source is `None`" (2026-08-13/14), refuted by the
     frozen tree, gated out only on 2026-08-14.
  P5 limit cycle — the score revisits the same small value set with
     destructions in between (79/74/79/74 across runs 58819-ee78a1d1 and
     929616-ee20d540).
  P7 sampling starvation — the sweep is the only measurement of the tree and
     the run took it once, or barely more often than never, because the fill
     batch between two sweeps is the whole frontier.  Measured 2026-08-15 from
     phase_c.tdd.iter timestamps: gaps of 198/253 min (3368-753757e6) and 243
     min (263217-d41a5bb5) against lives of 458m05s and 308m29s; one iteration
     each for 500301-6a66bf86 (214m10s) and 669347-cf33eae3 (4h17m).  Every
     sweep-boundary lever — batch ratchet, re-admission, crash retrigger,
     plateau exit — gets that many chances to fire in a whole run, and a fill
     pass landing after the last sweep is never scored at all, so the
     regression it caused ships to the next run as that run's baseline.

  P15 parked accessor — a failing test's owner waits under the dispatch cap,
     its unit-set unchanged for want of a dispatch, not served; the escalation
     used to fire on that identity (run 263474-b0e608d3 iter 2, test_altdebug
     past its never-tried src/fmt.rs::debug) and now defers, printing the line
     the detector counts.
  P14 compliance over compiler — the same unit fails consecutive attempts
     with an IDENTICAL primary diagnostic: the retry prompt carried the
     diagnostic and the reply made the same call anyway, the tell that a
     prompt input (a write-list instruction, a stored lesson, the model's own
     prior) outranks compile feedback.  Run 57025-6ba652f2 (2026-08-24):
     `Error::context` instructed into `construct_from_context`'s
     unsatisfiable bound, three identical `E0277`; the unprompted shape on
     the per-fn route dates to 2026-05-26.
  P16 regression-derived prescription — a retrospective written from a
     compiled attempt's REGRESSION verdict ("This compiled, but it REGRESSED
     previously-passing test(s): […]") diagnoses why THOSE tests reddened and
     prescribes what un-reddens them; read on a later re-fill of the unit for
     OTHER tests, the prescription can be the removal of the observable those
     tests pin.  Live instance (store key
     `src/ensure.rs::*impl*::BothDebugfor(A,B)`, cluster stable, 2026-08):
     `error_count 0, failing_test_count 13, convicted_by []`, guidance "Drop
     the fixed 40-byte SmallBuf … avoids the overflow fallback" — the exact
     40-byte overflow and whitespace fallback `test_trailer`/`test_whitespace`
     pin.  Screened since 2026-08-26 by the `regressed` stamp
     (`regression_scoped`); a record earned BEFORE the stamp carries none and
     is screened by nothing until it is re-earned.
  P8 fire that never lands — the layout retrigger SPENDS a full authoring
     round and the re-author dies in splice/locate infrastructure, so nothing
     reaches the tree.  Distinct from P6, which declines to spend.  Invisible
     until now because the two guards that meet it both work and both suppress
     its noise: `is_mechanical_reason` stops attempts 2..3, the refund keeps it
     off the destruction cap, and what remains is one line reading "attempt
     refunded".  Runs 669347-cf33eae3 (2026-08-15) and 1692702-94e6163a
     (2026-08-16, twice: 50m41s/14 model calls and 20m57s/7 of an 84-call run),
     both on `could not locate a frozen definition to replace: CastTo in
     ptr.rs` — a component neither crash named — after the 31-name `Error`
     component had authored a layout the skeleton COMPILED.

USAGE.
  python scripts/pattern_census.py [LOG ...] [--min-iters N] [--json OUT]
    LOG ...      log files in CHRONOLOGICAL order.  Default: the rotation
                 logs/pipeline_anyhow.log.10 .. .1 then logs/pipeline_anyhow.log
                 (only those that exist).  A run that spans a rotation boundary
                 is stitched back together by concatenating in this order.
    --min-iters  only report runs with at least N iteration summaries
                 (default 1: a launch that never reached phase-c has none and
                 is skipped, but a run that swept ONCE is reported — it is the
                 P7 case, and a default of 2 hid it).
    --json OUT   also write the full census (all runs, all evidence refs) as
                 JSON for downstream tooling.
  Read-only: no GPU, no model calls, no writes outside --json OUT.
  Cross-references logs/perf_ledger.jsonl for the operator note on each run id
  when that file exists.

HOW TO INTERPRET THE OUTPUT.  One block per qualifying run:
  run <id>  git=<sha> code=<hash>  <start time>  iters=<n>  <cluster>:pass
      trajectory and final/max pass — the score context for the hits below.
  P<k> HIT   <one-line evidence summary>  [file:line, ...]
      the class fired on this run; the refs are the log lines to read.
  P<k> hit + REACTED(<marker>) — a mechanism printed a reaction (the named
      marker line).  A reacted hit is a pathology the pipeline already answers;
      a SILENT hit is one it does not — those rows are the backlog for the
      generic detector, and the per-class SILENT count in the final aggregate
      is the priority order.
  terminal: how the run ended (death-watch exit/signal, plateau abort,
      traceback), plus the perf-ledger note when one exists.
After the cross-run chronics comes P13, which is standing state rather than a
run event: per FR file, how many `[regression]` notes are on disk and how many
carry NO body.  A body-less one asserts "this body broke those tests" with the
body missing, so the reader that retires such notes has nothing to compare and
keeps it forever, in every prompt naming that file.  Decision rule: any nonzero
BODY-LESS count is a backlog item, and a count that GROWS after 2026-08-22
means the FR round trip is destroying bodies again — the subjects line names
the fns whose prompts are affected.
After P13 comes P16's standing state: per retrospectives store, the records
on disk that were earned from a regression verdict (`error_count` 0 and a
`REGRESSED previously-passing test(s)` preview), with whether each carries the
`regressed` stamp the screen reads.  Decision rule: a record listed as
UNSTAMPED is screened by nothing — every prompt for that unit whose tests are
not the regressed ones is steered by a prescription about other tests until
the unit's next retro round overwrites it; the per-run P16 rows say which
runs it steered (SILENT: an honoured line on such a unit) and where the
        screen fired (REACTED: `retro guidance withheld (regression_scoped)`).
  P18 locator-collapsed session slice — the session's attributed units
     treated dispatch locators as a source, so a name with no site-frontier
     container expanded to every tree container of that name; the merge
     exceeded SLICE_SESSION_MAX_UNITS and plan_slices dropped the group
     (or a locator-only unit fanned out in the log).  Run 110993-6efa23be
     (2026-09-03): owner map named Debug, no phase_c.session.start, log
     `no slice planned (3 failing test(s) — hollow, empty, or over …)`.
     The collapsed three-way clause is this class; a split
     `N over SLICE_SESSION_MAX_UNITS=` refuse is not, unless the same fill
     also expanded a locator.  Distinct from an empty leftover (`ui`).
  P21 over-union session refusal — the failing tests' own slices each fit
     under SLICE_SESSION_MAX_UNITS and only their UNION does not (a
     coverage-complete leftover set sharing the construction spine), the
     session route refused the whole group, and the same fill's dispatch
     ranking still had never-served units it could not reach past the
     re-buys in the cap.  Run 2965-453dd7a4 (2026-09-05): 32 tests of 8-23
     sites, union 54, `no slice planned (32 failing test(s) — 1 over
     SLICE_SESSION_MAX_UNITS=32 (60 unit(s)))` on every fill, zero
     phase_c.session.start, `7 never-served unit(s)` on the same sweep.
     Distinct from P18: the split `N over SLICE_SESSION_MAX_UNITS=`
     line, not the three-way `hollow, empty, or over`; a locator-only
     expansion in a prior fill does not reclassify the refuse (service-
     order resets the fill's expansion flag).  Reaction: `session(s)
     fitted under the cap` (plan_slices packs the group).
  P24 crash-cohort lock — a sweep's crash-evidence cohort is larger than
     the dispatch batch and every graded unit is exempt from the pass
     cursor (`take_batch(exempt=_crash_grades.keys())` after
     `promote_crash_evidence`), so the batch is the cohort's head on every
     sweep and the tail is never dispatched.  The exemption's own contract
     ("ends by itself the moment the fault does") holds for a frame blame
     of a few units; over a chain-derived inference it is a lock.  Run
     1577437-c93d5c26 (2026-09-06): iter 3 synthesized chain-derived blame
     for 63 fn(s); iter 4's batch line read `63 unit(s) … 12 of them are in
     this batch` (the five construct_* and their neighbours), and both
     iter 2 and iter 4 read `(12/N this pass`, i.e. attempted_before=0; the
     process was SIGTERMed inside that batch, so the repeat is unread and
     the row grades ARMED rather than LOCKED.  P3 records the same run as
     `blamed NOBODY x6` and stops there.
  P27 orphaned stub at a green end — every test passes and the end gate
     exits 1 on reachable `unimplemented!()` bodies no test executes: the
     fill loop dispatches only what a red test reaches, and augmentation had
     stopped on a LINE percentage without asking whether each function runs.
     Run 301146-2d8a5f06 (2026-09-17): `WARNING: all tests passed, but 6 fn(s)
     are STILL unimplemented!()`, exit 1, after task0c stopped `nightly` at
     95.2 % (run 330242-0be2c3e1).  Reaction: the gate annotates each orphan
     from augmentation's function record (`EXECUTED this body at
     augmentation` / `no augmentation record accounts for it`); a stub the
     record measured unreached is a NOTE, not a WARNING, and not a hit.  A run
     that swept no phase-c iteration (a resumed task3 finishing in seconds)
     is below the default --min-iters; pass --min-iters 0 to see it.
  P28 unpeelable reply — a multi-body file-first reply failed to compile
     and the fn-granular peel refused `none_implicated`, so the whole reply
     reverted.  The live cause on run 3109629-f6bcbf01 (2026-09-18,
     src/error.rs, three attempts, 23 min): free fns answered `block=""`
     were routed to the prompt's target impl, the splice still landed them
     in the free fn, and the peel keyed them by the routed container, so a
     compile error inside one implicated no landed body.  Reaction: the
     router resolves a bare body to the file's single declaration of that
     name (`PEELED` on the next such reply).  A refusal that remains after
     the fix is either a scaffold/import error (honest) or a new key seam —
     read the refusal's diagnostic before trusting it.

P24 rows read: `LOCKED iters a->b` — consecutive dispatching sweeps, each
with cohort K > batch M, whose later batch is a subset of the earlier one
(nothing new dispatched); or `ARMED iter a` — the run's last dispatching
sweep had K > M and its cursor at 0 and no later sweep exists, so the
repeat could not be read.  Every row also carries the iters whose summary
read attempted_before=0 (the cursor zeroed by the exemption; cross-checked
against the `phase_c.tdd.iter` event) and the chain-derived blame lines with
their fn counts, because an INFERRED cohort is what pushes K past the cap.
The head is the schedule event's full batch when `phase_c.fill.schedule`
carries the sweep (the log prints at most 8 names), else the log's names
marked partial.  Decision rule: a cohort head repeated — or armed to repeat
— while attempted_before=0 is the cursor exemption applied to an inferred
cohort; the fix is a provenance-scoped exemption (frame-derived keys exempt,
chain-derived keys walk the cursor), not more GPU on the same head.  A
K <= M cohort re-dispatched identically is the exemption working and never
a row; a K > M sweep followed by a batch with a new name is the cursor
walking and never a row — that reading is the negative control for the fix.
The cross-run chronic `cohort_head` lists the same head bought under the cap
by more than one process.

The aggregate at the bottom counts, per class, total hits and how many were
SILENT.  Decision rule: a class with recurring SILENT hits needs its detector
and lever BEFORE any new bespoke mechanism is added; a class with only REACTED
hits is already owned and its mechanism is a consolidation candidate, not a
gap.  Healthy runs (score climbs, no hits) are listed at the end as negative
controls — a detector that fires on them is miscalibrated.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

# ---------------------------------------------------------------- line shapes

RE_STARTED = re.compile(
    r"PIPELINE STARTED — (?P<ts>[\d: -]+UTC)?.*?"
    r"(?:code=(?P<code>[0-9a-f]+))?\s*(?:git=(?P<git>[0-9a-f~]+))?\s*"
    r"run_id=(?P<run>[0-9a-f-]+)")
RE_ITER = re.compile(
    r"(?:\[(?P<cluster>\w+)\] )?\[phase-c/tdd\] iter (?P<iter>\d+): cargo test"
    r" rc=\S+ \([^)]*\); pass=(?P<passed>\d+) fail=(?P<fail>\d+)"
    r" oracle=(?P<oracle>\d+) \(improved=(?P<improved>\w+),"
    r" stall=(?P<stall>\d+)/(?P<stall_cap>\d+)\);"
    r" frontier=(?P<frontier>\d+) fn\(s\)[^,]*, (?P<exhausted>\d+) skipped")
# A retrigger's `continue` swallows the iteration summary, so a fired
# iteration's reading survives only on its binaries line (same recovery
# tools/distill_walk.py performs).  `X/Y` per binary is passed/failed.
RE_BINARIES = re.compile(
    r"\[phase-c/tdd\] iter (?P<iter>\d+) binaries: (?P<body>.*)")
RE_BIN_PAIR = re.compile(r" (\d+)/(\d+)(?:,|$)")
RE_NOT_READMIT = re.compile(
    r"moved pass no further than (?P<base>\d+) — NOT re-admitting")
RE_REBASE = re.compile(r"high-water re-based (?P<old>\d+) -> (?P<new>\d+)")
RE_RESTUB = re.compile(
    r"reverted (?P<n>\d+) (?:referencing )?fn\(s\) to re-fill")
RE_FIRED = re.compile(
    r"\[layout-retrigger\] \w+/(?P<comp>\[[^]]*\]): re-authored successfully"
    r" \(attempt (?P<attempt>\d+), cause=(?P<cause>\w+)\)")
RE_REJECTED = re.compile(
    r"re-author was not accepted \(rejection (?P<k>\d+)/(?P<cap>\d+)")
RE_SHRINK = re.compile(r"oracle shrink reproduced on immediate re-sweep")
RE_ESCALATE = re.compile(
    r"\[attribution/escalate\] (?P<test>\S+): accessors of (?P<accs>\[[^]]*\])"
    r" are futile — escalating to (?P<n>\d+) producer fn\(s\): (?P<head>[^(]*)")
RE_NOBODY = re.compile(r"blam(?:ing|ed) NOBODY")
RE_KEPT_BLOCKED = re.compile(
    r"kept (?P<fn>src/\S+) .*blocked by still-stubbed sibling")
RE_FILLED = re.compile(r"\d+/\d+ filled (?P<fn>src/[^\s]+)")
RE_EXH_CONVICTED = re.compile(
    r"skipped (?P<fn>src/\S+) \[[^]]*\] — fully exhausted across iters"
    r" \(retrospective marked")
RE_REVERTED_AFTER = re.compile(
    r"(?P<fn>src/\S+): reverted after \d+ attempt\(s\)")
# The honoured-record line the prompt-build seams emit since 2026-08-14. It is
# what turns P4 from a proxy into a direct read: the record that steered a
# prompt, its category, how many rounds it has survived, and whether it has a
# TREE PREMISE — recorded blockers it can be released against — or none, in
# which case nothing in the loop can ever expire it on its own evidence.
RE_RETRO_HONOURED = re.compile(
    r"\[phase-c/retro-honoured\] (?P<unit>\S+?):? category=(?P<cat>\S+) "
    r"rounds=(?P<rounds>\d+) (?P<premise>blocked_on\[\d+\]|NO tree premise)")
# P16 — the screen's own line, printed by the per-block/per-fn judge sites
# and by the prompt-build retirement line (`retired_guidance_line`), all
# carrying the phrase in the same spelling. The unit is the block id / fn key
# / store key that follows the route tag.
RE_RETRO_REGRESSION_SCOPED = re.compile(
    r"\[phase-c/(?:impl|per-fn|retro-retired)\] (?P<unit>\S+?):? .*?"
    r"retro guidance withheld \(regression_scoped\)")
RE_DEATH = re.compile(
    r"\[death-watch\] pipeline \d+ ended after (?P<dur>\S+) —"
    r" exit=(?P<exit>\d+)(?: signal=(?P<sig>\w+))?")
# A cause-C aim that REACHED a component and was refused by one of the
# retrigger's capped budgets. Each of these caps is cleared only by an outcome
# the refusal itself prevents, so every one of them is a candidate permanent
# lock — and the store they read is on disk, so the charge that spent the cap
# may have been made by a run that is not in this log at all.
RE_BUDGET_REFUSAL = re.compile(
    r"\[layout-retrigger\] [^:]+: (?P<why>"
    r"\d+ consecutive mechanical failure\(s\) already refunded"
    r"|\d+ consecutive re-author\(s\) REJECTED at validation"
    r"|cap \(\d+\) reached"
    r"|this component's re-author was already spent on this exact crash)")
RE_OWNS_GRADE = re.compile(r"the observed fault is inside code \d+ component")
# A reply the model was paid for that the READER turned into fewer records
# than it carried tags. Two shapes so old logs read like new ones: the drop
# sites' own line, which has printed a nonzero tag count 37 times across the
# retained logs without once being read as a defect, and the HEALTH line that
# now accompanies it and also fires on PARTIAL loss.
RE_TAGS_DROPPED = re.compile(
    r"\[phase-c/(?P<route>impl|file)\][^\n]*?(?P<unit>src/[^\s:]+(?:::[^\s:]+)?)"
    r"[^\n]*?no FN blocks[^\n]*?\((?:attempt \d+; )?(?P<tags>\d+) FN tag\(s\) in reply")
RE_READER_LOSS = re.compile(
    r"HEALTH ‼ \[reader_loss\] (?P<route>\S+) (?P<unit>\S+?): "
    r"(?P<tags>\d+) FN tag\(s\) in the reply, (?P<records>\d+) record\(s\) out"
    r"[^\n]*?Reply sha (?P<sha>[0-9a-f]+)")
# P23 — a declined unit: the model answered an empty body. Post-fix the
# route names it; pre-fix the only trace is a reader-loss line whose reply
# artefact (logs/prompts/<sha>.txt) holds empty `<FN>` tags.
RE_DECLINED = re.compile(
    r"\[phase-c/declined\] (?P<route>\S+) (?P<unit>\S+?): declined "
    r"(?P<n>\d+) unit\(s\) — the model answered an empty body: "
    r"(?P<names>[^(]+)\(reply sha (?P<sha>[0-9a-f]+)")
# P22 — the fill schedule's own line (the reaction to route starvation).
RE_SCHEDULE = re.compile(
    r"\[phase-c/schedule\] fill (?P<iter>\d+): (?P<n>\d+) run\(s\) — "
    r"1\. (?P<first>[^(]+)\((?P<reason>[^;)]+)")
RE_SESSION_OPENING = re.compile(
    r"\[phase-c/session\] session\[(?P<tests>\d+) test\(s\), (?P<units>\d+) "
    r"unit\(s\)\]: opening")
RE_STUB_ROUTE_LANDED = re.compile(
    r"\[phase-c/file\] (?P<file>src/\S+): FILE FILLED"
    r"|\[phase-c/per-fn\] pass \d+/\d+: \d+ stub\(s\) remaining")
# The reader-loss line prints a 12-char sha prefix and the full sha in the
# artefact path; the path is what opens the reply.
RE_READER_LOSS_PATH = re.compile(r"logs/prompts/(?P<full>[0-9a-f]{64})\.txt")
RE_INSTALLABLE_OFFERED = re.compile(
    r"\[phase-c/file\] (?P<file>src/\S+): (?P<n>\d+) declared fn\(s\) an "
    r"escalated producer could install are writable")
RE_INTERRUPTED = re.compile(
    r"an attempt was charged and never reached an outcome")
RE_MECHANICAL_DEATH = re.compile(
    r"re-author died in splice/locate infrastructure")
# P9 — the refusal and the later fire that contradicts it. The refusal names
# the component; the fire names the same component and its cause.
RE_SIG_REFUSED = re.compile(
    r"\[layout-retrigger\] \w+/(?P<comp>\[[^]]*\]): this component's re-author "
    r"was already spent on this exact crash")
RE_SIG_SUBSET_REFUSED = re.compile(
    r"refuted for a SUPERSET signature")
RE_VANISHED_SET = re.compile(
    r"vanished set \((?P<n>\d+) test\(s\)\)")
# P10 — a frontier stub no route dispatched. The driver subtracts the
# cfg-inactive population before printing, so a match is a routing gap and not
# the cfg filter working; pre-2026-08-17 logs did not, hence the count of
# cfg-dead names carried alongside.
RE_NO_ROUTE = re.compile(
    r"(?:\[(?P<cluster>\w+)\] )?\[phase-c/tdd\] iter \d+: "
    r"(?P<n>\d+) frontier stub\(s\) were dispatched by NO route this iter"
    r"[^:]*: (?P<fns>.+?)(?: \(\d+ further stub| *$)")
RE_CFG_INACTIVE = re.compile(
    r"(?:cluster (?P<cluster>\w+): )?"
    r"skipped \d+ of \d+ stub\(s\) not active in this cluster's cfg-set: "
    r"(?P<fns>.+)")
RE_LOCATE_MISS = re.compile(
    r"could not locate a frozen definition to replace: (?P<item>[^\s(]+)")
# P14 — compliance over compiler: the same unit failing consecutive attempts
# with an IDENTICAL primary diagnostic, the tell that something in the prompt
# (a write-list instruction, a stored lesson) is overriding compile feedback.
# The byte-identical resample skip catches only identical REPLIES; in the
# defining instance the replies differed while the error did not (run
# 57025-6ba652f2: three `E0277` on the same instructed call, 17:45/17:55/18:05
# UTC, and the class predates the instruction — the per-fn route hit the
# identical diagnostic on `src/error.rs::context` on 2026-05-26). Two line
# shapes: the per-fn/per-block route prints `compile failed (attempt N)` and
# then the first diagnostic on the unit's next line; the file-first route
# prints codes and the primary diagnostic on its revert line (added
# 2026-08-24 — earlier file-route logs carry no diagnostic, so file-route
# repeats before that date are invisible here rather than guessed at).
RE_ATTEMPT_FAIL = re.compile(
    r"\[phase-c/impl\] (?P<unit>\S+): compile failed \(attempt (?P<n>\d+)\)")
RE_ATTEMPT_DIAG = re.compile(
    r"\[phase-c/impl\] (?P<unit>\S+): +error\[(?P<code>E\d+)\]: (?P<msg>.+)")
RE_FILE_REVERT_DIAG = re.compile(
    r"\[phase-c/file\] (?P<unit>\S+): reverted \(compile failed; "
    r"codes=\[[^\]]*\] first: error\[(?P<code>E\d+)\]: (?P<msg>.+?)\) — ")

# Reaction markers: a line matching one of these is the pipeline ANSWERING a
# pattern (the value is the short name shown as REACTED(<name>)).  Grouped by
# the class whose hit they answer.
REACTIONS: dict[str, list[tuple[str, str]]] = {
    "P1": [
        # NOTE: the refusal line itself reads "NOT re-admitting exhausted",
        # so the positive marker must be the full positive phrasing.
        ("high-water re-based", "rebase"),
        ("previously-exhausted fn(s)/block(s)", "readmit"),
    ],
    "P2": [
        # Only the pre-fix-loop keep-and-defer counts: the older consumer
        # deferral line ("deferred N consumer(s) blocked by still-stubbed
        # producer(s)") reacts AFTER the model calls were already spent, which
        # is the burn itself, not an answer to it.
        ("sibling-blocked", "keep-and-defer"),
        ("salvaged_body_kept", "salvage"),
    ],
    "P3": [
        ("already spent on this exact crash", "sig-refuted"),
        ("walk stops here and the stall exit stands", "walk-closed"),
        ("traversal step(s) the futile accessors dispatch", "traversal"),
        ("CHAIN-DERIVED blame", "chain-blame"),
    ],
    "P4": [
        ("rejection(s) retired — they were earned under", "epoch-retire"),
        ("retro-gate: honoured=", "retro-gate"),  # weakest: gate ran at all
    ],
    "P5": [
        ("zero-payoff", "payoff"),
        ("refuted for this signature", "sig-refuted"),
    ],
    "P6": [
        # The refund that costs nothing: the death record says the process was
        # stopped, so the streak does not move (2026-08-15).
        ("NOT counted as a mechanical failure", "stop-not-charged"),
        # …and a streak already on file, earned by a build that is no longer
        # running, announces that it is standing down rather than releasing in
        # silence. Deliberately NOT keyed on "re-authored successfully": a
        # DIFFERENT component firing later in the run is not an answer to the
        # one that was refused.
        ("were earned by a different build", "stale-streak-released"),
    ],
    "P7": [
        # The batch cap: the loop stopped to measure instead of spending the
        # whole frontier before looking (2026-08-15).
        ("mid-pass)", "batch-capped"),
        # …and the sweep taken on the way out, so a fill pass that landed after
        # the last ordinary sweep is scored by the run that caused it rather
        # than inherited unscored by the next one.
        ("final sweep (stopped", "final-sweep"),
    ],
    "P8": [
        # The stale key is recognised before the model is asked, and repaired
        # from the file the splice will actually read.
        ("Re-deriving them from the scaffold", "locate-keys-repaired"),
        # …and when the repair cannot help, the unlocatable component is
        # dropped instead of discarding the one the evidence named.
        ("DROPPING", "scope-narrowed"),
        # The streak, said out loud: a refund is not a report.
        ("NEVER LANDED", "never-landed-reported"),
    ],
    "P9": [
        # The subset-aware refutation holding the line: the crash in front of
        # it is the refused one minus what the loop repaired, so the aim stays
        # shut and the log says which tests were fixed (2026-08-17).
        ("refuted for a SUPERSET signature", "subset-refuted"),
        # …and the authoring-round budget, which stops a signature buying a
        # second ~55-minute round whether or not the first one landed.
        ("authoring round(s) of a cap of", "round-budget"),
    ],
    "P10": [
        # The deferral no longer throws away the frontier's other stubs: the
        # producers the blockers are blocked on stay in the same batch.
        ("the deferral keeps", "deferral-keeps-stubs"),
        # …and the line itself now separates the cfg-dead population, so a hit
        # is a routing gap rather than the cfg filter working.
        ("are cfg-inactive in this cluster", "cfg-dead-excluded"),
    ],
    "P16": [
        # The screen firing IS the answer: the record stays in the store,
        # its text is what was dropped, and this line is the only trace.
        ("retro guidance withheld (regression_scoped)", "regression-scoped"),
    ],
    "P18": [
        # A session that actually opens is the answer to an exploded or
        # locator-only slice that used to prevent one. The over-cap / expand
        # lines remain the HIT; this marker is the reaction on a later iter
        # of the same log (or a later run of the same tree).
        ("opening — targets", "session-opened"),
    ],
    "P21": [
        # The packing statement is the reaction: the over-union group was
        # fitted into sessions under the cap instead of refused.
        ("fitted under the cap", "session-fitted"),
        ("opening — targets", "session-opened"),
    ],
    "P22": [
        # The schedule put a stub-holding run ahead of the session.
        ("[phase-c/schedule] fill", "scheduled"),
    ],
    "P23": [
        # The route named the decline (and the unit's next prompt carries it).
        ("[phase-c/declined]", "declined-named"),
    ],
    "P25": [
        # The sweep read the hang as a reading and put the durable pair back,
        # or a fill gate reverted the body that hung the suite.
        ("HUNG in ", "hang-scored"),
        ("hung tree restored", "hung-tree-restored"),
        ("phase-c/hang_reverted", "hang-reverted"),
        ("phase-c/u2_hang", "hang-rejected"),
        ("hung inside ", "group-killed"),
    ],
    "P28": [
        # The peel isolated the blamed bodies and kept the rest.
        ("PEELED ", "peeled"),
    ],
    "P27": [
        # The end gate classified each orphan against augmentation's
        # function-execution record (printed on the lines after the warning).
        ("EXECUTED this body at augmentation", "orphan-executed-upstream"),
        ("no augmentation record accounts for it", "orphan-unaccounted"),
    ],
    "P26": [
        # The bound converted an unbounded coverage wait into a P25 hang.
        ("hung inside ", "group-killed"),
        ("TIMEOUT after ", "group-killed"),
    ],
    "P19": [
        # The boundary invariant heals before the sweep (or on the sweep's
        # own uncoded diagnostic); the older reaction is the abort-time heal.
        ("TREE UNPARSEABLE", "boundary-heal"),
        ("[heal] ", "heal"),
    ],
    "P20": [
        # A re-seed line that reports its post-state came from the
        # transactional re-seed, which collapses wanted to units.
        ("tree after:", "txn-reseed"),
    ],
    "P24": [
        # No printed reaction exists. The answer is a data reading — the next
        # K > cap sweep's `(D/F this pass` with D - N > 0 and a batch carrying
        # a name the previous one did not — and on that reading the detector
        # does not fire at all rather than tag the hit REACTED.
    ],
    "P14": [
        # The per-block loop's own stopper: it ends the SPEND after the
        # repeat, which is damage control, not an answer to what keeps
        # instructing the same call — a hit that only ever shows this
        # reaction is still an open class.
        ("identical errors after attempt", "identical-error-stop"),
        # The resample skip fires only when the whole PROMPT rebuilt
        # byte-identical; it cannot see identical ERRORS under differing
        # replies, which is this class's defining shape.
        ("rebuilt a byte-identical prompt", "resample-skip"),
    ],
}

# GPU_GUARD_STOP is a terminal the LOOP chose: the flat-score guard stopped
# the run before spending a dispatch on a reading the window already held.
# A run that ends this way is a run whose burn was cut short, so it belongs
# beside the other terminals when a segment is judged — the census reads it
# as "ended by decision", not as a crash.
TERMINALS = ("MODULE_PLATEAU_ABORT", "VERDICT MISMATCH", "TIMED OUT",
             "Traceback (most recent call last)", "GPU_GUARD_STOP")


def default_logs() -> list[Path]:
    root = Path(__file__).resolve().parent.parent / "logs"
    order = [root / f"pipeline_anyhow.log.{i}" for i in range(10, 0, -1)]
    order.append(root / "pipeline_anyhow.log")
    return [p for p in order if p.exists()]


def load_ledger_notes(root: Path) -> dict[str, str]:
    path = root / "logs" / "perf_ledger.jsonl"
    if not path.exists():
        return {}
    notes = {}
    for line in path.read_text().splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("run_id") and row.get("note"):
            notes[row["run_id"]] = row["note"]
    return notes


# ------------------------------------------------------------------ the model


class Run:
    def __init__(self, run_id: str, git: str, code: str, ts: str, ref: str):
        self.run_id, self.git, self.code, self.ts = run_id, git, code, ts
        self.start_ref = ref
        self.lines: list[tuple[str, str]] = []   # (ref "file:lineno", text)

    # ---- extraction ----
    def iters(self) -> list[dict]:
        """Sweep readings in log order.  A summary line is a full reading; a
        binaries line whose summary never arrived (the retrigger's `continue`
        swallowed it) is recovered as a synthesized reading — pass = sum of
        per-binary passed counts, other fields absent.  Iter numbers restart
        after fires, so ORDER is the trajectory, not the numbers."""
        out: list[dict] = []
        pending: dict | None = None   # binaries reading awaiting its summary
        for ref, text in self.lines:
            if (m := RE_ITER.search(text)):
                d = {k: (int(v) if v.isdigit() else v)
                     for k, v in m.groupdict(default="").items()}
                d["ref"] = ref
                if pending and pending["iter"] == d["iter"]:
                    pending = None          # the summary made it out
                elif pending:
                    out.append(pending)     # swallowed: keep the recovery
                    pending = None
                out.append(d)
            elif (m := RE_BINARIES.search(text)):
                if pending:
                    out.append(pending)
                passed = sum(int(a) for a, _b in
                             RE_BIN_PAIR.findall(m["body"]))
                pending = {"iter": int(m["iter"]), "passed": passed,
                           "cluster": "", "recovered": True, "ref": ref}
        if pending:
            out.append(pending)
        return out

    def find(self, regex: re.Pattern) -> list[tuple[str, re.Match]]:
        return [(ref, m) for ref, text in self.lines
                if (m := regex.search(text))]

    def reactions_for(self, cls: str, after: int = 0) -> list[tuple[str, str]]:
        """Reaction markers AT OR AFTER line index `after` (the first evidence
        line of the hit).  A reaction printed before the pathology set in is
        not an answer to it — run 259637-36db9aad re-admitted fns in its early
        iterations and then locked for nine hours; counting the early line
        would tag the lock REACTED."""
        hits = []
        for needle, name in REACTIONS.get(cls, []):
            for i, (ref, text) in enumerate(self.lines):
                if i >= after and needle in text:
                    hits.append((name, ref))
                    break
        return hits


def split_runs(paths: list[Path]) -> list[Run]:
    runs: list[Run] = []
    cur: Run | None = None
    for path in paths:
        for i, text in enumerate(path.read_text(errors="replace")
                                 .splitlines(), 1):
            ref = f"{path.name}:{i}"
            m = RE_STARTED.search(text)
            if m:
                cur = Run(m["run"], m["git"] or "?", m["code"] or "?",
                          (m["ts"] or "?").strip(), ref)
                runs.append(cur)
            elif cur is not None:
                cur.lines.append((ref, text))
    return runs


# ------------------------------------------------------------------ detectors
# Each detector returns a list of hit dicts: {"evidence": str, "refs": [...]}.
# Empty list = the class did not fire on this run.


def detect_p1(run: Run, iters: list[dict]) -> list[dict]:
    """Destroyed-baseline lock: repeated `NOT re-admitting` against the same
    baseline B, after a deliberate re-stub, while no sweep between the first
    and last refusal reaches B.  A `high-water re-based` line BEFORE the first
    refusal means the baseline was told about the destruction — that is the
    reaction, not the pathology."""
    refusals = run.find(RE_NOT_READMIT)
    restubs = run.find(RE_RESTUB)
    if not refusals or not restubs:
        return []
    order = {ref: i for i, (ref, _t) in enumerate(run.lines)}
    by_base: dict[int, list[str]] = defaultdict(list)
    for ref, m in refusals:
        by_base[int(m["base"])].append(ref)
    hits = []
    for base, refs in by_base.items():
        if len(refs) < 2:
            continue
        # Only sweeps AFTER the first refusal count: a pre-destruction reading
        # equal to B is exactly the stale high-water the class is about.
        first = min(order.get(r, 0) for r in refs)
        later = [d["passed"] for d in iters if order.get(d["ref"], 0) > first]
        if later and max(later) >= base:
            continue   # the run did reach B again: the gate was satisfiable
        hits.append({
            "evidence": f"{len(refs)} refusal(s) against baseline {base}; "
                        f"max pass after the first refusal "
                        f"{max(later) if later else '?'} (< {base}); "
                        f"{len(restubs)} deliberate re-stub(s)",
            "refs": refs[:4]})
    return hits


def detect_p2(run: Run, iters: list[dict]) -> list[dict]:
    """Blocked-majority spend: an iteration whose kept-blocked outcomes cover
    most of the frontier the closing summary reports — the spend went to fns
    whose tests cannot run.  Calibrated on run 259637-36db9aad's tail
    (pipeline_anyhow.log.3): spans of 31 and 15 kept-blocked against
    frontiers of ~17-18, the '17 of 17 kept' instance."""
    spans: list[dict] = [{"kept": [], "filled": 0, "frontier": None}]
    for ref, text in run.lines:
        if (m := RE_ITER.search(text)):
            spans[-1]["frontier"] = int(m["frontier"])
            spans.append({"kept": [], "filled": 0, "frontier": None})
        elif (m := RE_KEPT_BLOCKED.search(text)):
            spans[-1]["kept"].append(ref)
        elif RE_FILLED.search(text):
            spans[-1]["filled"] += 1
    hits = []
    for span in spans:
        k, fr = len(span["kept"]), span["frontier"]
        if fr is not None and fr >= 5 and k >= 3 and k >= 0.7 * fr:
            hits.append({
                "evidence": f"{k} kept-blocked vs frontier {fr} "
                            f"({span['filled']} filled) in one iteration",
                "refs": span["kept"][:4]})
    return hits[:3]


def detect_p3(run: Run, iters: list[dict]) -> list[dict]:
    """Futile aim, two shapes.  (a) the same component re-authored ≥2 times
    while the crash class persists (an oracle-shrink line appears after the
    last fire).  (b) the same test escalated to an identical producer set on
    ≥3 sweeps."""
    hits = []
    fires: dict[str, list[str]] = defaultdict(list)
    for ref, m in run.find(RE_FIRED):
        fires[m["comp"]].append(ref)
    shrink_refs = [ref for ref, _ in run.find(RE_SHRINK)]
    # "after" must be judged on line ORDER, never on the ref strings: a ref is
    # "file:lineno", which sorts neither within a file ("log.1:998" >
    # "log.1:1500") nor across the rotation (log.1 is the NEWEST file but
    # sorts before log.10).  The string compare cost two limit-cycle runs
    # their P3 rows (1135197-c811b168, 2846-58cbbf3b: re-fired components
    # with the crash still reproducing, silently dropped).
    order = {ref: i for i, (ref, _t) in enumerate(run.lines)}
    for comp, refs in fires.items():
        if (len(refs) >= 2 and shrink_refs
                and order.get(shrink_refs[-1], -1) > order.get(refs[-1], -1)):
            hits.append({
                "evidence": f"component {comp} re-authored {len(refs)}x, "
                            "crash class still reproducing after the last fire",
                "refs": refs[:4]})
    esc: dict[tuple[str, str], list[str]] = defaultdict(list)
    for ref, m in run.find(RE_ESCALATE):
        esc[(m["test"], m["head"].strip())].append(ref)
    stuck = {t for (t, _h), refs in esc.items() if len(refs) >= 3}
    if stuck:
        refs = [r for (t, _h), rs in esc.items() if t in stuck for r in rs]
        hits.append({
            "evidence": f"{len(stuck)} test(s) escalated to an identical "
                        f"producer set on >=3 sweeps: {sorted(stuck)[:4]}",
            "refs": refs[:4]})
    # (c) frame-blind crash: the localizer looked and blamed nobody at least
    # twice while the shrink kept reproducing — blame structurally cannot
    # land, so every localizer run is the same action with zero payoff.
    nobody = [ref for ref, _ in run.find(RE_NOBODY)]
    if len(nobody) >= 2 and shrink_refs:
        hits.append({
            "evidence": f"localizer blamed NOBODY {len(nobody)}x with the "
                        "crash reproducing deterministically",
            "refs": nobody[:4]})
    return hits


def detect_p4(run: Run, iters: list[dict]) -> list[dict]:
    """Poisoned evidence.

    DIRECT read where the log carries it: a stored record steered ≥3 prompts
    for one unit while carrying NO tree premise. Both halves matter. The count
    says the record is not a one-off nudge but the standing instruction for
    that unit; "no tree premise" says it records no blockers, so
    `impossibility_claim_is_unfounded` — the one gate that can retire a record
    on evidence rather than on a score move — has nothing to test it against
    and the record cannot expire on its own. That is the class exactly: not "a
    conviction exists" but "a conviction that cannot be checked keeps steering".
    Measured on `logs/pipeline_anyhow.log` 2026-08-16: 76 honoured lines, 71 of
    them with no tree premise.

    The two PROXIES below (skipped-under-conviction, reverted-after-attempts)
    are kept for segments recorded before the honoured line existed — every run
    before 2026-08-14 — and are skipped when the direct signal is available,
    because they infer from downstream symptoms what the line now states."""
    direct: dict[str, list[str]] = defaultdict(list)
    meta: dict[str, tuple[str, int]] = {}
    for ref, m in run.find(RE_RETRO_HONOURED):
        if m["premise"] != "NO tree premise":
            continue
        unit = m["unit"]
        direct[unit].append(ref)
        meta[unit] = (m["cat"], int(m["rounds"]))
    if direct:
        hits = []
        for unit, refs in sorted(direct.items(), key=lambda kv: -len(kv[1])):
            if len(refs) < 3:
                continue
            cat, rounds = meta[unit]
            hits.append({
                "evidence": f"{unit} steered {len(refs)} prompt(s) on a "
                            f"{cat} record with NO tree premise "
                            f"(rounds={rounds}) — nothing can expire it",
                "refs": refs[:4]})
        if hits:
            return hits[:4]
    hits = []
    for regex, what in ((RE_EXH_CONVICTED, "skipped under conviction"),
                        (RE_REVERTED_AFTER, "reverted after attempts")):
        per_fn: dict[str, list[str]] = defaultdict(list)
        for ref, m in run.find(regex):
            per_fn[m["fn"].rstrip(":")].append(ref)
        for fn, refs in sorted(per_fn.items(), key=lambda kv: -len(kv[1])):
            if len(refs) >= 3:
                hits.append({
                    "evidence": f"{fn} {what} {len(refs)}x",
                    "refs": refs[:4]})
    return hits[:4]


def detect_p5(run: Run, iters: list[dict]) -> list[dict]:
    """Limit cycle: some pass value recurs ≥3 times with dips below it in
    between, and at least one retrigger fire in the run (the cycle is driven
    by deliberate destruction, not by noise)."""
    passes = [(d["passed"], d["ref"]) for d in iters]
    fires = run.find(RE_FIRED)
    if len(passes) < 4 or len(fires) < 2:
        return []
    hits = []
    for v in sorted({p for p, _ in passes}, reverse=True):
        idx = [i for i, (p, _) in enumerate(passes) if p == v]
        if len(idx) < 2:
            continue
        dips = sum(1 for a, b in zip(idx, idx[1:], strict=False)
                   if any(passes[i][0] < v for i in range(a + 1, b)))
        # zero net progress: after cycling, the run never gets above v
        if dips >= 1 and max(p for p, _ in passes[idx[-1]:]) <= v:
            hits.append({
                "evidence": f"pass returns to {v} ({len(idx)} visits, "
                            f"{dips} dip(s) between) and never exceeds it; "
                            f"{len(fires)} retrigger fire(s) this run",
                "refs": [passes[i][1] for i in idx[:4]]})
            break   # the highest cycling value is the finding
    return hits


def detect_p6(run: Run, iters: list[dict]) -> list[dict]:
    """Budget lockout: the aim REACHED a component — graded it on observed
    blame — and a capped budget refused it. Distinct from P3, where the aim
    finds no candidate at all: here the evidence was good enough and the
    refusal is bookkeeping.

    Worth its own class because every one of these caps is cleared only by an
    outcome the refusal prevents, and the store is on disk, so the charge that
    spent it may predate this run entirely. Measured 2026-08-15: two operator
    SIGTERMs during the 34-name construct/vtable component's re-author (runs
    `462635-c8369188`, `496645-9b9de7e1`) each booked a mechanical failure
    through the interrupted-attempt refund, and run `500301-6a66bf86` then
    refused that component at iteration 1 having just graded it OWNS off an
    in-crate ASAN origin — three runs, no model attempt failed anywhere in the
    sequence."""
    refusals = run.find(RE_BUDGET_REFUSAL)
    if not refusals:
        return []
    graded = run.find(RE_OWNS_GRADE)
    refunds = run.find(RE_INTERRUPTED)
    hits = []
    for why in sorted({m["why"] for _ref, m in refusals}):
        refs = [ref for ref, m in refusals if m["why"] == why]
        hits.append({
            "evidence": f"aim refused {len(refs)}x by a capped budget: {why}"
                        + (f"; the same run graded a component on observed "
                           f"blame {len(graded)}x" if graded else "")
                        + (f"; {len(refunds)} interrupted attempt(s) refunded "
                           "in this run — a charge nothing measured"
                           if refunds else ""),
            "refs": refs[:4]})
    return hits


#: A run whose mean iteration costs at least this many minutes is sampling its
#: own tree more slowly than the levers that read the samples can act. Set from
#: the measured spread: healthy multi-iteration runs on anyhow sit well under
#: it, while the starved ones measured 198-253 minutes per iteration.
P7_MINUTES_PER_ITER = 90
#: …and a run that produced exactly one iteration is starved regardless of the
#: mean (there is no gap to average) once it lived long enough to have taken a
#: second reading. That precondition has to be MEASURED, never assumed: the
#: first version of this detector read `life is None or life >= 60`, which
#: inverts the guard — the one case where the run's life is unknown is the one
#: case where "it lived long enough" cannot be established. Measured over the
#: live logs on 2026-08-16 it produced 19 of P7's 35 hits, all of them runs
#: with no death record, and they are dominated by launches that died in setup:
#: `1527710-e72c9dd0` started 16:20:36 and its successor started 16:28:01, one
#: sweep, `pass: 0`. A false row in this census is expensive out of proportion
#: to its size, because CLAUDE.md makes the census the FIRST step of every
#: diagnosis precisely so that a known class is a lookup instead of an
#: investigation; a class whose majority is noise sends the next reader back to
#: the raw log, which is the ritual the census exists to end.
P7_MIN_LIFE_MINUTES = 60


def _life_minutes(run: Run) -> float | None:
    """MEASURED wall-clock minutes from the death-watch line, or None.

    None means "this run's life is not established by the log", and every
    caller must treat that as a reason to stay silent rather than as a reason
    to report. There is no fallback on purpose: the only other bound available
    here is the NEXT run's start time, which is an upper bound on how long this
    run lived, and P7 needs a LOWER one — the question is whether the run lived
    long enough to have taken a second reading. An upper bound can refute that
    and can never confirm it, so a detector leaning on one would be asserting
    exactly what it cannot see."""
    for _ref, m in run.find(RE_DEATH):
        mm = re.fullmatch(r"(?:(\d+)m)?(?:(\d+)s)?", m["dur"] or "")
        if mm and (mm.group(1) or mm.group(2)):
            return int(mm.group(1) or 0) + int(mm.group(2) or 0) / 60
    return None


def detect_p7(run: Run, iters: list[dict]) -> list[dict]:
    """Sampling starvation: the sweep is the only measurement of the tree and
    the run took it once, or barely more often than never.

    The sweep costs about a second of cargo; what separates two of them is the
    fill batch between them. When that batch is the whole frontier the period
    is hours, so a run's whole life fits inside one or two readings — and every
    gate that acts at a sweep boundary (batch ratchet, re-admission, crash
    retrigger, plateau exit) gets one or two chances to fire. Measured
    2026-08-15: gaps of 198/253 minutes in `3368-753757e6`, 243 in
    `263217-d41a5bb5`, and single-iteration lives of 214m10s
    (`500301-6a66bf86`) and 4h17m (`669347-cf33eae3`).

    The second-order cost is why this is a census class rather than a note: a
    fill pass that lands after the last sweep is never scored at all, so a
    regression it caused ships to the NEXT run as that run's baseline, where it
    reads as a starting condition instead of as damage something did.

    Both arms require a MEASURED life (`_life_minutes`). Silence on a run whose
    end the log does not record is deliberate and is the whole calibration of
    this detector — see `P7_MIN_LIFE_MINUTES`.
    """
    if not iters:
        return []
    life = _life_minutes(run)
    hits = []
    if len(iters) == 1:
        # A measured life or nothing. A run whose end the log does not record
        # is usually one that died in setup, and those are exactly what
        # `--min-iters` is for; reporting them as starvation buries the two
        # runs the class was written from.
        if life is not None and life >= P7_MIN_LIFE_MINUTES:
            hits.append({
                "evidence": f"ONE sweep in the whole run ({life:.0f}m of "
                            "wall-clock) — the score is a single reading, and "
                            "any fill pass after it left unmeasured",
                "refs": [iters[0]["ref"]]})
        return hits
    if life is not None:
        per = life / len(iters)
        if per >= P7_MINUTES_PER_ITER:
            hits.append({
                "evidence": f"{len(iters)} sweep(s) in {life:.0f}m — "
                            f"{per:.0f}m per iteration, so every "
                            "sweep-boundary lever got at most "
                            f"{len(iters)} chance(s) to fire",
                "refs": [d["ref"] for d in iters[:4]]})
    return hits


def detect_p8(run: Run, iters: list[dict]) -> list[dict]:
    """Fire that never lands: the retrigger spends a full authoring round and
    the re-author dies in splice/locate infrastructure, so nothing reaches the
    tree — repeatedly, and QUIETLY.

    WHY IT IS ITS OWN CLASS, and not P6. A budget refusal (P6) declines to
    spend; this SPENDS and then discards. It is invisible to every other
    detector because the two guards that meet it both behave correctly and both
    suppress the noise it used to make: `is_mechanical_reason` stops attempts
    2..3 being spent on an attempt-invariant reason, and the refund keeps the
    mechanical failure off the destruction cap. What is left in the log is one
    line reading "attempt refunded", which is indistinguishable from a cheap
    no-op.

    MEASURED. Run `1692702-94e6163a` (2026-08-16) fired twice, at 13:00:47Z and
    16:16:17Z, and both died on `could not locate a frozen definition to
    replace: CastTo in ptr.rs` — a component the crash never named — AFTER the
    31-name `Error` component had authored a layout the skeleton COMPILED.
    50m41s/14 model calls and 20m57s/7, out of a 3h36m run's 84 calls. Run
    `669347-cf33eae3` (2026-08-15) died the same way, which is the run
    `is_mechanical_reason`'s docstring was written from — the class is at least
    two runs old and had no census row until it cost a third."""
    deaths = run.find(RE_MECHANICAL_DEATH)
    if not deaths:
        return []
    locate = run.find(RE_LOCATE_MISS)
    hits = [{
        "evidence": f"{len(deaths)} fire(s) spent an authoring round and "
                    "landed nothing (re-author died in splice/locate "
                    "infrastructure, attempt refunded)"
                    + (f"; {len(locate)} unlocatable frozen definition(s): "
                       + ", ".join(sorted({m['item'] for _r, m in locate})[:4])
                       if locate else ""),
        "refs": [ref for ref, _m in deaths[:4]]}]
    return hits


def detect_p9(run: Run, iters: list[dict]) -> list[dict]:
    """Destruction authorised by progress: the retrigger REFUSES a component
    for a crash signature, the loop then repairs part of that crash, and the
    smaller crash re-opens the aim it had just been refused.

    WHY IT IS ITS OWN CLASS. Every other class here is a mechanism failing to
    act or acting on nothing. This one is a mechanism acting BECAUSE the loop
    succeeded: `crash_signature` hashes the vanished-test set, so any fix that
    un-crashes a binary changes the key, the refutation row is not found, and a
    component that was correctly immune becomes a target again. The predicate
    "has the situation changed since we last refused" is satisfied by progress,
    which is the general shape — and it is the opposite sign of P1, where a
    baseline destroyed by a deliberate re-stub made a retry gate unsatisfiable
    exactly when it was needed.

    MEASURED. Run `1920662-40f711b2` (2026-08-16/17). Iteration 1 refused the
    31-name `Error` component in as many words, `vanished` being
    `{test_large_alignment, test_no_backtrace_fallback}`. The promoted dispatch
    filled `object_drop_front`, `tests/test_downcast.rs` went 0/7 to 4/3 and
    pass 79 to 83 at 92 minutes — the first verdict movement on that binary in
    the recorded run history — and `vanished` shrank to
    `{test_no_backtrace_fallback}`. The aim re-opened, fired at iterations 2
    (three attempts, all rejected, 55 min) and 4 (landed: 54 bodies reverted,
    83 to 25). Net for 9h48m and 191 model calls: −46 tests.

    THE SIGNATURE IN THE LOG is a refusal naming a component, followed later in
    the same run by a fire on that same component with a strictly SMALLER
    vanished-set reading. LATER is load-bearing and is enforced on line order:
    the refutation WORKING produces the opposite sequence — a fire lands at
    iter 1, the crash reproduces, the hash-exact refusal prints at iter 3 —
    and a pooled comparison would flag that healthy run as the pathology,
    polluting the census the recurrence check reads. The subset-aware
    refutation shipped 2026-08-17 prints `refuted for a SUPERSET signature`
    when it holds the line, so a run carrying that marker is REACTED rather
    than a hit."""
    refusal_at: dict[str, int] = {}
    repeats: list[tuple[str, re.Match]] = []
    for i, (ref, text) in enumerate(run.lines):
        if (m := RE_SIG_REFUSED.search(text)):
            refusal_at.setdefault(m["comp"], i)
        elif (m := RE_FIRED.search(text)):
            if m["comp"] in refusal_at and i > refusal_at[m["comp"]]:
                repeats.append((ref, m))
    if not repeats:
        return []
    refused_comps = {m["comp"] for _r, m in repeats}
    # The shrink itself, when the log carries the per-sweep vanished readings.
    counts = [int(m["n"]) for _r, m in run.find(RE_VANISHED_SET)]
    shrank = bool(counts) and min(counts) < max(counts)
    return [{
        "evidence": f"{len(repeats)} fire(s) on {len(refused_comps)} component(s) "
                    "this run had already REFUSED for the same crash"
                    + (f"; the vanished set shrank {max(counts)} -> "
                       f"{min(counts)} test(s) in between, which is the loop "
                       "working and is what re-opened the aim" if shrank else "")
                    + f" — {', '.join(sorted(refused_comps))[:120]}",
        "refs": [ref for ref, _m in repeats[:4]]}]


def detect_p10(run: Run, iters: list[dict]) -> list[dict]:
    """Undispatchable frontier unit: a stub sits in the frontier, no route
    claims it, and nothing charges or escalates — so the loop can spend
    iterations on its CONSUMERS indefinitely while the pass count says nothing.

    WHY IT IS ITS OWN CLASS. P7 is sampling starvation — a unit the cap keeps
    reaching last. This is a unit no route will take at all, which no counter
    can see: the driver deliberately does not charge an exhaustion strike to
    something nobody attempted (that charge is what locked every A/B/C cluster
    out of its impl members on 2026-08-11/12), so the correct fix for one defect
    created a state with no terminator.

    MEASURED. Run `1920662-40f711b2` iteration 6 (2026-08-17 05:14Z): the impl
    route deferred itself behind 8 free-fn/oversized blockers, all 8 landed, and
    all 8 logged `kept ... (compiles; N attributed test(s) blocked by
    still-stubbed sibling(s))` — zero verdicts could move by construction and
    the sweep read 33/105 twice. The four they were waiting on —
    `src/context.rs::ext_context`, `src/error.rs::drop`, `src/error.rs::from`,
    `src/kind.rs::new` — were dispatched by no route in any of the run's 7
    iterations, 10 hours and 191 model calls. `src/kind.rs::new` alone is the
    named stub target of eight distinct tests.

    THE CFG-INACTIVE POPULATION IS SUBTRACTED, and that is the difference
    between a row and a false positive. Before 2026-08-17 the driver's line did
    not exclude stubs this cluster's cfg-set gates out, and on the retained logs
    8 of its 10 instances were `src/nightly.rs::fmt` in cluster `stable` —
    correctly undispatched, since the engine refuses a cfg-dead dispatch by
    design. This detector re-applies the subtraction from the membership report
    the per-fn route prints, so old logs read the same as new ones."""
    hits = run.find(RE_NO_ROUTE)
    if not hits:
        return []
    # The cfg-dead population is PER CLUSTER: `src/nightly.rs::fmt` is
    # cfg-inactive in `stable` and a live routing gap in `nightly`, and a
    # run-wide union would suppress the second with the first — a false
    # negative in the one detector whose point is separating those
    # populations. Lines missing the cluster capture (older formats) fall
    # into the "" bucket, which every hit also consults, preserving the old
    # pooled behaviour exactly where the cluster is unknowable.
    dead: dict[str, set[str]] = {}
    for _ref, m in run.find(RE_CFG_INACTIVE):
        bucket = dead.setdefault(m["cluster"] or "", set())
        bucket.update(n.strip() for n in m["fns"].split(",") if n.strip())
    live: dict[str, int] = {}
    refs: list[str] = []
    for ref, m in hits:
        names = [n.strip() for n in m["fns"].split(",") if n.strip()]
        _dead_here = dead.get(m["cluster"] or "", set()) | dead.get("", set())
        alive = [n for n in names if n not in _dead_here]
        if not alive:
            continue
        refs.append(ref)
        for n in alive:
            live[n] = live.get(n, 0) + 1
    if not live:
        return []
    worst = sorted(live.items(), key=lambda kv: (-kv[1], kv[0]))
    return [{
        "evidence": f"{len(refs)} iteration(s) left {len(live)} live frontier "
                    "stub(s) undispatched by every route (cfg-inactive stubs "
                    "excluded) — "
                    + ", ".join(f"{n} x{c}" for n, c in worst[:4]),
        "refs": refs[:4]}]


def detect_p11(run: Run, iters: list[dict]) -> list[dict]:
    """Discarded answer: the model returned FN tags and the reader produced
    fewer records than it carried — the fourth per-fill class, and the only one
    of the four that is ALWAYS a pipeline defect rather than a judgement call.

    WHY IT IS ITS OWN CLASS. Every other detector here reads the loop's
    decisions. This one reads a call that never became a decision at all: the
    reply is discarded, the unit is logged as unanswered, and the sweep that
    contains it is indistinguishable from one where the model said nothing. It
    cannot be found from the pass count, and the escalation, the retro store
    and the futility gates will all subsequently reason about the unit as
    though it had refused to answer.

    WHY A CENSUS ROW AND NOT A LOG LINE. The log line already existed. It
    printed 97 times across the retained logs, 37 of them with a nonzero tag
    count, on `src/context.rs::StdErrorforContextError<C,Error>` (the unit the
    record has convicted since 2026-08-14), `src/error.rs::Error`,
    `src/chain.rs::ExactSizeIteratorforChain` and `src/kind.rs::Trait` — and it
    was read as a defect none of those times, because nothing aggregated it.
    Six distinct reader shapes were found between 2026-08-04 and 2026-08-22 and
    each cost its own investigation; this row is what makes the seventh a
    lookup.

    NO REACTION KEY, deliberately. There is nothing the loop can do about a
    discarded answer at run time — the correct response is always a reader fix
    — so every hit is backlog until the shape is closed, and marking it
    REACTED would say the opposite."""
    hits = [(ref, m) for ref, m in run.find(RE_TAGS_DROPPED)
            if int(m["tags"]) > 0]
    losses = list(run.find(RE_READER_LOSS))
    if not hits and not losses:
        return []
    per_unit: dict[str, int] = {}
    shas: list[str] = []
    refs: list[str] = []
    for ref, m in hits:
        per_unit[m["unit"]] = per_unit.get(m["unit"], 0) + int(m["tags"])
        refs.append(ref)
    for ref, m in losses:
        lost = int(m["tags"]) - int(m["records"])
        per_unit[m["unit"]] = per_unit.get(m["unit"], 0) + lost
        shas.append(m["sha"][:12])
        refs.append(ref)
    worst = sorted(per_unit.items(), key=lambda kv: (-kv[1], kv[0]))
    total = sum(per_unit.values())
    return [{
        "evidence": f"{total} answered body(ies) discarded by the reader over "
                    f"{len(refs)} call(s) on {len(per_unit)} unit(s): "
                    + ", ".join(f"{u} x{c}" for u, c in worst[:4])
                    + (f"; reply sha(s) {', '.join(shas[:3])}" if shas else
                       "; no sha recorded (pre-2026-08-22 log format) — find "
                       "the reply with tools/scan_dropped_fn_responses.sh"),
        "refs": refs[:4]}]


def detect_p12(run: Run, iters: list[dict]) -> list[dict]:
    """Silent complement: the futility escalation fired and the run never once
    offered an escalated producer's INSTALLABLE fns alongside it.

    WHY IT IS ITS OWN CLASS. The pass-92 defect spans two units — a producer
    that builds a fn-pointer table, and the free fns the table names — and
    every intermediate state scores identically, so the loop can only climb it
    when both halves are writable in ONE prompt. The mechanism that makes the
    second half writable (`domain/installable_fns.py`) is gated on the first
    having fired, so a run in which the fire is logged and the offer never is
    has spent every escalated batch on half a defect. Run 55484-84c991f1
    (2026-08-22): six fires at iter 2, the constructors graded ESCALATED in
    every `src/error.rs` prompt from iter 3, the per-type vtable wiring landed
    at iter 3 — and `context_downcast` stayed `Ref::from_raw(NonNull::dangling())`
    because the offer returned () on a type-text spacing mismatch. Five flat
    sweeps, and the census filed the run as a healthy negative control.

    A run that predates the offer line (before 2026-08-22) hits too, and that
    reading is correct: the complement was never offered in it either.

    NO REACTION KEY: the loop has nothing to do about it at run time — the
    offer is a deterministic computation, and a miss is always a defect in
    it (or a crate with no fn-pointer table, which this crate is not)."""
    fires = list(run.find(RE_ESCALATE))
    if not fires:
        return []
    if list(run.find(RE_INSTALLABLE_OFFERED)):
        return []
    tests = sorted({m["test"] for _, m in fires})
    return [{
        "evidence": f"{len(fires)} futility fire(s) for {len(tests)} test(s) "
                    f"({', '.join(tests[:4])}) and 0 installable-complement "
                    "offers in the run — every escalated producer was "
                    "dispatched without the free fns its table names, so the "
                    "split defect was never writable in one prompt",
        "refs": [ref for ref, _ in fires[:4]]}]


def _diag_symbols(msg: str) -> tuple[str, ...]:
    """The backticked symbols of a diagnostic's first line — `Error:
    core::error::Error` out of "the trait bound `Error: core::error::Error`
    is not satisfied".  The log prints only the diagnostic's HEADER line, so
    the called symbol of E0277's "required by a bound in ..." note is not
    recoverable here; (unit, code, header symbols) identifies the class just
    as uniquely, and hashes the 2026-05 per-fn shape and the 2026-08
    instructed shape to the same key.  Falls back to the whole message when
    the header carries no backticks."""
    syms = tuple(re.findall(r"`([^`]+)`", msg))
    return syms if syms else (msg.strip(),)


def _compliance_seqs(
        run: Run) -> dict[str, list[tuple[str, tuple, str]]]:
    """Per unit, in log order: (ref, (code, symbols), attempt-label) for every
    failed attempt whose primary diagnostic the log carries.  Units interleave
    in the log, so consecutiveness is judged per unit, never per line."""
    seqs: dict[str, list[tuple[str, tuple, str]]] = defaultdict(list)
    pending: dict[str, str] = {}   # unit -> attempt n awaiting its first diag
    for ref, text in run.lines:
        if (m := RE_ATTEMPT_FAIL.search(text)):
            pending[m["unit"]] = m["n"]
        elif (m := RE_ATTEMPT_DIAG.search(text)) and m["unit"] in pending:
            seqs[m["unit"]].append(
                (ref, (m["code"], _diag_symbols(m["msg"])),
                 pending.pop(m["unit"])))
        elif (m := RE_FILE_REVERT_DIAG.search(text)):
            seqs[m["unit"]].append(
                (ref, (m["code"], _diag_symbols(m["msg"])), "file"))
    return seqs


def detect_p14(run: Run, iters: list[dict]) -> list[dict]:
    """Compliance over compiler: >=2 CONSECUTIVE failed attempts of one unit
    with an identical primary diagnostic (code + header symbols).  One
    identical repeat is already the class — the second attempt's prompt
    carried the first attempt's diagnostic and the reply made the same call
    anyway — and the defining instance ran to three (run 57025-6ba652f2,
    `Error::context` instructed into `construct_from_context`'s unsatisfiable
    bound by the write list).  The row names unit, code, and the repeated
    symbols, so the next instance is a lookup, not an investigation."""
    hits = []
    for unit, seq in _compliance_seqs(run).items():
        i = 0
        while i < len(seq):
            j = i
            while j + 1 < len(seq) and seq[j + 1][1] == seq[i][1]:
                j += 1
            if j > i:
                code, syms = seq[i][1]
                hits.append({
                    "evidence": f"{unit}: {j - i + 1} consecutive attempt(s) "
                                f"({', '.join(s[2] for s in seq[i:j + 1])}) "
                                f"failed with the identical {code} on "
                                f"{', '.join(repr(s) for s in syms[:3])} — "
                                "the diagnostic was in the record before "
                                "every attempt after the first",
                    "key": [unit, code, list(syms)],
                    "refs": [s[0] for s in seq[i:j + 1]][:4]})
            i = j + 1
    return hits


RE_ESC_DEFERRED = re.compile(
    r"\[attribution/escalate\] (?P<test>\S+): NOT escalating — unit-set "
    r"unchanged but (?P<n>\d+) attributed unit\(s\) never dispatched since "
    r"it was observed \(parked under the dispatch cap, not served\): "
    r"(?P<units>.+)$")


def detect_p15(run: Run, iters: list[dict]) -> list[dict]:
    """Parked accessor: a failing test whose attributed unit-set is unchanged
    across sweeps because its units are WAITING under the dispatch cap, not
    because rewrites served them.  Before 2026-08-25 the escalation fired on
    set identity alone and declared such a unit futile without a dispatch —
    run 263474-b0e608d3 iter 2, `test_altdebug` escalated past its never-tried
    owner `src/fmt.rs::debug` to the producers of `ErrorImpl`; the accessor
    then met its own batch under a "measured futile" grade.  The loop now
    DEFERS the fire and prints the line matched here, so a hit is the class
    observed and answered: the row counts, per test, how many sweeps its owner
    waited and names the units.  Pre-fix instances leave no log signature (the
    fire line names producers, not the futile units) — they are recoverable
    only from `events.jsonl`: an `escalated` event whose `futile_units` appear
    in no earlier dispatch of the same run."""
    hits: dict[str, dict] = {}
    for ref, m in run.find(RE_ESC_DEFERRED):
        h = hits.setdefault(m["test"], {
            "evidence": "", "key": [m["test"]], "refs": [], "sweeps": 0,
            "units": m["units"].strip()})
        h["sweeps"] += 1
        if len(h["refs"]) < 4:
            h["refs"].append(ref)
    out = []
    for test, h in hits.items():
        h["evidence"] = (f"{test}: escalation deferred {h['sweeps']} sweep(s) "
                         f"— owner(s) parked under the dispatch cap, never "
                         f"served: {h['units']}")
        out.append({k: h[k] for k in ("evidence", "key", "refs")})
    return out


#: Injectable for tests; None means "read the stores under tmp/ once".
P16_UNSTAMPED_UNITS: set[str] | None = None


def _p16_unstamped_units() -> set[str]:
    """Store keys of gate-verdict-derived records carrying no stamp of their
    class (`regressed` for a regression verdict, `crash_stub_sites` for a
    crash / abort verdict) — the population nothing screens (see
    `regression_derived_records`).  Read once per process from the stores
    under the repo's tmp/."""
    global P16_UNSTAMPED_UNITS
    if P16_UNSTAMPED_UNITS is None:
        root = Path(__file__).resolve().parent.parent
        P16_UNSTAMPED_UNITS = {
            r["unit"] for r in regression_derived_records(root)
            if r["stamp"] is None}
    return P16_UNSTAMPED_UNITS


def detect_p16(run: Run, iters: list[dict]) -> list[dict]:
    """Regression-derived prescription, two shapes.  (a) SILENT: an honoured
    line (`[phase-c/retro-honoured]`) on a unit whose store record was earned
    from a regression verdict and carries no `regressed` stamp — the record
    steered the prompt and nothing could have screened it, because the stamp
    the screen reads was never written.  (b) REACTED: the screen's own line,
    `retro guidance withheld (regression_scoped)`, per unit.  The live
    instance is (a): `src/ensure.rs::*impl*::BothDebugfor(A,B)` honoured
    into re-fills for `test_trailer` while its lesson removed the 40-byte
    overflow fallback that test pins.  A unit is keyed by the store key on
    the honoured line and by the block id / fn key on the judge-site lines,
    so the two shapes are reported separately rather than joined."""
    steered: dict[str, list[str]] = defaultdict(list)
    unstamped = _p16_unstamped_units()
    for ref, m in run.find(RE_RETRO_HONOURED):
        if m["unit"] in unstamped:
            steered[m["unit"]].append(ref)
    withheld: dict[str, list[str]] = defaultdict(list)
    for ref, m in run.find(RE_RETRO_REGRESSION_SCOPED):
        withheld[m["unit"]].append(ref)
    hits = []
    for unit, refs in sorted(steered.items(), key=lambda kv: -len(kv[1])):
        hits.append({
            "evidence": f"{unit} steered {len(refs)} prompt(s) on a record "
                        "written from a regression verdict with NO `regressed` "
                        "stamp — nothing screened it",
            "refs": refs[:4]})
    for unit, refs in sorted(withheld.items(), key=lambda kv: -len(kv[1])):
        hits.append({
            "evidence": f"{unit}: regression-scoped guidance withheld "
                        f"{len(refs)}x — the lesson is about tests this unit "
                        "does not answer for",
            "refs": refs[:4]})
    return hits[:6]


RE_FILL_CLASSES = re.compile(
    r"iter \d+: cargo test .*classes: cashed=(?P<cashed>\d+) wrong=(?P<wrong>\d+) "
    r"mis=(?P<mis>\d+) unreachable=(?P<unr>\d+) "
    r"dropped=(?P<drop>\d+) compile=(?P<comp>\d+)(?: declined=(?P<decl>\d+))?")


def detect_p17(run: Run, iters: list[dict]) -> list[dict]:
    """Fill-class histogram — which of cashed/wrong/mis/unreachable/dropped/
    compile dominated a run's dispatched fills.  Pre-emitter logs have no
    signature (no hit).  Nothing is gated on the dominant class this
    code_id; the row exists so the next gate has a measured distribution.
    """
    totals = {"cashed": 0, "wrong": 0, "mis": 0,
              "unreachable": 0, "dropped": 0, "compile": 0, "declined": 0}
    refs: list[str] = []
    n_lines = 0
    for ref, m in run.find(RE_FILL_CLASSES):
        n_lines += 1
        totals["cashed"] += int(m["cashed"])
        totals["wrong"] += int(m["wrong"])
        totals["mis"] += int(m["mis"])
        totals["unreachable"] += int(m["unr"])
        totals["dropped"] += int(m["drop"])
        totals["compile"] += int(m["comp"])
        totals["declined"] += int(m["decl"] or 0)
        if len(refs) < 4:
            refs.append(ref)
    total = sum(totals.values())
    if n_lines == 0 or total == 0:
        return []
    dominant = max(totals, key=lambda k: totals[k])
    pct = 100.0 * totals[dominant] / total
    return [{
        "evidence": (
            f"{n_lines} histogram line(s), {total} fill(s): "
            f"cashed={totals['cashed']} wrong={totals['wrong']} "
            f"mis={totals['mis']} unreachable={totals['unreachable']} "
            f"dropped={totals['dropped']} compile={totals['compile']} "
            f"declined={totals['declined']} "
            f"— dominant {dominant} ({pct:.0f}%)"
        ),
        "refs": refs,
    }]


RE_SESSION_OVER_CAP = re.compile(
    r"\[phase-c/session\] no slice planned \((?P<detail>.*"
    r"(?:hollow, empty, or over SLICE_SESSION_MAX_UNITS"
    r"|over SLICE_SESSION_MAX_UNITS=).*)\)")
# The collapsed three-way is P18; `\d+ over` is the split over-cap clause
# (P21 unless the same fill also expanded a locator). The three-way line
# is `or over`, not `N over`, so the split regex does not match it.
# Computed 2026-09-05 on the rotation: every P18 run also hit P21 while
# both detectors keyed on RE_SESSION_OVER_CAP (5 overlap, 0 P18-only,
# 0 P21-only). Canonical P18 instance 110993-6efa23be has expand_lines=0.
RE_SESSION_COLLAPSED = re.compile(
    r"\[phase-c/session\] no slice planned \((?P<detail>.*"
    r"hollow, empty, or over SLICE_SESSION_MAX_UNITS.*)\)")
RE_SESSION_SPLIT_OVER_CAP = re.compile(
    r"\[phase-c/session\] no slice planned \((?P<detail>.*"
    r"\d+ over SLICE_SESSION_MAX_UNITS=.*)\)")
RE_SESSION_LOCATOR_EXPAND = re.compile(
    r"\[phase-c/session\] locator-only (?P<unit>\S+) expanded to "
    r"(?P<n>\d+) tree site")
# P21 — the over-union refusal's two neighbours: the never-served count on
# the same fill's service-order line, and the locator-expansion line whose
# presence in the SAME fill (service-order resets it) separates this class
# from P18.
RE_SERVICE_NEVER_SERVED = re.compile(
    r"\[phase-c/dispatch\] service order: (?P<never>\d+) never-served unit")

# P19 — a sweep (or the red-build refusal) whose rustc diagnostic carries NO
# error code and sits under src/: rustc codes every type/borrow/resolution
# error and none of its parse errors, so this is the sweep-side signature
# of a file the extractor cannot enumerate — no fill route can open it.
RE_UNCODED_SRC = re.compile(
    r"(?P<file>src/[\w/.\-]+\.rs):(?P<line>\d+): error: (?P<msg>[^`;\]]*)")
# The mechanical writers that run without a model in the loop; the last
# one printed before an uncoded diagnostic is the writer the row names.
RE_MECH_WRITE = re.compile(
    r"(?P<writer>\[phase-c/ledger\] iter \d+: [^—]+— re-seeded"
    r"|\[layout-sync\] reinstalled"
    r"|\[ownership\] (?:seeded|carried|backfill)"
    r"|\[layout-retrigger\] [^\n]*reverted \d+ referencing"
    r"|\[heal\] [^\n]*restored"
    r"|\[phase-c/entry\])")
# How far back (in log lines) a writer line is attributed to the break.
P19_WINDOW = 80
# P20 reads the re-seed events: the multiset is only visible there (the
# log line prints counts, the event prints keys).
EVENTS_PATH = Path(__file__).resolve().parent.parent / "logs" / "events.jsonl"


def _session_refuse_walk(run: Run) -> list[dict]:
    """Fill-scoped refuse walk: a service-order line starts a fill and
    clears the expansion flag. Sticky-12-line windows taint a later fill's
    split refuse with a prior fill's expansion (run 57079-a5f83827 expanded
    at iter 5 and split-refused 405+ lines later after a new service-order).
    """
    out: list[dict] = []
    never = 0
    never_ref = ""
    saw_expand = False
    expand_ref = ""
    for ref, text in run.lines:
        if (m := RE_SERVICE_NEVER_SERVED.search(text)):
            never = int(m["never"])
            never_ref = ref
            saw_expand = False
            expand_ref = ""
            continue
        if RE_SESSION_LOCATOR_EXPAND.search(text):
            saw_expand = True
            expand_ref = ref
            continue
        collapsed = RE_SESSION_COLLAPSED.search(text)
        split = RE_SESSION_SPLIT_OVER_CAP.search(text)
        if not collapsed and not split:
            continue
        detail = (collapsed or split)["detail"]
        out.append({
            "ref": ref, "text": text, "detail": detail,
            "never": never, "never_ref": never_ref,
            "saw_expand": saw_expand, "expand_ref": expand_ref,
            "collapsed": collapsed is not None, "split": split is not None,
        })
    return out


def detect_p18(run: Run, iters: list[dict]) -> list[dict]:
    """Locator-collapsed session slice — eligible treated as a unit source so
    a name with no site-frontier container expanded to every tree container,
    the merge exceeded the cap, and plan_slices dropped the group. Also the
    locator-only expansion log after the join, and a split over-cap refuse
    in a fill that also expanded a locator. An empty leftover (`ui` only)
    is not this class. A split `N over` refuse with no same-fill expansion
    is P21. Run 110993-6efa23be (2026-09-03) is the positive instance of the
    collapsed three-way log line.
    """
    hits: list[dict] = []
    refuses = _session_refuse_walk(run)
    collapsed = [r for r in refuses if r["collapsed"]]
    if collapsed:
        hits.append({
            "evidence": (
                f"{len(collapsed)} collapsed hollow/empty/over line(s) — "
                f"{collapsed[0]['detail']}"
            ),
            "refs": [r["ref"] for r in collapsed[:4]],
        })
    split_exp = [r for r in refuses if r["split"] and r["saw_expand"]]
    if split_exp:
        hits.append({
            "evidence": (
                f"{len(split_exp)} over-cap refuse(s) in a fill that also "
                f"expanded a locator — {split_exp[0]['detail']}"
            ),
            "refs": [r["ref"] for r in split_exp[:4]],
        })
    expansions: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for ref, m in run.find(RE_SESSION_LOCATOR_EXPAND):
        expansions[m["unit"]].append((ref, int(m["n"])))
    for unit, rows in sorted(expansions.items()):
        n = rows[0][1]
        hits.append({
            "evidence": (
                f"{unit}: locator-only unit expanded to {n} tree site(s) "
                f"({len(rows)} time(s))"
            ),
            "refs": [r for r, _ in rows[:4]],
        })
    return hits[:6]


def detect_p21(run: Run, iters: list[dict]) -> list[dict]:
    """Over-union session refusal with never-served units waiting.

    HIT: a split `no slice planned … N over SLICE_SESSION_MAX_UNITS=` line
    with no locator-only expansion in the same fill (service-order resets
    the flag; that same-fill case is P18), on a sweep whose service-order
    line counted at least one never-served unit. The collapsed three-way
    (`hollow, empty, or over`) is P18, not this class. Run 2965-453dd7a4
    (2026-09-05) is the positive instance: seven split refusals, never-
    served 7 → 6 → 6 → 0.
    """
    hits: list[dict] = []
    for r in _session_refuse_walk(run):
        if not r["split"] or r["saw_expand"] or r["never"] <= 0:
            continue
        hits.append({
            "evidence": (
                f"session refused an over-cap group while the same sweep "
                f"ranked {r['never']} never-served unit(s) — "
                f"{r['text'].strip()[:120]}"
            ),
            "refs": [r["ref"], r["never_ref"]],
        })
    return hits[:6]


_SESSION_PLAN_EVENTS: dict[str, list[dict]] | None = None


def _session_plan_events_by_run() -> dict[str, list[dict]]:
    """`phase_c.session.plan` events by run — the only record of how many
    leftover tests were hollow on a fill that still opened a session."""
    global _SESSION_PLAN_EVENTS
    if _SESSION_PLAN_EVENTS is not None:
        return _SESSION_PLAN_EVENTS
    out: dict[str, list[dict]] = defaultdict(list)
    try:
        with open(EVENTS_PATH, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if "phase_c.session.plan" not in line:
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                d = dict(ev.get("data") or {})
                d["ts"] = str(ev.get("ts", ""))
                out[str(ev.get("run_id", ""))].append(d)
    except OSError:
        pass
    _SESSION_PLAN_EVENTS = dict(out)
    return _SESSION_PLAN_EVENTS


def detect_p22(run: Run, iters: list[dict]) -> list[dict]:
    """Route starvation — a fill whose session opened FIRST while a stub
    held leftover tests hollow (the plan event's `hollow > 0`) and no stub
    route (file-first / per-fn) had landed anything in that fill. Run
    334902-2774708c iter 11 (2026-09-06 05:23 UTC) is the positive
    instance: hollow=24, three sessions fitted, the 18-test session opened
    at once, the per-fn route never ran before the process was killed.
    Reaction: the schedule line putting a stub-holding run first.
    """
    plans = _session_plan_events_by_run().get(run.run_id, [])
    hollow_plans = [p for p in plans if int(p.get("hollow", 0) or 0) > 0
                    and (int(p.get("fitted", 0) or 0) > 0
                         or p.get("sessions"))]
    if not hollow_plans:
        return []
    hits: list[dict] = []
    # Fill-scoped walk: a service-order line opens a fill; a session
    # opening before any stub-route landing in that fill is the hit.
    stub_landed = False
    fill_ref = ""
    n_plans = 0
    for ref, text in run.lines:
        if RE_SERVICE_NEVER_SERVED.search(text):
            stub_landed = False
            fill_ref = ref
            continue
        if RE_STUB_ROUTE_LANDED.search(text):
            stub_landed = True
            continue
        if RE_SCHEDULE.search(text):
            # The reaction is present on this fill: its order is decided.
            stub_landed = True
            continue
        m = RE_SESSION_OPENING.search(text)
        if not m or stub_landed:
            continue
        if n_plans >= len(hollow_plans):
            break
        plan = hollow_plans[n_plans]
        n_plans += 1
        hits.append({
            "evidence": (
                f"session[{m['tests']} test(s)] opened first while "
                f"{plan.get('hollow')} leftover test(s) were hollow and no "
                "stub route had landed in this fill"),
            "refs": [ref, fill_ref] if fill_ref else [ref],
        })
    return hits[:6]


def detect_p23(run: Run, iters: list[dict]) -> list[dict]:
    """Declined units — the model answered an empty `<FN>` body. Post-fix
    the route prints `[phase-c/declined] …`; pre-fix the only trace is a
    reader-loss line whose reply artefact holds empty tags, which this
    detector opens (run 959654-1e96b403, 2026-09-06: three vtable drop
    helpers, logged as a parser defect).
    """
    hits: list[dict] = []
    for ref, m in run.find(RE_DECLINED):
        hits.append({
            "evidence": (f"{m['route']} {m['unit']}: declined {m['n']} — "
                         f"{m['names'].strip()}"),
            "refs": [ref],
        })
    prompts_dir = EVENTS_PATH.parent / "prompts"
    for ref, m in run.find(RE_READER_LOSS):
        text_line = next((t for r, t in run.lines if r == ref), "")
        pm = RE_READER_LOSS_PATH.search(text_line)
        reply_path = prompts_dir / f"{pm['full'] if pm else m['sha']}.txt"
        try:
            text = reply_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        empty = re.findall(r'<FN name="([^"]+)"[^>]*>\s*</FN>', text)
        if not empty:
            continue
        hits.append({
            "evidence": (f"{m['route']} {m['unit']}: reader-loss line, but "
                         f"{len(empty)} of the {m['tags']} tag(s) were EMPTY "
                         f"({', '.join(empty[:4])}) — declined, not lost"),
            "refs": [ref],
        })
    return hits[:6]


def detect_p19(run: Run, iters: list[dict]) -> list[dict]:
    """Mechanical write left an unparseable tree — an uncoded rustc
    diagnostic under src/ on a sweep or refusal line, attributed to the
    last mechanical-writer line within P19_WINDOW lines before it (or
    reported as an unnamed writer). Run 2973-66e50ad9 (2026-09-04) is the
    positive instance: the cluster-entry re-seed, then three sweeps at
    `src/context.rs:35: error: unexpected closing delimiter`.
    """
    hits: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for i, (ref, text) in enumerate(run.lines):
        m = RE_UNCODED_SRC.search(text)
        if not m:
            continue
        key = (m["file"], m["msg"].strip())
        if key in seen:
            continue
        seen.add(key)
        writer = "writer not named in the preceding lines"
        writer_ref = ""
        for j in range(i - 1, max(-1, i - P19_WINDOW - 1), -1):
            wm = RE_MECH_WRITE.search(run.lines[j][1])
            if wm:
                writer = wm["writer"].strip()
                writer_ref = run.lines[j][0]
                break
        hits.append({
            "evidence": (f"{m['file']}:{m['line']}: error: {m['msg'].strip()} "
                         f"(uncoded = parse-level) after: {writer}"),
            "refs": [r for r in (writer_ref, ref) if r],
        })
    return hits[:6]


_RESEED_EVENTS: dict[str, list[dict]] | None = None


def _reseed_events_by_run() -> dict[str, list[dict]]:
    """`phase_c.ledger.reseed` events grouped by run id, read once."""
    global _RESEED_EVENTS
    if _RESEED_EVENTS is not None:
        return _RESEED_EVENTS
    out: dict[str, list[dict]] = defaultdict(list)
    try:
        with open(EVENTS_PATH, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if "phase_c.ledger.reseed" not in line:
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                out[str(ev.get("run_id", ""))].append(ev.get("data") or {})
    except OSError:
        pass
    _RESEED_EVENTS = dict(out)
    return _RESEED_EVENTS


def detect_p20(run: Run, iters: list[dict]) -> list[dict]:
    """Site-cardinality leak into the re-seed — a `phase_c.ledger.reseed`
    event whose seeded/rejected list repeats a key, i.e. the same unit was
    handed to the writer once per impl site (`src/error.rs::from` x4 on
    run 2973-66e50ad9). Skipped lists repeat legitimately (one row per
    site the gate refused) and are not counted.
    """
    hits: list[dict] = []
    for ev in _reseed_events_by_run().get(run.run_id, []):
        for field in ("seeded", "rejected"):
            keys = list(ev.get(field) or [])
            dup = sorted({k for k in keys if keys.count(k) > 1})
            if dup:
                hits.append({
                    "evidence": (f"{ev.get('site', '?')}: {field} repeats "
                                 f"{', '.join(dup)} (" 
                                 f"{len(keys)} element(s), {len(set(keys))} distinct)"),
                    "refs": [run.start_ref],
                })
    return hits[:6]


# P24 — the crash-cohort lock. Three line shapes and two events. The batch
# line is matched on its stable prefix only (`carry crash evidence this
# sweep`); the wording around it is being extended with frame/chain counts,
# so K, M, the iter and the name list are each pulled by their own search
# and default to None when absent. The name list is capped at 8 by the
# driver with no marker, which is why the full batch comes from events.
RE_CRASH_BATCH_PREFIX = "carry crash evidence this sweep"
RE_CRASH_BATCH_K = re.compile(r"(?P<k>\d+) unit\(s\) carry crash evidence this sweep")
RE_CRASH_BATCH_M = re.compile(r"(?P<m>\d+) of them are in this batch")
RE_CRASH_BATCH_NAMES = re.compile(r"of them are in this batch: (?P<names>src/.+?)\s*$")
RE_CRASH_BATCH_ITER = re.compile(r"\[phase-c/tdd\] iter (?P<iter>\d+):")
RE_CHAIN_DERIVED = re.compile(
    r"\[phase-c/crash\] iter (?P<iter>\d+):.*?synthesized CHAIN-DERIVED blame"
    r".*? for (?P<k>\d+) fn\(s\)")
# The iter summary's dispatch suffix: `dispatching N of F (D/F this pass`.
# D is attempted_before + N, so the cursor position the event records as
# `attempted_before` is D - N and needs no event to read.
RE_DISPATCHING = re.compile(
    r"dispatching (?P<n>\d+) of (?P<of>\d+) \((?P<done>\d+)/\d+ this pass")

_COHORT_EVENTS: dict[str, dict[str, dict[int, list]]] | None = None


def _cohort_events_by_run() -> dict[str, dict[str, dict[int, list]]]:
    """Per run: `batch[iter]` = the dispatched batch of that sweep as the
    union of `phase_c.fill.schedule` `runs[*].targets` (one entry per
    schedule event, in stream order), and `iter[iter]` = the
    `phase_c.tdd.iter` readings (`attempted_before`, `dispatched`).

    The schedule event is the batch source because it is emitted BEFORE
    the fill runs, so a run killed mid-batch (`1577437-c93d5c26`, exit 143
    inside iter 4's file route) still has it; `phase_c.route.run` is
    written at route END and `phase_c.fn.fill.start` names per-fn units
    only. `phase_c.tdd.iter` carries `dispatched` as a count. Iter numbers
    can restart after a retrigger fire, so each iter keeps a list and the
    detector consumes it in order."""
    global _COHORT_EVENTS
    if _COHORT_EVENTS is not None:
        return _COHORT_EVENTS
    out: dict[str, dict[str, dict[int, list]]] = {}
    try:
        with open(EVENTS_PATH, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if ("phase_c.fill.schedule" not in line
                        and "phase_c.tdd.iter" not in line):
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                d = ev.get("data") or {}
                rid = str(ev.get("run_id", ""))
                slot = out.setdefault(rid, {"batch": {}, "iter": {}})
                it = d.get("iter")
                if not isinstance(it, int):
                    continue
                if ev.get("event") == "phase_c.fill.schedule":
                    names: set[str] = set()
                    for r in d.get("runs") or []:
                        names.update(str(t) for t in (r.get("targets") or []))
                    slot["batch"].setdefault(it, []).append(names)
                elif ev.get("event") == "phase_c.tdd.iter":
                    slot["iter"].setdefault(it, []).append({
                        "attempted_before": d.get("attempted_before"),
                        "dispatched": d.get("dispatched")})
    except OSError:
        pass
    _COHORT_EVENTS = out
    return _COHORT_EVENTS


def _parse_crash_batch(text: str) -> dict | None:
    if RE_CRASH_BATCH_PREFIX not in text:
        return None
    mk = RE_CRASH_BATCH_K.search(text)
    mm = RE_CRASH_BATCH_M.search(text)
    mi = RE_CRASH_BATCH_ITER.search(text)
    mn = RE_CRASH_BATCH_NAMES.search(text)
    names = ([n.strip() for n in mn["names"].split(",") if n.strip()]
             if mn else [])
    return {"k": int(mk["k"]) if mk else None,
            "m": int(mm["m"]) if mm else None,
            "iter": int(mi["iter"]) if mi else None,
            "names": names}


def _cohort_sweeps(run: Run) -> tuple[list[dict], list[tuple[str, int | None, int]]]:
    """Dispatching sweeps in log order, each with its crash batch line (if
    any), the dispatch count and the cursor position; plus the chain-derived
    blame lines as (ref, iter, count)."""
    events = _cohort_events_by_run().get(run.run_id) or {"batch": {}, "iter": {}}
    used_batch: dict[int, int] = defaultdict(int)
    used_iter: dict[int, int] = defaultdict(int)
    sweeps: list[dict] = []
    chain: list[tuple[str, int | None, int]] = []
    pending: dict | None = None
    for ref, text in run.lines:
        if (mc := RE_CHAIN_DERIVED.search(text)):
            chain.append((ref, int(mc["iter"]), int(mc["k"])))
            continue
        if (pb := _parse_crash_batch(text)) is not None:
            pb["ref"] = ref
            pending = pb
            continue
        m = RE_ITER.search(text)
        if not m:
            continue
        it = int(m["iter"])
        md = RE_DISPATCHING.search(text)
        sw: dict = {"iter": it, "ref": ref, "batch": None,
              "dispatched": int(md["n"]) if md else None,
              "attempted_before": (int(md["done"]) - int(md["n"])) if md else None,
              "event_attempted_before": None}
        evs = events["iter"].get(it) or []
        if used_iter[it] < len(evs):
            sw["event_attempted_before"] = evs[used_iter[it]]["attempted_before"]
            used_iter[it] += 1
        if pending is not None and pending["iter"] == it:
            sw["batch"] = pending
            full = events["batch"].get(it) or []
            if used_batch[it] < len(full):
                sw["batch"]["full"] = full[used_batch[it]]
                used_batch[it] += 1
        pending = None
        sweeps.append(sw)
    return sweeps, chain


def _cohort_tail(sw: dict) -> bool:
    """The sweep's crash cohort is larger than the batch it filled: K graded
    units, M of them dispatched, M >= 1, K > M, and the batch held nothing
    else (M >= dispatched when the summary says how many were dispatched).
    K > M with M < dispatched is the `_off` shape — graded units absent from
    the frontier — not a tail waiting under the cap."""
    b = sw["batch"]
    if not b or b["k"] is None or b["m"] is None or b["m"] < 1:
        return False
    if b["k"] <= b["m"]:
        return False
    return sw["dispatched"] is None or b["m"] >= sw["dispatched"]


def _cohort_batch_set(sw: dict) -> tuple[set[str], bool]:
    """The dispatched batch as a name set and whether it is complete: the
    schedule event's union when the stream has it, else the log's names,
    which are complete only when M <= 8 (the driver prints `_aimed[:8]`)."""
    b = sw["batch"]
    full = b.get("full")
    if full:
        return set(full), True
    return set(b["names"]), len(b["names"]) >= (b["m"] or 0)


RE_HUNG_SWEEP = re.compile(
    r"\[phase-c/tdd\] iter (?P<iter>\d+): cargo test "
    r"(?:TIMED OUT \((?P<secs>\d+)s\)\. Stopping\.|"
    r"rc=-1 HUNG in (?P<where>\S+))")
RE_HUNG_COVERAGE = re.compile(
    r"\[coverage\].*hung inside (?P<where>\S+)")


def detect_p25(run: Run, iters: list[dict]) -> list[dict]:
    """A cluster sweep that timed out inside a test. Pre-fix the loop logged
    `cargo test TIMED OUT (Ns). Stopping.`, broke out of the cluster with no
    reading and went on to the next cluster with the hung tree on disk (run
    `3371694-c8835690`, 2026-09-08: stable abandoned at a 91-passing durable
    mark, nightly filled at 0 passing); post-fix the sweep prints `rc=-1
    HUNG in <binary>::<test>` and either restores the durable pair or scores
    the hang as a crash. The fill gates that KEPT the hanging body are the
    other half of the class; their reaction markers are listed too.
    """
    hits: list[dict] = []
    for ref, m in run.find(RE_HUNG_SWEEP):
        where = m["where"] or f"timed out after {m['secs']}s (pre-fix: cluster abandoned)"
        hits.append({"evidence": f"iter {m['iter']}: sweep hung — {where}",
                     "refs": [ref]})
    for ref, m in run.find(RE_HUNG_COVERAGE):
        hits.append({"evidence": f"coverage hung inside {m['where']}",
                     "refs": [ref]})
    return hits


def detect_p24(run: Run, iters: list[dict]) -> list[dict]:
    """Crash-cohort lock: the crash-evidence cohort is larger than the
    dispatch batch and every graded unit is exempt from the pass cursor,
    so the same head is re-bought sweep after sweep and the tail is never
    dispatched. `take_batch(exempt=_crash_grades.keys())` subtracts the
    exempt keys from `attempted` before ranking, and `promote_crash_evidence`
    puts the whole cohort at the front, so with K > cap the pending list
    starts at slot 0 on every sweep — which is exactly what the iter line's
    `(D/F this pass` reads as `attempted_before = D - N = 0` and the
    `phase_c.tdd.iter` event records as `attempted_before: 0`.

    Two grades. LOCKED: consecutive dispatching sweeps both with K > M whose
    later batch is a subset of the earlier one (nothing new dispatched).
    ARMED: the run's last dispatching sweep has K > M and its cursor at 0,
    and no later judged sweep exists — the repeat is unread because the
    process ended inside that batch. A K > M sweep followed by a batch that
    carries a new name is the cursor walking the cohort and is not a hit; a
    K <= M cohort re-dispatched identically (the frame-blame shape, 5 of 5
    every sweep) is the exemption doing what its docstring says and is not
    a hit either.

    Sources, and why. The batch line is matched on the prefix `carry crash
    evidence this sweep`; K, M, iter and names are each searched separately
    so an extended wording (frame/chain counts) still parses. The driver
    prints at most 8 names with no marker, so for M > 8 the batch is the
    union of `phase_c.fill.schedule` `runs[*].targets` for the same run and
    iter — emitted before the fill runs, so a killed run still carries it
    (`phase_c.route.run` is written at route end; `phase_c.tdd.iter` has
    `dispatched` as a count only). With no schedule event the log's names
    stand and the row says the set is partial. The cursor reading comes
    from the summary line and is cross-checked against the event's
    `attempted_before`; the chain-derived line count and each line's fn
    count come from the `synthesized CHAIN-DERIVED blame … for K fn(s)`
    lines, because an inferred cohort is what makes K exceed the cap.

    The recorded instance, run `1577437-c93d5c26` (2026-09-06): iter 2
    `(12/67 this pass` and iter 4 `(12/68 this pass` both read
    attempted_before=0; iter 3 synthesized chain-derived blame for 63 fn(s)
    (the event lists 50 chain keys under `blamed`); iter 4's batch line read
    `63 unit(s) … 12 of them are in this batch` naming 8, the schedule event
    the 12; the process was SIGTERMed 14 minutes into that batch, so the run
    has one K > cap sweep and grades ARMED. P3 records the same run as
    `blamed NOBODY x6` and stops there.
    """
    sweeps, chain = _cohort_sweeps(run)
    if not sweeps:
        return []
    resets = [s for s in sweeps
              if s["attempted_before"] == 0 and (s["dispatched"] or 0) > 0]
    reset_txt = ("cursor attempted_before=0 at iter(s) "
                 + ", ".join(str(s["iter"]) for s in resets)
                 if resets else "no dispatching sweep read attempted_before=0")
    disagree = [s for s in resets if s["event_attempted_before"] not in (None, 0)]
    if disagree:
        reset_txt += (" (event disagrees at iter(s) "
                      + ", ".join(str(s["iter"]) for s in disagree) + ")")
    chain_txt = (f"{len(chain)} chain-derived blame line(s): "
                 + "; ".join(f"iter {it} for {k} fn(s)" for _r, it, k in chain)
                 if chain else "no chain-derived blame line")

    def _head(sw: dict) -> str:
        names, complete = _cohort_batch_set(sw)
        shown = sorted(names)
        txt = ", ".join(shown[:6]) + (" …" if len(shown) > 6 else "")
        return (f"{len(names)} unit(s){'' if complete else ' (partial: log names only)'}"
                f": {txt}")

    hits: list[dict] = []
    i = 0
    while i < len(sweeps):
        sw = sweeps[i]
        if not _cohort_tail(sw):
            i += 1
            continue
        base, _complete = _cohort_batch_set(sw)
        run_chain = [sw]
        j = i + 1
        while j < len(sweeps) and _cohort_tail(sweeps[j]):
            nxt, _c = _cohort_batch_set(sweeps[j])
            if not nxt or not nxt <= base:
                break
            run_chain.append(sweeps[j])
            base = nxt
            j += 1
        first, last = run_chain[0], run_chain[-1]
        refs = [first["batch"]["ref"], first["ref"]]
        if len(run_chain) >= 2:
            refs += [last["batch"]["ref"], last["ref"]]
            hits.append({
                "grade": "LOCKED",
                "head": sorted(base),
                "evidence": (
                    f"LOCKED iters {first['iter']}->{last['iter']}: crash cohort "
                    f"{first['batch']['k']}/{last['batch']['k']} unit(s) > batch "
                    f"{first['batch']['m']}, the head re-dispatched on "
                    f"{len(run_chain)} consecutive sweep(s) (nothing new dispatched), "
                    f"tail never reached — head {_head(last)}; {reset_txt}; "
                    f"{chain_txt}"),
                "refs": refs + [r for r, _i, _k in chain[:1]]})
            i = j
            continue
        if j >= len(sweeps) and sw["attempted_before"] == 0:
            hits.append({
                "grade": "ARMED",
                "head": sorted(base),
                "evidence": (
                    f"ARMED iter {sw['iter']}: crash cohort {sw['batch']['k']} "
                    f"unit(s) > batch {sw['batch']['m']}, the batch is the cohort "
                    f"head ({_head(sw)}) and the cursor read attempted_before=0 "
                    f"(the exemption zeroed the pass cursor over the cohort); "
                    f"{reset_txt}; {chain_txt}; no later judged sweep — the run "
                    f"ended inside this batch, so the repeat is unread"),
                "refs": refs + [r for r, _i, _k in chain[:1]]})
        i = j if j > i else i + 1
    return hits[:4]


_P26_START = (
    "phase_c.session.turn", "phase_c.fn.fill.done", "phase_c.fill.reading")
_P26_AUDIT = "flow_audit.report"
# 5x PHASE_C_TEST_TIMEOUT default. 3x would tag a bounded sequential
# recovery of several timed-out `--exact` runs as this class.
_P26_GAP_SEC = 600.0


def detect_p26(run: Run, iters: list[dict]) -> list[dict]:
    """Unbounded wait inside a fill: a session turn / per-fn done / fill
    reading, then no non-audit event for longer than 5x the sweep timeout.

    Pre-fix this is a hit (run `3500465-67c40e78`: `phase_c.session.turn`
    at 18:00:14 UTC, then silence while coverage waited on `lib_gen_1`).
    Post-fix the bound converts it into a P25 coverage hang
    (`phase_c.coverage.hung` within 120s). A long LLM call is not this:
    it opens with `llm.request`, which is not a start event.
    """
    events: list[tuple[float, str]] = []
    try:
        with open(EVENTS_PATH, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if str(ev.get("run_id", "")) != run.run_id:
                    continue
                ts = _p26_ts(ev.get("ts"))
                if ts is None:
                    continue
                events.append((ts, str(ev.get("event", ""))))
    except OSError:
        return []
    events.sort()
    hits: list[dict] = []
    for i, (ts, name) in enumerate(events):
        if name not in _P26_START:
            continue
        nxt = next((t for t, n in events[i + 1:] if n != _P26_AUDIT), None)
        gap = (nxt if nxt is not None else _p26_now()) - ts
        if gap >= _P26_GAP_SEC:
            hits.append({
                "evidence": (
                    f"{name} at {ts:.0f} then {gap:.0f}s with no non-audit "
                    "event (unbounded wait)"),
                "refs": [run.start_ref]})
            break
    return hits


def _p26_ts(raw: object) -> float | None:
    if not raw:
        return None
    text = str(raw)
    try:
        from datetime import datetime
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


RE_ORPHANED_STUBS = re.compile(
    r"WARNING: all tests passed, but (?P<n>\d+) fn\(s\) are STILL unimplemented")


def detect_p27(run: Run, iters: list[dict]) -> list[dict]:
    """The end gate's orphaned-stub exit after a green suite. Evidence names
    the stubs from the indented lines that follow the warning."""
    hits: list[dict] = []
    for i, (ref, text) in enumerate(run.lines):
        m = RE_ORPHANED_STUBS.search(text)
        if not m:
            continue
        names: list[str] = []
        for _r, nxt in run.lines[i + 1:i + 1 + int(m["n"])]:
            s = nxt.strip()
            if not s.startswith("src/"):
                break
            names.append(s.split(" — ", 1)[0])
        hits.append({"evidence": (f"green suite, exit on {m['n']} orphaned stub(s): "
                                  + ", ".join(names[:8])),
                     "refs": [ref]})
    return hits


RE_PEEL_NONE = re.compile(
    r"\[phase-c/file\] (?P<file>\S+): peel refused \(none_implicated\)")


def detect_p28(run: Run, iters: list[dict]) -> list[dict]:
    """A whole-file revert after the peel found no landed body to blame.
    One hit per run, counting the refusals and naming the files."""
    refs: list[str] = []
    files: list[str] = []
    for ref, text in run.lines:
        m = RE_PEEL_NONE.search(text)
        if m:
            refs.append(ref)
            files.append(m["file"])
    if not refs:
        return []
    return [{"evidence": (f"{len(refs)} whole-reply revert(s) on peel refused "
                          f"none_implicated: {', '.join(sorted(set(files)))}"),
             "refs": refs[:3]}]


def _p26_now() -> float:
    import time
    return time.time()


DETECTORS = {"P1": detect_p1, "P2": detect_p2, "P3": detect_p3,
             "P4": detect_p4, "P5": detect_p5, "P6": detect_p6,
             "P7": detect_p7, "P8": detect_p8, "P9": detect_p9,
             "P10": detect_p10, "P11": detect_p11, "P12": detect_p12,
             "P14": detect_p14, "P15": detect_p15, "P16": detect_p16,
             "P17": detect_p17, "P18": detect_p18,
             "P19": detect_p19, "P20": detect_p20, "P21": detect_p21,
             "P22": detect_p22, "P23": detect_p23, "P24": detect_p24,
             "P25": detect_p25, "P26": detect_p26, "P27": detect_p27,
             "P28": detect_p28}
CLASS_NAMES = {
    "P1": "destroyed-baseline lock", "P2": "blocked-majority spend",
    "P3": "futile aim", "P4": "poisoned evidence", "P5": "limit cycle",
    "P6": "budget lockout", "P7": "sampling starvation",
    "P8": "fire that never lands",
    "P9": "destruction authorised by progress",
    "P10": "undispatchable frontier unit",
    "P11": "discarded answer (reader)",
    "P12": "silent complement (escalation without installable offer)",
    "P14": "compliance over compiler (identical diagnostic across attempts)",
    "P15": "parked accessor (escalation deferred: unit-set unchanged, never dispatched)",
    "P16": "regression-derived prescription (retro written from a REGRESSED verdict)",
    "P17": "fill-class histogram (dominant cashed/wrong/mis/unreachable/dropped/compile)",
    "P18": "locator-collapsed session slice (eligible-as-source exploded past the cap)",
    "P19": "mechanical write left an unparseable tree (uncoded src/ diagnostic on a sweep)",
    "P20": "site-cardinality leak into the re-seed (a unit handed once per impl site)",
    "P21": "over-union session refusal (every test fits the cap, the union does not, never-served units wait)",
    "P22": "route starvation (a session opened first while a stub held leftover tests hollow and no stub route had run this fill)",
    "P23": "declined unit (the model answered an empty body; pre-fix logged as a reader loss)",
    "P24": "crash-cohort lock (cohort > batch and exempt from the cursor: head re-dispatched, tail never reached)",
    "P25": "hung sweep (cargo test timed out inside a test; pre-fix the cluster was abandoned and the hung tree left on disk)",
    "P26": "unbounded wait (a fill event then no non-audit event for longer than 5x the sweep timeout; pre-fix the coverage slice waited forever)",
    "P27": "orphaned stub at a green end (all tests pass, exit 1 on reachable stubs no test executes; augmentation stopped on line coverage alone)",
    "P28": "unpeelable reply (file-first compile failure, peel refused none_implicated, whole reply reverted; pre-fix a bare-block free fn was keyed by the target impl)"}


# -------------------------------------------------------------------- report


def census(paths: list[Path], min_iters: int) -> tuple[list[dict], dict]:
    root = Path(__file__).resolve().parent.parent
    notes = load_ledger_notes(root)
    out = []
    all_runs = split_runs(paths)
    for run in all_runs:
        iters = run.iters()
        row: dict = {
            "run_id": run.run_id, "git": run.git, "code": run.code,
            "started": run.ts, "start_ref": run.start_ref,
            "iterations": len(iters),
            "trajectory": [d["passed"] for d in iters],
            "clusters": sorted({d["cluster"] for d in iters if d["cluster"]}),
            "hits": {}, "reacted": {}, "terminal": [], "note": notes.get(
                run.run_id, ""),
        }
        order = {ref: i for i, (ref, _t) in enumerate(run.lines)}
        for cls, fn in DETECTORS.items():
            found = fn(run, iters)
            if found:
                row["hits"][cls] = found
                first = min((order.get(r, 0) for h in found
                             for r in h["refs"]), default=0)
                row["reacted"][cls] = run.reactions_for(cls, after=first)
        for ref, m in run.find(RE_DEATH):
            row["terminal"].append(
                f"ended after {m['dur']} exit={m['exit']}"
                + (f" signal={m['sig']}" if m["sig"] else "") + f" [{ref}]")
        for ref, text in run.lines:
            for t in TERMINALS:
                if t in text:
                    row["terminal"].append(f"{t} [{ref}]")
                    break
        row["terminal"] = row["terminal"][:3]
        if len(iters) >= min_iters:
            out.append(row)
    return out, chronics(all_runs)


def chronics(runs: list[Run]) -> dict[str, list[dict]]:
    """Cross-run recurrences — the pattern the per-run detectors cannot see.
    A pathology repaired live and reproduced by the NEXT run (the user's
    'restart from scratch and it fails again') shows up here as the same
    escalation set, the same convicted unit, or the same fired component
    recurring across distinct run ids.  Each row lists the runs, oldest
    first; >=2 runs qualifies because every restart between them was a fresh
    chance to behave differently."""
    esc: dict[tuple[str, str], list[str]] = defaultdict(list)
    conv: dict[str, list[str]] = defaultdict(list)
    fired: dict[str, list[str]] = defaultdict(list)
    nobody: dict[str, int] = defaultdict(int)
    compl: dict[tuple, list[str]] = defaultdict(list)
    heads: dict[str, list[str]] = defaultdict(list)
    for run in runs:
        seen_e, seen_c, seen_f = set(), set(), set()
        for h in detect_p14(run, []):
            key = json.dumps(h["key"])
            if run.run_id not in compl[key]:
                compl[key].append(run.run_id)
        # The same cohort head bought under the cap by more than one process:
        # a relaunch on the same tree re-derives the same inference and the
        # same exemption, so the head recurs across run ids without any run
        # ever reaching the tail.
        for h in detect_p24(run, []):
            key = ", ".join(h["head"])
            if key and run.run_id not in heads[key]:
                heads[key].append(run.run_id)
        for _ref, m in run.find(RE_ESCALATE):
            key = (m["test"], m["head"].strip())
            if key not in seen_e:
                esc[key].append(run.run_id)
                seen_e.add(key)
        for _ref, m in run.find(RE_EXH_CONVICTED):
            fn = m["fn"].rstrip(":")
            if fn not in seen_c:
                conv[fn].append(run.run_id)
                seen_c.add(fn)
        for _ref, m in run.find(RE_FIRED):
            if m["comp"] not in seen_f:
                fired[m["comp"]].append(run.run_id)
                seen_f.add(m["comp"])
        if run.find(RE_NOBODY):
            nobody[run.run_id] = len(run.find(RE_NOBODY))
    return {
        "escalation": [
            {"what": f"{t} -> {h[:70]}", "runs": r}
            for (t, h), r in sorted(esc.items(), key=lambda kv: -len(kv[1]))
            if len(r) >= 2][:12],
        "conviction": [
            {"what": fn, "runs": r}
            for fn, r in sorted(conv.items(), key=lambda kv: -len(kv[1]))
            if len(r) >= 2][:12],
        "component": [
            {"what": comp, "runs": r}
            for comp, r in sorted(fired.items(), key=lambda kv: -len(kv[1]))
            if len(r) >= 2][:12],
        "frame_blind": [
            {"what": f"blamed NOBODY x{n}", "runs": [rid]}
            for rid, n in nobody.items()],
        # The same (unit, code, symbols) repeating identically across RUNS is
        # the model reaching for the same wrong call under different prompts
        # and different code — the strongest evidence that a prompt input
        # (instruction, lesson, or its own prior) is steering it, and the key
        # that hashes the 2026-05 unprompted shape and the 2026-08 instructed
        # shape together.
        "compliance": [
            {"what": " ".join(str(x) for x in json.loads(k)), "runs": r}
            for k, r in sorted(compl.items(), key=lambda kv: -len(kv[1]))
            if len(r) >= 2][:12],
        "cohort_head": [
            {"what": k[:110] + (" …" if len(k) > 110 else ""), "runs": r}
            for k, r in sorted(heads.items(), key=lambda kv: -len(kv[1]))
            if len(r) >= 2][:12],
    }


def print_report(rows: list[dict]) -> None:
    agg: dict[str, list[tuple[str, bool]]] = defaultdict(list)
    healthy = []
    for row in rows:
        traj = row["trajectory"]
        head = (f"run {row['run_id']}  git={row['git']} code={row['code']}  "
                f"{row['started']}  iters={row['iterations']}")
        print(head)
        if traj:
            print(f"  pass: {' '.join(str(p) for p in traj[:20])}"
                  + (" …" if len(traj) > 20 else "")
                  + f"  (max={max(traj)}, final={traj[-1]})")
        if not row["hits"]:
            healthy.append(row["run_id"])
            print("  no pattern hits (negative control)")
        for cls, found in row["hits"].items():
            reacted = row["reacted"].get(cls) or []
            tag = ("REACTED(" + ",".join(n for n, _ in reacted) + ")"
                   if reacted else "SILENT")
            agg[cls].append((row["run_id"], bool(reacted)))
            for h in found:
                print(f"  {cls} {CLASS_NAMES[cls]:<24} {tag:<28} "
                      f"{h['evidence']}")
                print(f"      {' '.join(h['refs'])}")
        for t in row["terminal"]:
            print(f"  terminal: {t}")
        if row["note"]:
            print(f"  ledger note: {row['note'][:160]}")
        print()
    print("=" * 72)
    print("aggregate (hits / SILENT among them) — SILENT rows are the backlog")
    for cls in DETECTORS:
        rows_ = agg.get(cls, [])
        silent = [r for r, ok in rows_ if not ok]
        print(f"  {cls} {CLASS_NAMES[cls]:<24} hits={len(rows_):<3} "
              f"silent={len(silent):<3} {('runs: ' + ', '.join(silent[:6])) if silent else ''}")
    print(f"  healthy negative controls: {', '.join(healthy) or 'none'}")


def immortal_accusations(root: Path) -> list[dict]:
    """P13 — `[regression]` notes on disk that carry no body, per FR file.

    A `[regression]` note says "THIS body broke those tests" and the reader
    that retires it (`is_stale_regression_note`) works by comparing the
    recorded body against the file's current one. A note whose body is missing
    therefore states a claim that cannot be checked, cannot be acted on, and
    can never be retired — it is injected into every prompt naming that file
    for the rest of the crate's life. Measured 2026-08-22 on
    `tmp/anyhow/functional_requirements_anyhow.md`: 29 `[regression]` notes, 14
    body-less, and the two that mattered (`construct_from_adhoc`,
    `construct_from_display`) sat in the same prompt that graded those units
    ESCALATED and ordered them rewritten.

    This is standing state, not a run event, so it is printed once rather than
    per run. It is NOT covered by `run_health`'s POISONED_NOTES finding, which
    counts only the other class (a note whose recorded aggregate did not drop).

    A nonzero count after the round-trip fix means notes written BEFORE it are
    still on disk (the fix is reader-side, so they are now retired from prompts
    but remain in the file) — or that the writer has regressed again, which the
    fn names and their dates distinguish."""
    sys.path.insert(0, str(root))
    from pipeline_core.domain.model_notes import (  # noqa: PLC0415
        parse_note_bullets,
        regression_note_body_probe,
        regression_note_subject,
        slice_model_notes,
    )
    out: list[dict] = []
    for fr in sorted(root.glob("tmp/*/functional_requirements_*.md")):
        try:
            _, section = slice_model_notes(fr.read_text(encoding="utf-8"))
        except OSError:
            continue
        notes = parse_note_bullets(section)
        regressions = [n for n in notes if n.lstrip().startswith("[regression]")]
        # A multi-body regression EVENT note ("Bodies filled this pass: …;
        # no single body is accused") is body-less BY DESIGN — it accuses
        # nobody, so it is not this class. Counting it here would make the
        # detector fire on the very writer change that removed the shotgun
        # accusations it was built to count.
        bodyless = [n for n in regressions
                    if not regression_note_body_probe(n)
                    and "no single body is accused" not in n]
        if not regressions:
            continue
        out.append({
            "file": str(fr.relative_to(root)),
            "notes": len(notes), "regressions": len(regressions),
            "bodyless": len(bodyless),
            "fns": sorted({regression_note_subject(n) or "?" for n in bodyless}),
        })
    return out


def print_immortal_accusations(rows: list[dict]) -> None:
    print()
    print("=" * 72)
    print("P13 immortal accusation — [regression] notes on disk with no body,")
    print("which no reader can retire and every prompt for that file carries.")
    if not rows:
        print("\n  no FR file with [regression] notes found")
        return
    for r in rows:
        print(f"\n  {r['file']}: {r['notes']} note(s), "
              f"{r['regressions']} [regression], {r['bodyless']} BODY-LESS")
        if r["fns"]:
            print(f"    subjects: {', '.join(r['fns'])}")


# The openers of the two gate verdicts a compiled attempt earns from a test
# process that died under it: the per-fn abort renderer
# (`cargo_runner._render_u2_abort`) and the regression detail's crash head
# (`test_results.render_regression_detail`).  A record opening on either is
# a CRASH-EARNED prescription: the lesson explains an abort/crash, which on a
# tree with a hollow drop path is the stubs' doing, not the body's.  The
# stamp the screen reads for this class is `crash_stub_sites`
# (`retro_gate.CRASH_STUB_SITES`); the regression class's stamp is
# `regressed`.  Both live records of run `2585062-494e630e` (2026-09-08) —
# `src/ptr.rs::new::Own<T>` ("Your body ABORTED the test process…",
# `fully_exhausted: true`) and `src/ptr.rs::*impl*::Own<T>` ("This made
# test binaries […] CRASH before reporting results…") — printed as
# `no regression-derived record in any store` before this class existed,
# because the detector keyed on `regressed`, which is `[]` for a crash.
_CRASH_VERDICT_HEADS = (
    "Your body ABORTED the test process",
    "This made test binar",
)


def regression_derived_records(root: Path) -> list[dict]:
    """P16 standing state — the records on disk earned from a GATE verdict
    on a compiled attempt: `error_count` 0 (the lesson came from test
    outcomes alone) and a `last_error_preview` opening on one of the two
    verdict shapes.  ``kind`` is `regression` (the `REGRESSED
    previously-passing test(s)` head `render_regression_detail` writes;
    stamp `regressed`) or `crash` (the abort / crash heads above; stamp
    `crash_stub_sites`).  Per record: the store, the unit key, the kind,
    the failing-test count and `convicted_by` it carries, whether it is
    `fully_exhausted`, the class's ``stamp`` (a list, or None when the
    record predates the stamp — the population the screen cannot see; the
    legacy ``regressed`` key mirrors it for regression rows), and the
    guidance head.  Measured on
    `tmp/anyhow/.phase_c_logs/retrospectives.json` 2026-08-26: 5 of 23
    compiled records, all `convicted_by []`, none stamped; 2026-09-08: the
    two `src/ptr.rs` crash-earned records, one of them `fully_exhausted`,
    neither stamped."""
    out: list[dict] = []
    for store in sorted(root.glob("tmp/*/.phase_c_logs/retrospectives.json")):
        try:
            data = json.loads(store.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        for key, rec in sorted(data.items()):
            if not isinstance(rec, dict):
                continue
            if int(rec.get("error_count") or 0) != 0:
                continue
            preview = str(rec.get("last_error_preview") or "")
            if "REGRESSED previously-passing test(s)" in preview:
                kind, stamped = "regression", rec.get("regressed")
            elif preview.startswith(_CRASH_VERDICT_HEADS):
                kind, stamped = "crash", rec.get("crash_stub_sites")
            else:
                continue
            stamp = ([str(t) for t in stamped]
                     if isinstance(stamped, list) else None)
            out.append({
                "store": str(store.relative_to(root)),
                "unit": key,
                "kind": kind,
                "failing_tests": int(rec.get("failing_test_count") or 0),
                "convicted_by": [str(t) for t in (rec.get("convicted_by") or [])],
                "exhausted": bool(rec.get("fully_exhausted")),
                "stamp": stamp,
                "regressed": stamp if kind == "regression" else None,
                "guidance": " ".join(str(rec.get("guidance") or "").split())[:100],
            })
    return out


def print_regression_derived(rows: list[dict]) -> None:
    print()
    print("=" * 72)
    print("P16 gate-verdict prescription — retro records on disk earned from a")
    print("REGRESSED verdict (stamp `regressed`) or from a CRASH / ABORT verdict")
    print("(stamp `crash_stub_sites`); UNSTAMPED ones are screened by nothing.")
    if not rows:
        print("\n  no regression- or crash-derived record in any store under tmp/")
        return
    by_store: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_store[r["store"]].append(r)
    for store, recs in by_store.items():
        unstamped = [r for r in recs if r["stamp"] is None]
        print(f"\n  {store}: {len(recs)} record(s), {len(unstamped)} UNSTAMPED")
        for r in recs:
            field = "regressed" if r["kind"] == "regression" else "crash_stub_sites"
            stamp = ("UNSTAMPED" if r["stamp"] is None
                     else f"{field}[{len(r['stamp'])}]")
            print(f"    {r['unit']}: kind={r['kind']} "
                  f"failing_tests={r['failing_tests']} "
                  f"convicted_by={r['convicted_by']} "
                  f"exhausted={r['exhausted']} {stamp}")
            print(f"      guidance: {r['guidance']}")


def print_chronics(ch: dict) -> None:
    print()
    print("=" * 72)
    print("CROSS-RUN CHRONICS — the same thing recurring across restarts;")
    print("every row survived at least one relaunch, so no per-run mechanism")
    print("owns it.  These are the strongest candidates for the diagnostician.")
    titles = {
        "escalation": "identical escalation (test -> producer set) in >=2 runs",
        "conviction": "same unit skipped-under-conviction in >=2 runs",
        "component": "same component re-authored in >=2 runs",
        "frame_blind": "runs where the localizer blamed NOBODY",
        "compliance": "same identical-diagnostic repeat (unit code symbols)"
                      " in >=2 runs",
        "cohort_head": "same crash-cohort head bought under the cap in >=2 runs",
    }
    for key, title in titles.items():
        entries = ch.get(key) or []
        if not entries:
            continue
        print(f"\n  {title}:")
        for e in entries:
            print(f"    {e['what']}")
            print(f"      runs: {', '.join(e['runs'])}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("logs", nargs="*", type=Path)
    # Default 1, not 2. At 2 the census skipped exactly the runs P7 is about:
    # a run that swept once is not "a launch that died in setup", it is a run
    # whose whole life fit inside one iteration — and on 2026-08-15 that hid
    # both `500301-6a66bf86` and `669347-cf33eae3`, the two most recent runs
    # and the ones whose tree every current number describes. A launch that
    # never reached phase-c still has 0 iterations and is still skipped.
    ap.add_argument("--min-iters", type=int, default=1)
    ap.add_argument("--json", type=Path)
    args = ap.parse_args()
    paths = args.logs or default_logs()
    missing = [p for p in paths if not p.exists()]
    if missing:
        print(f"missing: {missing}", file=sys.stderr)
        return 2
    rows, chron = census(paths, args.min_iters)
    root = Path(__file__).resolve().parent.parent
    immortal = immortal_accusations(root)
    regression_derived = regression_derived_records(root)
    print_report(rows)
    print_chronics(chron)
    print_immortal_accusations(immortal)
    print_regression_derived(regression_derived)
    if args.json:
        args.json.write_text(json.dumps(
            {"runs": rows, "chronics": chron,
             "immortal_accusations": immortal,
             "regression_derived": regression_derived}, indent=1))
        print(f"json written: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
