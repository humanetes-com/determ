# CLAUDE.md — conventions for code written in this repo

## Nothing leaves this machine

**Never transmit repository or workspace content to any external service.**
No claude.ai Artifact publishing, no gists or pastebins, no third-party APIs,
no cloud-side agents or remote sessions. This covers everything: source,
logs, prompts, spec docs, trackers, and the crate under reconstruction. The
only sanctioned LLM endpoint is the locally served model (vLLM/Ollama on
localhost).

**Why.** The pipeline's whole design assumes a closed loop on this machine —
uncommitted trees, untracked working notes, and logs that quote both. An
upload publishes whatever it carries; external services may cache or index
it, and deletion is not retrieval.

**Instead, render and share locally.** A markdown preview in the IDE, a
local mermaid render to SVG, a file path in the chat. If a preview genuinely
cannot be produced locally, say so and stop — do not fall back to an upload.

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

## `technical_debt.md` holds only open work — implemented entries move to `completed_tech_debt.md`

When every point of a debt entry is implemented (or refuted / withdrawn),
move the entry **verbatim** to `completed_tech_debt.md` — never reworded,
never summarized, so its run ids and measurements stay quotable. An entry
with ANY open remainder — an unshipped sub-item, an open question, a pending
live validation — stays in `technical_debt.md` until the remainder closes or
is split into its own entry. A validation/amendment section moves with the
entry it validates. Group headings whose children split (some shipped, some
open) stay in `technical_debt.md` while any child does; copy the intro into
the archive alongside the moved children.

**The assistant does not write, delete or repair a single line of the crate under
reconstruction.** That covers `tmp/<project>/src` (the merged tree), every
`.skeleton-filled/<cluster>/src`, every `.skeleton/<cluster>/src`, `.best_tree`,
and the generated tests — everything the pipeline authors. No exception for "it is
one obvious line", "the loop is stuck on it", or "I will put it back". The editable
surface is `pipeline.py`, `pipeline_core`, `tools`, the extractor's Rust sources,
the scripts and the trackers. Nothing else.

**Why, in three parts.** The crate IS the measurement: pass counts, verdicts, the
ratchet and every conviction describe a tree the pipeline produced, and one
hand-authored line makes every later number a statement about a tree a human
repaired — unfalsifiable and worthless as evidence that any fix works. The crate is
also the model's job: the whole point is whether the loop can reconstruct it, so
doing the loop's work for it converts an open question into a hidden assumption.
And a broken body is EVIDENCE about a pipeline defect; deleting it destroys the
reproduction before the defect is understood, which is the fastest known way to
ship a fix that addresses nothing.

**The instance, 2026-08-11, run `3337398-fbf7e143`.** The merged tree stopped
compiling on `error[E0592]`: `ErrorImpl::debug` defined twice, at
`src/error.rs:301` and `src/fmt.rs:11`, identical signature. The fix loop had
written it — `debug` exists only in `fmt.rs` in both authored layouts and both
filled cluster trees, `error.rs` carried it nowhere upstream of the merge, and the
run log holds three `[fix loop/impl] spliced src/error.rs::debug` lines against
zero in the run before. The loop then could not recover: a unit-scope fix may only
rewrite a body, never remove a definition, so every attempt re-checked to the
identical error, escalated to whole-file rewrites of `lib.rs`/`kind.rs`/`chain.rs`/
`context.rs` — none of which held the duplicate — and cycled 9 → 6 → 10 → 7 → 7 →
9 → 10 of 12 clean files across two full passes with zero scored sweeps and zero
verdict rows in 4h45m. The assistant proposed deleting the duplicate by hand. That
was the wrong instinct twice over: it would have patched the symptom of a splicer
defect, and it would have contaminated the tree whose numbers were about to be used
to judge whether the splicer fix worked.

