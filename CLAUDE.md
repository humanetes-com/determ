See autopilot.md for autopilot behavior rules.

Past session handovers: see handover/HANDOVERS.md.

Do not use RemoteTrigger, ScheduleWakeup, or create remote schedule routines. No code or data should be sent to Anthropic's cloud servers via scheduled agents.

IMPORTANT: Always produce compact structured output instead of markdown prose when generating guides, strategy docs, or rule sets for LLM consumption. Use tables, symbolic notation, and abbreviated rules rather than verbose English paragraphs. Prose is an inefficient token format for conveying structured rules to an LLM.

IMPORTANT: Keep all responses short, condensed, and unformatted. No markdown headers, no bullet lists, no tables in prose responses. Plain short sentences only. Code blocks are fine.

---

# Engineering principles

## Never install dependencies manually
Every new tool, binary, or Python package must be added to `setup.sh` (and `requirements.txt` for Python deps). Never run `pip install`, `cargo install`, or `rustup component add` directly — those are one-off fixes that break reproducibility. The correct action is always: add to setup, then run setup.

## Pipeline restart timestamps
Each restart is logged as `PIPELINE STARTED — <timestamp> UTC` in `logs/pipeline_anyhow.log`. Check this to confirm which code version is running.

## Static analysis after every Python edit
After modifying any `.py` file, run `bash check.sh` and fix **all** reported errors — including pre-existing ones unrelated to your change. Never leave check.sh failing.

## Fix processes, not symptoms
Do not patch individual failures in isolation. Ask: what process produced this failure, and how should that process be improved so the failure cannot recur? Examples:
- A type error in pipeline.py → add static analysis to the dev workflow, not just fix that one return statement.
- A coverage tool giving wrong results → improve the coverage backend selection process, not just ignore that file.
- An LLM returning bad output → improve the prompt or retry logic, not manually correct the output.

## Let the LLM fix code, not the autopilot
The autopilot (this assistant) must not directly fix bugs in project source files inside `tmp/`. Those are owned by the pipeline. The correct action is to improve pipeline.py so the LLM produces better output on the next run. The autopilot fixes the process; the process fixes the code.

## Same principle applies to the pipeline itself
If pipeline.py has a bug, fix the pipeline code and restart — do not manually edit workspace files or call pipeline functions directly to work around the bug.

---

# Repository structure

The pipeline is its own git repository. Project workspaces live in `tmp/` and are
not committed to the pipeline repo (gitignored). Each workspace is a separate
git repo managed by the pipeline.

```
determ/                     ← pipeline repo (this repo)
  pipeline.py
  model_interact.py
  monitor.py
  continue.sh / restart.sh
  .env                      ← not committed
  tmp/                      ← not committed (gitignored)
    anyhow-master/          ← workspace repo, managed by pipeline
    argh-master/            ← workspace repo
```

---

# Running the pipeline

## Start / restart from scratch

```bash
bash restart.sh --y
```

Kills any running pipeline, cargo-cleans, strips gen_* blocks, starts fresh with `--clean`.

## Resume

```bash
bash continue.sh
```

## Using a git URL or local path (full git-managed mode)

```bash
# Clone from URL into tmp/, checkout pipeline branch
nohup .venv/bin/python pipeline.py --repo https://github.com/dtolnay/anyhow > /tmp/pipeline_anyhow.log 2>&1 &

# Local directory (copies into tmp/, git init if no .git)
nohup .venv/bin/python pipeline.py --repo /path/to/project > /tmp/pipeline_anyhow.log 2>&1 &

# Existing workspace in tmp/ (auto-detects git, backward compat)
nohup .venv/bin/python pipeline.py --project anyhow-master > /tmp/pipeline_anyhow.log 2>&1 &
```

`--project name` resolves to `tmp/{name}` first, then `determ/{name}` (backward compat).

---

# Git workspace system

When `--repo` is used (or `--project` points to a git repo), the pipeline commits
at key transitions. Every step is reversible.

## Branch strategy

```
main/master   ← original source, never touched
pipeline      ← all pipeline commits
```

## Commit points

| Transition | Commit message | Tag |
|---|---|---|
| task0b done | `pipeline: task0b done` | — |
| each cluster task0c done | `pipeline: task0c/stable — anyhow:83.4%` | — |
| before strip | `pipeline: pre-strip — {module} task0c complete` | `pre-strip-{module}` |
| after strip | `pipeline: stripped — {module}` | — |
| task1 done | `pipeline: task1 — {module}` | — |
| task3 pass | `pipeline: task3 iter N — {module} tests pass` | `passing-{module}-N` |
| locked | `pipeline: locked — {module}` | — |
| complete | `pipeline: complete — all tests pass iter N` | — |

## Reverting to before strip

```bash
git -C tmp/anyhow-master checkout pre-strip-anyhow
```

## --clean in git mode

Resets to default branch, deletes pipeline branch, recreates it — full clean slate.

---

# Hard rules

**1. Never write project source files directly.**
Never edit files inside `tmp/{project}/src/`. Only pipeline.py via LLM may write those.

**2. Never manually run pipeline steps.**
Never call task functions directly. Start pipeline.py as a process only.

**3. Role separation.**
Autopilot touches: pipeline.py, .env, autopilot.md, shell scripts, CLAUDE.md, handover/.
Pipeline touches: files inside tmp/{project}/.

**4. Never commit secrets.**
.env is gitignored. Never add it to git.

**5. No implementation without explicit approval.**
When asked for evaluation or analysis, discuss findings and proposed fixes until the user says to proceed. Do not implement anything — no edits, no file writes — until the user explicitly says to go ahead.

**6. Ask before committing.**
After implementing a fix, give a short overview of what was done, then ask "May I commit?" Wait for yes or no before running any git command.

---

# Improvements enabled by pipeline-in-own-repo

| Capability | How |
|---|---|
| Versioning | Each run records which pipeline commit generated the results |
| Rollback | `git revert` a pipeline commit if a change breaks results |
| CI | Run `pipeline.py --project test-fixture` on push to catch regressions |
| Reproducibility | `git checkout abc1234` pins pipeline version to reproduce past result |
| Diffing | `git diff` between pipeline versions shows exactly what changed coverage |
