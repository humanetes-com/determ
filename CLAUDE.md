# CLAUDE.md — conventions for code written in this repo

## Comments and docstrings must stand alone

**Never reference a tracker ID in source, tests, prompts, log lines or event
payloads.** That means no `debt 91.2`, no `debt #46 point 2`, no `FR-018`, no
`plan #77 T1`, no `#82`, and no bare `91.5)` shorthand.

**Why.** The person reading the file has the repository and nothing else. The
`.md` trackers are not distributed with it — `technical_debt.md`,
`handover.md` and the plan files are untracked working notes, and several have
been rewritten or archived since the IDs in the code were written. A number
that resolves to nothing is worse than no citation: it reads as a promise of
context that cannot be redeemed, and it invites the next reader to go looking
for a file that is not there.

**Instead, write the fact.** Everything a tracker entry would have supplied
belongs in the comment itself, in whatever length the point deserves:

- what the defect was, in terms of this code;
- the observation that establishes it — a measured count, a log line, a date,
  a run id, a file and symbol;
- why the fix is shaped the way it is, and what it deliberately does not do.

```python
# WRONG
# Cap the producer set (debt 91.2).

# RIGHT
# Escalating on a generic parameter names every type and therefore none:
# `impl<E> StdError for E` yielded the subject `E`, which matched 33 of the
# crate's 186 fns on the live tree (2026-08-01). Only names the crate declares
# as a struct or enum are answerable.
```

Run ids, dates, file paths, symbol names and measured numbers are all
encouraged — they are verifiable from the repository or from the logs the
repository produces. Session numbers ("session 308") are tracker references by
another name; leave them out too.

**On touch, scrub.** When you edit a file that already carries such a
reference, remove it and replace it with the fact it stood for. The trackers
keep their own numbering; the code does not mirror it.

## A deliberate destruction invalidates a measurement for EVERY consumer of it

Several mechanisms here destroy progress **on purpose**: the crash/layout
retrigger reverts filled bodies to `unimplemented!()`, the batch ratchet restores
an older tree, the merge replaces a cluster's file. Each one leaves a score behind
that no longer describes the tree. Every gate reading that score has to be told.
The gates nobody tells do not merely mis-report — **a gate that governs a RETRY
becomes a permanent lock, because the thing that would unlock it is the progress
the destruction just removed.**

**The instance that cost two days.** Run `259637-36db9aad` (2026-08-07, cluster
`stable`) resumed a tree at 81 passing. The crash retrigger fired attempt 2 of a
cap of 2, re-authored the `Error|ErrorImpl|ErrorVTable|Own|Ref|construct*|
object_*|vtable` component and reverted **56 of 178 filled bodies**. The next
sweep read 21. Three consumers of the 81 knew the drop was deliberate, and each
says so in the log:

- the batch ratchet — `breach (pass_count 21<81) NOT charged: this sweep measures
  the layout retrigger's deliberate re-stub of 56 body(ies)`;
- the stall budget — `state.stalls = 0` every iteration while
  `RefillGrace.bodies_in_flight`;
- the stub-progress exemption — a sweep that strictly shrank the stub set is not a
  plateau.

The fourth consumer was never told. `readmission_warranted` (`domain/refine.py`)
releases the disk-persisted `fully_exhausted` flags only on
`best_metric > metric_at_last_reset`, and `best_pass` in `run_phase_c_tdd` is a
monotone high-water mark — still **81**, set by the sweep *before* the re-stub. So
from iteration 4 the loop printed `109 fill(s) since the last check moved pass no
further than 81 — NOT re-admitting exhausted fn(s)` on every sweep and never
re-admitted anything again. By then `.phase_c_logs/retrospectives.json` had
convicted the only two units that can reach the ~60 missing tests —
`src/error.rs::*impl*::Error` and `src/error.rs::new::Error`. **Passing 81 needed
those two; releasing those two needed passing 81.** Nine hours, 148 landed fills,
+5 tests, and no available exit except the plateau exit at 26/110.

**The generalisation to apply before shipping any exemption.** When you write "do
not charge X, this drop was deliberate", grep for every other reader of the same
quantity in the same breath. Any comparison of the form
`progress_since(baseline)` where `baseline` is a high-water taken before an
intentional destruction is unsatisfiable *exactly when the destruction was
intentional* — which is the case the exemption was written for.

## Diagnose a stalled loop from the frontier, not from the score

The pass count cannot distinguish "filling a producer chain that has not closed
yet" from "locked out of the only fns that matter". The frontier and the exhausted
set can, and both are printed on the `[phase-c/tdd] iter N: cargo test rc=...`
line. Read them as a pair across iterations:

- healthy re-climb — `7445-00ee8a3c`, 2026-08-07: frontier **84 → 26 → 44**,
  `skipped as exhausted` 0 → 50 → 1, and iteration 3's 75-fn file-first batch took
  the score 13 → 83 in one sweep;
- deadlock — `259637-36db9aad`, same day: frontier **63 → 44 → 18 → 17**,
  `skipped as exhausted` 0 → 29 → 24 → 30, fills still landing at 39–109 an
  iteration.

The second tell is what the frontier entries *are*. In that run's last iteration
all **17 of 17** logged `kept ... (compiles; N attributed test(s) blocked by
still-stubbed sibling(s) — deferred to outer loop)`: three hours of model calls
rewriting bodies that already compiled, whose tests cannot run, kept on no
evidence. **When every candidate is blocked, the iteration is being spent on the
blocked instead of on the blockers, and the score movement is noise.**