**What to do instead — find the mechanism that already owns the removal.** The
pipeline has one for most damage, and it is usually cheaper than it looks. Here it
was the merge: `merge_cluster_outputs` passes the workspace file as
`--merge-output` only and takes its inputs exclusively from the `--cluster` paths,
so re-running task2b regenerates `src/` from cluster trees that never held the
duplicate — the pipeline removes it, by its own rule, with the provenance intact.
Other owners: the crash/layout retrigger re-stubs, heal restores `.best_tree`, the
batch ratchet restores an older tree. If no mechanism owns it, that absence IS the
finding — write the guard that stops the damage being created, and let the next run
build the tree. State the cost of the in-process route honestly (a re-merge
discards task2c's merged-tree fills, which live only in the workspace `src/`)
rather than reaching for an editor because the honest route is expensive.

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

## A recurring finding is not a finding — check the record before diagnosing

The costliest loop in this project is not in the pipeline; it is the working
ritual around it: a run stalls → the stall is diagnosed from scratch → the
diagnosis is confirmed as "recurring" → a bespoke mechanism ships → the next
instance of the same class starts the ritual over. Measured 2026-08-14
(`scripts/pattern_census.py` over 34 runs): the frame-blind crash blamed NOBODY
in 8 runs across ten days before it was named; the four chain tests escalated
to the identical producer set in 11 consecutive runs; `test_large_alignment`
in 29 of 34 runs across 18 days. Every one of those instances was individually
"detected", discussed, and agreed to be recurring — the detection itself
recurred, and each pass through the ritual cost a session and shipped one more
run-scoped mechanism against a pathology that lives across runs.

**The rule.** Before diagnosing any stall, crash class, burn, or lockout as
new, interrogate the record first — it costs two commands:

    python scripts/pattern_census.py        # the five classes + cross-run chronics
    grep -n "<symptom>" technical_debt.md   # the entry that already names it

If the class is already recorded, the deliverables CHANGE. Do not re-derive
the diagnosis; cite the entry. Do not confirm "yes, this recurs"; the census
row already proves it, and restating it is the ritual. Do not design a sibling
mechanism; the standing consolidation program (debt 171: observability →
shadow detector → lever vocabulary → diagnostician) exists precisely so that
recurring classes get one answer — advance it, or state concretely why the
instance falls outside it. The one genuinely new question a recurrence raises
is: **why did the existing answer not fire?** That gap — a detector that
missed, a reaction scoped to the wrong lifetime, a class with no log
signature — is the finding. Everything else is re-reading.

The tells that the ritual has started: a diagnosis that re-derives from raw
logs a conclusion the tracker already states with run ids; the sentence "this
is the same issue as ..." arriving as a discovery rather than as the premise;
a proposed fix whose trigger condition matches a census class. On any of
them, stop and run the census. A new pathology no census detector catches is
real work twice over: fix it AND extend `pattern_census.py` so its next
instance is a lookup, not an investigation.

## The crate under reconstruction is not in `src/`, and a path-scoped negative proves nothing

`src/` in the workspace is a shell: after strip it holds `*.gen_tests_*.rs` and a
one-line `lib.rs`. The crate the loop builds, tests and scores is
`.skeleton-filled/<cluster>/src` — `run_phase_c_tdd`'s `target_src_dir` is
`filled_dir = project_dir / ".skeleton-filled" / cluster.name`
(`application/generators/task2_generator.py`), which is what cargo runs against,
what the task2a checkpoint stages, and what a resume picks up (the seed at cluster
entry is `if not dst.exists()`, so prior fills are never overwritten). Two siblings
complete the picture: `.skeleton/<cluster>/src` is the authored layout those bodies
answer to, and `.best_tree/<cluster>/src` is heal's durable restore tier. **All
three are tracked in the workspace repo** — its `.gitignore` is `/target/` and
`/Cargo.lock`, nothing else.

**The instance, and it cost a plan.** A debt entry concluded that the commit
`pipeline: task2a checkpoint — stable:95 passing (iter 3)` "contains no
reconstruction", on one measurement:

    $ git ls-tree -r --name-only 1f4099b -- src | grep -v gen_tests
    src/lib.rs                          # 1 line

True, and evidence of nothing. The same commit carries
`.skeleton-filled/stable/src/error.rs` at 40,645 bytes with 9 `unimplemented!()` —
25 stubs across the cluster against the 107 in the tree that replaced it — and the
`gen_tests` blobs are byte-identical between the two commits, so the counts compare
directly. The record tree was recoverable the entire time it was being written up
as lost, and "the checkpoint is broken" became the premise of the next session's
fix batch.

**The rule.** A query that returns nothing has two explanations — the thing is
absent, or the query was scoped where it never was — and only one is a finding.
Before reporting an absence, establish the path from the code that WRITES the
artefact, or drop the pathspec and enumerate whole: `git ls-tree -r <sha>` with no
`-- path`, `find` with no `-path`, `grep -r` from the repo root. It costs one
command, and it is the difference between a broken mechanism and a working one
whose output nobody had located.

**Restoring a past tree is a pair, never one directory.** t1.5 re-authors the
layout on every run, so bodies from one run do not compile against another's
skeleton: between `1f4099b` and the tree that replaced it, `ErrorImpl` lost
`pub(crate) backtrace: crate::backtrace::Backtrace`. Take `.skeleton/<cluster>` and
`.skeleton-filled/<cluster>` from the same commit or take neither. Extract with
`git archive <sha> <paths> | tar -x -C <scratch>` and copy deliberately — a
`git checkout <sha> -- <path>` overwrites the working tree, which in this workspace
is permanently dirty and holds fills newer than any commit.

**The tracking is also what makes `--restart-from-augmented` destructive.**
`RestoreFromAugmented.run` (`adapters/workspace_restore.py`) is
`git reset --hard <task0c-augmented-* tag>`, and the tag predates every fill, so the
reset DELETES those tracked dot-directories rather than leaving them alone. Measured
across the 2026-08-08 restart: 5,137 files removed, 5,067 of them
`.skeleton-filled`. The artefact survives in the object store — after such a reset,
reach a past tree by sha, never by looking at the working tree.

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

## A repo operation has a tool that owns it — find it before doing the operation by hand

`tools/` holds ~55 helpers, `scripts/` 8, the repo root ~30 user-owned
launchers. Nobody remembers that inventory, which is exactly why the rule is
mechanical rather than a matter of recall: **before performing any repo
operation by hand — saving a handover, rotating a log, archiving debt,
restoring a tree, distilling a walk, resetting a counter — run one `ls`+`grep`
for a tool that already owns it.** It costs one command:

    ls tools/ scripts/ *.sh | grep -i <operation>

**The instance, 2026-08-17.** Asked to save the handover, the assistant read
`handover.md`, saw sessions 348, 347, 346, 343 and 325 stacked newest-first,
inferred "prepend a new block", mapped the file's section structure to do it by
hand, and took a manual backup. All of that was wrong. `tools/rotate_handover.sh`
owns the operation and the convention is **archive-then-OVERWRITE**, not
prepend: it moves the current `handover.md` into `previous_handovers.md`, is
idempotent (it refuses to double-archive a session), strips `<!-- PINNED:BEGIN
… -->` blocks out of the archived copy because standing reference has a
different lifecycle from the session it sits in, and mirrors them to
`handover_pinned.md` so the next session can paste them back verbatim. Doing it
by hand would have buried the pinned block in an archive nobody loads. The five
stacked sessions that made prepend *look* like the convention were not the
convention — they were evidence the rotation had not been run in a while.

**Why this is the same rule as the path-scoped negative above, not a new one.**
Both are "establish the mechanism from the code that owns the artefact, never
from the artefact's current shape". A tree's layout does not tell you what
writes it; a file's accumulated state does not tell you how it is meant to be
written. In both cases the artefact is the *output* of a convention and reading
it backwards recovers a plausible convention that is not the real one.

**The tell** is the sentence "the file looks like it wants X" — or any plan
whose first step is to hand-edit a file the pipeline or the workflow also
writes. On that sentence, stop and grep. If no tool owns it, that absence is
worth a line in the summary, and a durable operation with no owner is a
candidate for `scripts/` under the section below.

## A script the assistant writes goes in `scripts/`, with a usage-and-interpretation header

Every durable script the assistant creates lives in `scripts/` — never the
repo root, which holds the user-owned launchers (`continue.sh`, `pre_run.sh`,
`redo.sh`, ...), and never `tools/` unless it is part of the pipeline itself
(`tools/` is inside the tree stamp, so adding a helper there invalidates the
pre-run gate for no reason; `scripts/` is outside it). One-off working files
still go to the session scratchpad, not `scripts/`.

Each script opens with a header comment that answers, in this order: WHY the
script exists (the measured fact or defect that motivated it, with run ids);
USAGE (arguments, env knobs, preconditions — especially GPU/vLLM ownership
and cost); and HOW TO INTERPRET THE OUTPUT — every field of the summary it
prints, and the decision rule the numbers feed (e.g. "place the cap above the
healthy cell's max; would-hit-cap(X)=k/N is how many healthy calls the
current cap would have broken"). A script whose output needs its author to
read is not finished. `scripts/measure_muse_reasoning.sh` is the template.

## A final summary of changes ends with a suggested commit message

Commits are user-owned and the tree is permanently dirty: the assistant never
runs `git commit` and never checks commit state. The flip side is that the
user commits from the summary, not from a diff crawl — so **every final
summary of a finished batch of changes concludes with a ready-to-use
suggested commit message as its last line.** One message per coherent batch;
if the batch is the session's work, one message for the session.

## Saving a handover is `rotate_handover.sh`, then OVERWRITE — never a hand-prepend

    bash tools/rotate_handover.sh   # archive current handover.md -> previous_handovers.md
                                    # strips PINNED blocks, mirrors them to handover_pinned.md
    # then WRITE handover.md fresh: the new session block + the pinned block(s) back

`handover.md` holds ONE session plus its pinned blocks — not a stack. The
rotation is what keeps it that way, so a `load handover` reads a few hundred
lines instead of paging through superseded sessions (it had reached 274 KB and
five stacked sessions before the 2026-08-17 rotation; it is 795 lines after).
The script refuses to archive a session already in `previous_handovers.md`, so
running it twice is safe and running it when unsure is the cheap move.

**The pinned blocks are the part a hand-edit loses.** Content between
`<!-- PINNED:BEGIN name -->` and `<!-- PINNED:END name -->` is standing
reference with its own lifecycle — the score history, the standing cautions —
and archiving it with the session would bury it in a file nobody loads. The
script strips it from the archived copy and mirrors it to `handover_pinned.md`;
**carrying it into the new `handover.md` is the writer's job and the script
prints a CARRY FORWARD reminder.** Verify with
`grep -c 'PINNED:BEGIN' handover.md` before calling the save done.

A session block opens `# Handover — session N (date) — **<what the session
established, in bold>**` — the script hard-fails on any other first line — and
closes with `## ON RETURN`, `## State at handover`, `## Key facts` and the
suggested commit message(s).

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

## Pick the restart lever by the phase your fix lives in

Every launch goes through `continue.sh`, so **`bash pre_run.sh` comes first, always**
— `restart_from.sh` and `redo.sh` both `exec continue.sh`, which recomputes the
stamp and exits 3 when it moved. Answer this before choosing: which phase executes
the code I changed, and does validating it need a tree that has not seen it?

**The phases, and what each lever re-enters.** `task0a/0c` clone + augment,
`strip` removes the bodies, `task1` synthesises FRs, `t1.5` re-authors the layout,
`task2a` fills per cluster (the TDD loop, `run_phase_c_tdd`), `task2b` merges,
`task2c` fills the merged tree, `task3` tests and repairs, `task4` genesis. The
fills live in `tmp/anyhow/.skeleton-filled/<cluster>/src`, the layout they answer
to in `.skeleton/<cluster>/src`.

- **`bash continue.sh`** — resume at the first phase `pipeline_state.md` does not
  mark done. Cheapest and the default; on 2026-08-10 it re-entered a killed run at
  the merge phase in under two minutes and zero model calls. It validates only what
  runs AFTER the resume point: a run that died in t3 resumes in t3, and a t2a-phase
  fix is never executed. Flags: `--y` (stop a running pipeline first), `--force`
  (SIGTERM+SIGKILL), `--no-tail` (exit after launch), `--no-pre-run` (bypass the
  gate, prints as the escape hatch it is). `--wipe` is gone and refuses loudly.
  Never wrap it in `timeout` — coreutils signals the whole process group and
  SIGTERMs the pipeline it just started.
- **`bash restart_from.sh <stage>`** (anyhow; `<stage>` ∈ task1|task2|task2a|task2b|
  task2c|task3|task4, `--force` to skip the graceful stop) — clears the matching
  `[<key>]` lines from `pipeline_state.md` plus the `passing-*` lock tags, then
  execs `continue.sh --y`. **It preserves every fill on disk**: the cluster seed is
  `if not dst.exists()`, so re-entering task2a resumes the TDD loop ON the existing
  bodies rather than re-stubbing them. This is the lever for a driver-half fix
  (charging, taint, epochs, re-admission, the red-build budget) that only executes
  inside `run_phase_c_tdd`. Note the real boundaries are task1 and task2 —
  task2a…task4 are visibility labels for sub-stages of one atomic `task2-4`
  reconstruct phase, so task2a/2b/2c all re-run the loop; task3/task4 clear only
  `task2-4` and go straight to test/fix. `resume_from_t2.sh` is a shim for
  `restart_from.sh task2`.
- **`bash run_hardcrate.sh [--complexity=N] [--from=STAGE]`** — the hardcrate smoke
  in `tmp/hardcrate`, same t1…t4 vocabulary via the shared
  `lib/restart_from_lib.sh`. Without `--from` it re-assembles the crate and
  `rm -rf`s the workspace; with `--from` it preserves fills exactly as
  `restart_from.sh` does. Requires vLLM already up on :8000.
- **`bash redo.sh [--force] [--full]`** — the only lever that DESTROYS the
  reconstruction: it wipes `.skeleton-filled`, and with `--full` also `.skeleton`
  (forcing the bake to re-run), then relaunches `continue.sh --y --keep-augmented`,
  i.e. `pipeline.py --restart-from-augmented`, whose `git reset --hard` to the
  task0c tag removed 5,137 tracked files on 2026-08-08.

**When to wipe, and when wiping destroys the measurement.** Wipe when the thing
under test is the construction of the tree itself — a t1.5 layout change, a bake or
strip change, an FR-synthesis change — because those cannot be observed on a tree
already built by the old code (`redo.sh --full --force` skips clone+task0c and is
what re-arms the bake/pin harvest). Do NOT wipe to validate a loop-behaviour fix:
the fills ARE the state the gates read, and a from-scratch run spends hours
re-reaching the position the pathology needs before it can even reproduce. And
never wipe to "start clean" while a diagnosis is open — the run log and the tree are
the only two records of what earlier runs achieved, which is why `--wipe` was
removed from `continue.sh` rather than fixed.

**Comparing two runs means neither of them wipes.** A/B is
`compare_models.sh snapshot <name>` once, then `arm <seed> <qwen|muse>` per arm —
`arm` restores the seed by `rm -rf`ing `.skeleton` and `.skeleton-filled` and
copying the snapshot back, so both arms start from the same tree AND the same
layout (they are a pair; taking one without the other gives bodies that do not
compile). A `redo.sh` between arms replaces the seed with a fresh one and the
comparison is over. Sequence: `snapshot baseline`, then
`arm baseline qwen && bash pre_run.sh && bash continue.sh`, same for the other arm,
then `report qwen=<run-id> muse=<run-id>`; `compare_models.sh list` recovers a run
id you did not note. What is compared is progress per SWEEP and per hour, not
tokens/sec. Take the cheap validation first — `run_microcrate.sh` on a new model
before spending anyhow hours, so a model that ignores the FN/FILE output contract
fails in minutes instead of looking like a slow model six hours in.

**The model and the server.** `MODEL=` in `.env` is the one switch —
`VLLM_MODEL_NAME` falls back to it, `TaskModels.from_env` defaults every
`TASK*_MODEL` to it, and `MODEL_CAPS`/`model_tier` key the reasoning dialect and
prompt tier on the same string. `docker_vllm.sh` derives image, parsers and a
max-len CEILING from it (`--y` to stop what is running, `--allow-download` to let
vLLM fetch an uncached model, `--no-pull` for an offline relaunch). **It pulls
`:latest` on every launch by default**, because `docker run` never re-fetches a tag
it already holds — this box sat on a 2026-05-08 image serving vLLM 0.20.2 for three
months, which is how a model needing a newer server fails as "unknown architecture"
instead of "your image is stale". The corollary: restarting the server also
upgrades it, so do not restart vLLM in the middle of a measurement you intend to
compare against an earlier run — check `curl -s localhost:8000/version` and leave a
healthy container alone.

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
