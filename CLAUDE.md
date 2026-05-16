See autopilot.md for autopilot behavior rules.

Past session handovers: see handover/HANDOVERS.md.

Do not use RemoteTrigger or create remote schedule routines. No code or data should be sent to Anthropic's cloud servers via scheduled agents.

IMPORTANT: Always produce compact structured output instead of markdown prose when generating guides, strategy docs, or rule sets for LLM consumption. Use tables, symbolic notation, and abbreviated rules rather than verbose English paragraphs. Prose is an inefficient token format for conveying structured rules to an LLM.

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

---

# Improvements enabled by pipeline-in-own-repo

| Capability | How |
|---|---|
| Versioning | Each run records which pipeline commit generated the results |
| Rollback | `git revert` a pipeline commit if a change breaks results |
| CI | Run `pipeline.py --project test-fixture` on push to catch regressions |
| Reproducibility | `git checkout abc1234` pins pipeline version to reproduce past result |
| Diffing | `git diff` between pipeline versions shows exactly what changed coverage |