Corollary for the retrospectives store: a conviction is a claim about a moment.
`src/error.rs::new::Error` was convicted `GENUINELY_HARD` with the guidance "the
vtable functions referenced in the body ... are not implemented in the crate", and
`object_drop`, `object_drop_front`, `object_ref`, `object_boxed`,
`object_downcast`, `object_reallocate_boxed`, `context_drop_rest`,
`context_chain_drop_rest` and `construct` were all filled on disk by the time it
mattered. Check a conviction's stated premise against the tree before believing it.

## A fix that silences a failure deletes whatever guard was keying on the noise

`carry_frozen_repr` (`domain/authored_layout.py`) is correct and it is what made
the above expensive. Before it, the identical re-author dropped
`#[repr(transparent)]`, `tests/test_ffi.rs` stopped compiling, the sweep read 0,
the batch ratchet convicted the tree, reverted 8 files, and **81 came back inside
40 minutes** — the loop was escaping the bad layout *through the crash*. With the
repr carried, the same bad state compiles and scores 21, so the ratchet has
nothing to convict and the loop sits in it for the full 19-iteration grace window.

So: when a change converts a loud failure into a quiet wrong state, find what was
keying on the loudness and ship its replacement **in the same change**. The
question to ask of any such fix is not "is the new state valid" but "what used to
fire here, and what fires now".

## The commands this repo's gate is made of

    bash pre_run.sh                 # check.sh + walks, then stamp the tree
    bash pre_run.sh --walks-only    # walks alone; writes NO stamp, for the fix loop
    bash continue.sh                # refuses (exit 3) unless the stamp matches
    bash continue.sh --no-pre-run   # launch untested, printed loudly
    pytest -m walk                  # the walks alone, outside the gate
    python tools/distill_walk.py    # a run's log -> a walk fixture

`lib/tree_stamp.sh` computes the identity everything above compares on:
`pipeline_tree_stamp <root>` hashes file CONTENTS and repo-relative paths for
pipeline.py, pipeline_core, tools and the extractor's Rust sources — tests and
docs are deliberately outside it, since a new test cannot make a passing gate
stale. `pipeline_stamp_file <root>` is where the stamp lives (`.pre_run_stamp`,
untracked). Both are sourced, never executed.

## A run does not start until something has vouched for the code

`bash pre_run.sh` is the gate; `continue.sh` refuses to launch without it. Two
stages, in this order because the second is unreadable while the first is red:

1. **check.sh** — is the code correct. Static gates, the pytest suite, the
   coverage ratchet.
2. **walks** (`pytest -m walk`) — does the loop's decision sequence go where the
   theory says. A walk replays a recorded pathological run through the real
   `run_phase_c_tdd` with cargo and the model stubbed.

Adding a walk costs pointing the distiller at the log the pathology is in — the
run's iterations, its retrigger fires and its refusals are all already printed:

    python tools/distill_walk.py --cluster stable --run 259637-36db9aad \
        logs/pipeline_anyhow.log.1 logs/pipeline_anyhow.log \
        -o tests/fixtures/walks/259637-36db9aad.json

then a test module under `tests/walks/` driving `tests.walks.harness.replay`.
The harness names the three things it MODELS rather than replays (the entry
high-water, the iteration a retrigger's `continue` swallowed, and test identity);
extend that list rather than quietly widening one of them.

On a full pass `pre_run.sh` writes `.pre_run_stamp`: the content hash of
pipeline.py, pipeline_core, tools and the extractor's Rust sources
(`lib/tree_stamp.sh`). `continue.sh` recomputes it and exits 3 when it has moved.
Git cannot play this role — runs are launched from an uncommitted tree, so
`HEAD` is identical across the edit that introduced a deadlock and the edit that
fixed it. A relaunch after a crash with no edits still matches and costs nothing.

**What a walk may assert.** A recorded walk is a FIXED observation sequence, and
it stops describing reality at the first iteration where the new code would have
done something different. It cannot tell you the run would have reached 108
passing. It can tell you a decision became reachable — re-admission fires by
iter N, the durable slot is never overwritten by a worse tree, the plateau exit
reports a number describing the tree it exits on. Assert reachability, never the
score.

**Every walk carries its negative control.** A fix-verification test that does
not fail on the pre-fix code has proved nothing. Run the same walk with the old
grading injected and assert the bad outcome reproduces. That is the difference
between "the new code is correct" and "the theory bites on run
`259637-36db9aad`", which cost nine hours, 148 landed fills and +5 tests.

## Watch a live run at the iteration boundary, in-session

The loop emits exactly one summary per iteration and it carries the whole state
vector:

    iter N: cargo test rc=... pass=X fail=Y oracle=Z (improved=..., stall=a/b);
    frontier=F fn(s) (merged), E skipped as exhausted

That is the same vector a walk fixture is distilled from, so what is asserted
offline is what arrives online. Watch it with a persistent in-session monitor
over `logs/pipeline_anyhow.log`, not a cold `claude -p`: a cold spawn re-derives
the run from the log every wake and reports symptoms, having no memory of which
fix is under test or what trajectory it predicted.

**Silence is not success.** A filter matching only the summary line cannot tell a
crashloop from a slow iteration. It must also carry the terminal signatures —
`MODULE_PLATEAU_ABORT`, `VERDICT MISMATCH`, `TIMED OUT`, `Traceback`,
`high-water re-based`, `NOT re-admitting`. Keep the alternation tight: a broad
`Error` match over a half-megabyte log is a firehose.

**On each wake**, compare the arriving vector against the trajectory the fix
predicted. On-theory: say nothing. Off-theory — the gate the fix targeted did not
fire by the iteration the walk said it would — stop, fix, validate; not
"watching, N iterations left". A pathology no walk covers: distill it from the
live log into a fixture, so the expensive path funds the cheap one.
