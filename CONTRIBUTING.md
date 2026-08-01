# Contributing to determ

## The gate

One command must stay green for every change:

```bash
bash check.sh
```

It runs, and fails on any finding:

| Stage | Tool | Config |
|---|---|---|
| types | `mypy` (`pipeline_core` held to strict-optional) | `pyproject.toml` |
| lint / idioms | `ruff` (`F,I,UP,C4,B,SIM` + select E codes) | `pyproject.toml` |
| architecture | `import-linter` (layer contract) | `pyproject.toml` |
| extraction invariants | `tools/check_extraction_invariants.py` | — |
| tests + coverage | `pytest`, with `pipeline_core.domain` floored at 95% | `pyproject.toml` |

The Rust crate (`tools/skeleton_extractor`) has its own gate:

```bash
cd tools/skeleton_extractor
cargo fmt --check && cargo clippy --all-targets -- -D warnings && cargo test && cargo doc --no-deps
```

CI (`.github/workflows/ci.yml`) runs both on every push / PR. If you add a tool or dependency,
add it to `requirements.txt` / the crate's `Cargo.toml` — never `pip install` / `cargo install`
ad-hoc (reproducibility).

## Architecture rules (enforced, not just described)

`pipeline_core/` is layered; **a higher layer may import a lower one, never the reverse**
(`import-linter` enforces this):

```
domain  ◄  ports  ◄  application  ◄  adapters  ◄  pipeline.py (composition root)
```

- **domain** — pure record types + rules. stdlib + the foundational `log`/`retry`/`io` facades only.
- **ports** — `typing.Protocol` interfaces.
- **application** — the extracted `task*` use cases; receive collaborators via frozen-dataclass
  deps records (constructor injection, no framework).
- **adapters** — the *only* layer that does I/O (LLM client, extractor binary, cargo).

The Rust crate mirrors this: `model.rs` is the serde wire contract, `cfg.rs` the predicate logic,
`skeleton.rs` / `enumerate.rs` / `merge.rs` the work; the library edge uses `thiserror`, `anyhow`
lives only in `main.rs`.

## Three hard rules specific to this project

1. **The strip boundary is sacred.** After `strip`, no code path may read the original source
   again — not as a fallback, not for hard cases. Source-derived data is extracted once in the
   pre-strip bake step and saved to disk. New needs extend the bake step, never add a post-strip
   read. (See `CODE_ARCHITECTURE.md` and the project's rule on source-derived strategy.)
2. **Self-hosted only.** The pipeline must solve every reconstruction issue with the locally
   served model. It may never call a cloud LLM — not as a fallback. The fix for "the model is
   stuck" is a better prompt / architecture / model, never outsourcing the call.
3. **No tracker IDs in code.** Comments, docstrings, tests, prompts, log lines and event payloads
   must not cite a debt number, plan number or requirement ID — the tracker files are untracked
   working notes, so the reference resolves to nothing for anyone reading the repository. Write
   the fact and its evidence instead. See `CLAUDE.md`.

## Adding a task or an extraction

Extracted tasks live in `pipeline_core/application/tasks/` behind a `*Deps` frozen dataclass; the
invariant checker enforces that every declared field is provided at the composition root and
consumed in the body, with no unbound names. Follow an existing task (e.g. `task3.py`) and keep
the deps record minimal — a giant deps record means the unit isn't actually decoupled.

## Running the pipeline

```bash
bash setup.sh                                    # full environment (Rust, venv, deps, extractor)
.venv/bin/python pipeline.py --repo <git-url>    # clones into tmp/, git-managed
bash restart.sh --y                              # restart from scratch
bash continue.sh                                 # resume a stopped run
```

The model backend and flags are configured in `.env` (not committed).
