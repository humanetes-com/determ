#!/usr/bin/env python3
"""Print the served model's server facts as shell assignments for docker_vllm.sh.

WHY: the launcher used to carry its own `case "$VLLM_MODEL"` table (image,
parsers, context ceiling) beside the Python side's dialect, tier and route
tables, each matching the model string its own way. Both sides now read
`pipeline_core.domain.model_profile`, so a model the pipeline would send the
wrong reasoning dialect to cannot be launched with a guessed parser either.
This printer lives here, not in the domain, because the domain does no I/O.

USAGE:
    eval "$(.venv/bin/python scripts/model_profile_shell.py --env-file .env)"
    .venv/bin/python scripts/model_profile_shell.py --model Qwen/Qwen3.6-27B-FP8

HOW TO INTERPRET: prints, one per line,
    VLLM_FAMILY=<family>
    _def_image=<image>  _def_reasoning=<parser>  _def_tool=<parser>
    _max_len_ceiling=<tokens>
    VLLM_MAX_LEN=<resolved context>  VLLM_MAX_LEN_SOURCE=<env|profile|default>
VLLM_MAX_LEN is the same env → profile → default resolution the transport plans
with. An id no profile matches exits 2 with the known families on stderr; set
MODEL_FAMILY=<family> in .env for a repo of a known family spelled differently.
"""

from __future__ import annotations

import argparse
import os
import shlex
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline_core.domain.model_profile import (  # noqa: E402
    UnknownModelError,
    require_profile,
    resolve_model_knobs,
    served_model,
)


def shell_lines(env: dict[str, str], model: str | None = None) -> list[str]:
    """The assignments for `model` (default: the served id in `env`).
    Raises UnknownModelError for an id no profile matches."""
    served = model or served_model(env)
    prof = require_profile(served, env.get("MODEL_FAMILY"))
    knobs = resolve_model_knobs(env, served)
    max_len = knobs.get("VLLM_MAX_MODEL_LEN")
    pairs = (
        ("VLLM_FAMILY", prof.family),
        ("_def_image", prof.image),
        ("_def_reasoning", prof.reasoning_parser),
        ("_def_tool", prof.tool_call_parser),
        ("_max_len_ceiling", str(prof.max_model_len)),
        ("VLLM_MAX_LEN", str(max_len.value)),
        ("VLLM_MAX_LEN_SOURCE", max_len.source),
    )
    return [f"{k}={shlex.quote(v)}" for k, v in pairs]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="model profile → shell assignments")
    ap.add_argument("--env-file", default=None)
    ap.add_argument("--model", default=None, help="served id (default: VLLM_MODEL_NAME or MODEL)")
    args = ap.parse_args(argv)
    env: dict[str, str] = {}
    if args.env_file:
        from dotenv import dotenv_values
        env.update({k: v for k, v in dotenv_values(args.env_file).items() if v is not None})
    env.update(os.environ)
    try:
        lines = shell_lines(env, args.model)
    except UnknownModelError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 2
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
