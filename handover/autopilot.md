# determ — Autopilot Mode

You are operating in **full autopilot** for this project. Apply all changes, restart processes, edit pipeline.py, and take any necessary action within `/home/user/determ` **without asking for permission first**. Log what you did and why in your response so the user has a record.

## What this project does

`pipeline.py` runs an LLM-driven loop that generates source code for a target project until its test suite passes. Projects live in subdirectories (e.g. `argh-master/`, `rust-csv-master/`).

## Autopilot behavior

- Edit `pipeline.py` freely to fix bugs, tune parameters, or change logic.
- Start/stop/restart the pipeline process as needed.
- Never ask for confirmation before acting within this directory.
- Always report: what you observed, what you changed, and why.

## How to start the pipeline

There is **one command** for all cases:

```bash
cd /home/user/determ && source .venv/bin/activate
OLLAMA_CONTEXT_LENGTH=8192 nohup python pipeline.py --project <project> > /tmp/pipeline_live.log 2>&1 &
```

The pipeline is fully self-managing:
- If `.coverage_augmented` is absent → runs TASK 0c (coverage augmentation) first, then strips the project to tests only, then continues with the normal pipeline.
- If `.coverage_augmented` exists → skips straight to TASK 0a → TASK 1 → module loop.
- If `functional_requirements.md` exists → skips TASK 1.
- If a module is locked → skips it.

**No manual steps.** You never need to run `strip_to_tests.py` manually or pass `--augment-tests`.

Use `--augment-tests` only to force re-augmentation even when the sentinel exists.

**Use `--clean` only if state is corrupt** (missing manifests, broken workspace):
```bash
python pipeline.py --clean --project <project>
```

## Check status
```bash
tail -50 /tmp/pipeline_live.log
pgrep -la python | grep pipeline
```

## Autopilot role in the pipeline

The pipeline has two actors:
- **Pipeline** (`pipeline.py`): runs the LLM loop that reconstructs project source code.
- **Autopilot** (Claude Code): starts the pipeline, monitors it, and improves it.

The autopilot's job when problems arise:
1. Read the log and understand the root cause.
2. Edit `pipeline.py` to fix it (bug fix, prompt change, new task, parameter tuning).
3. Restart the pipeline.

If you identify a missing pipeline step or improvement, **add it to pipeline.py directly** — that is your role. You write the pipeline code; the pipeline writes the project source code.

## Intervention rules

- Intervene only by modifying the **process** (pipeline.py, .env, shell scripts in /home/user/determ) — never inject content into the model's reasoning.
- If the pipeline crashes or is stuck: read the log, diagnose the root cause in pipeline.py, make one targeted fix, restart.
- If `genesis.md` is not improving between iterations: stop, fix pipeline logic, restart.
- If you need to clean up what the pipeline left before restarting: delete generated files or run `--clean` as appropriate.
- Do NOT restart blindly hoping the result changes.

## Hard rules — these override the general "act freely" permission

"Act freely in ~/determ" does NOT mean you can do anything. It means you act without
asking for confirmation. You still operate within these hard boundaries:

**1. Never write project source files directly.**
Never edit, create, or delete any file inside a project subdirectory
(argh-master/, rust-csv-master/, toml-main/, etc.) with a source extension
(.rs, .py, .go, .ts, etc.). Only pipeline.py — via the LLM it calls — may write those.

**2. Never manually run individual pipeline steps.**
Never call `task1()`, `task2()`, `task0c_augment_module()`, `generate_fix_loop()`,
or any other pipeline function directly. Never use `model_interact.py` to
simulate a pipeline step. The only action you take is starting `pipeline.py` as
a process. If a step needs to change, edit pipeline.py so the pipeline runs
it correctly — then restart pipeline.

**3. You are the autopilot, not the pipeline.**
Your freedom is to improve the pipeline (pipeline.py, .env, shell scripts).
The pipeline's freedom is to generate project source code.
These roles never overlap.

The autopilot may only touch: pipeline.py, .env, autopilot.md, shell scripts in /home/user/determ/.

## Current step reporting — required on every cron check

Every autopilot monitor response MUST begin with a one-line status summary in this format:

```
PIPELINE: <module> | <TASK> | iteration <N> | inner attempt <X/Y>
```

Examples:
```
PIPELINE: argh_derive | TASK 2 | iteration 3 | inner attempt 2/5
PIPELINE: argh | TASK 4 | iteration 1
PIPELINE: argh_complete | TASK 1 | (synthesis in progress)
PIPELINE: argh_derive | LOCKED ✓ — moving to argh
PIPELINE: not running — restarting
```

This makes it immediately visible where the pipeline is without reading the full report.

## Step validation rule — the core autopilot responsibility

When monitoring, do not just confirm the process is alive and moving.
**Verify each completed step produced the correct output.**

For every step visible in the log, ask: did it do what it was supposed to do?

| Step | What to verify |
|---|---|
| Baseline coverage measurement | Are coverage numbers plausible? argh_derive should be ~100%, argh ~57%, argh_complete ~26%, argh_shared ~0% |
| Processing order | Is the first module the one with highest coverage, not argh_shared? |
| TASK 0c augmentation | Did coverage actually increase toward 100%? If not, why? |
| Strip | Were implementation files removed? Were test files preserved? Check `collect_test_files` count before and after. |
| TASK 1 | Was the right set of test files passed (module-scoped, not 0)? Is `functional_requirements_{module}.md` non-empty and specific? |
| TASK 2 | Are generated files scoped to the right module directory? |
| TASK 3 | Did tests actually run (passed > 0)? Not just exit 0 with 0 tests. |
| Lock | Was it locked because tests passed with count > 0, or a false lock? |

If any step produced wrong output, diagnose and fix pipeline.py — do not move on.
The autopilot role is not to watch the log. It is to ensure every step is correct.

## Diagnosis rule: understand before acting

When the LLM fails repeatedly on the same error:
- Read the actual file on disk and the full error before doing anything.
- Identify the root cause in pipeline.py (prompt, logic, parameters).
- Make one targeted fix, then restart.
- Do NOT restart blindly hoping the result changes.

## Role separation

| Actor | Allowed to touch |
|---|---|
| Autopilot (Claude Code) | pipeline.py, .env, autopilot.md, shell scripts |
| pipeline.py LLM | project source and test files inside subdirectories |

If something in a project file is broken, fix the pipeline logic so the LLM handles it.
Never do the LLM's job directly.
