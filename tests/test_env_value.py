"""Pins for `tools/env_value.sh` — the settings resolver shell entry points use.

Precedence is shell-env > `.env` > built-in default, mirroring `load_dotenv`,
which does not override variables the environment already carries. Getting this
backwards would silently pin every operator pass to whatever `.env` says even
when the caller asked for something else on the command line.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent.parent / "tools" / "env_value.sh"


def _resolve(key: str, default: str = "", *, env_file: Path | None = None,
             **overrides: str) -> str:
    env = dict(os.environ)
    env.pop(key, None)
    env.update(overrides)
    if env_file is not None:
        env["ENV_FILE"] = str(env_file)
    out = subprocess.run(
        ["bash", str(_SCRIPT), key, default],
        capture_output=True, text=True, env=env, check=True)
    return out.stdout.rstrip("\n")


def _env(tmp_path: Path, body: str) -> Path:
    p = tmp_path / ".env"
    p.write_text(body, encoding="utf-8")
    return p


def test_shell_env_beats_the_file(tmp_path: Path):
    f = _env(tmp_path, "OPERATOR_EFFORT=low\n")
    assert _resolve("OPERATOR_EFFORT", "max", env_file=f,
                    OPERATOR_EFFORT="high") == "high"


def test_file_beats_the_default(tmp_path: Path):
    f = _env(tmp_path, "OPERATOR_EFFORT=max\n")
    assert _resolve("OPERATOR_EFFORT", "medium", env_file=f) == "max"


def test_default_applies_when_neither_is_set(tmp_path: Path):
    assert _resolve("OPERATOR_EFFORT", "max", env_file=_env(tmp_path, "")) == "max"


def test_missing_file_is_not_an_error(tmp_path: Path):
    assert _resolve("OPERATOR_MODEL", "opus",
                    env_file=tmp_path / "absent.env") == "opus"


def test_empty_assignment_reads_as_unset(tmp_path: Path):
    # `.env` carries placeholder rows like `BACKEND=`; those mean "not set",
    # not "override with empty", or a placeholder would blank a real default.
    f = _env(tmp_path, "OPERATOR_MODEL=\n")
    assert _resolve("OPERATOR_MODEL", "opus", env_file=f) == "opus"


def test_last_uncommented_assignment_wins(tmp_path: Path):
    f = _env(tmp_path, "OPERATOR_MODEL=sonnet\n# OPERATOR_MODEL=haiku\n"
                       "OPERATOR_MODEL=opus\n")
    assert _resolve("OPERATOR_MODEL", "x", env_file=f) == "opus"


def test_commented_rows_are_ignored(tmp_path: Path):
    f = _env(tmp_path, "# OPERATOR_EFFORT=low\n")
    assert _resolve("OPERATOR_EFFORT", "max", env_file=f) == "max"


def test_quotes_and_surrounding_space_are_stripped(tmp_path: Path):
    for body in ('OPERATOR_EFFORT="max"\n', "OPERATOR_EFFORT='max'\n",
                 "  OPERATOR_EFFORT=  max  \n"):
        assert _resolve("OPERATOR_EFFORT", "low",
                        env_file=_env(tmp_path, body)) == "max", body


def test_a_prefix_of_the_key_does_not_match(tmp_path: Path):
    # `OPERATOR_MODEL=` must not be answered by `OPERATOR_MODEL_EXTRA=`.
    f = _env(tmp_path, "OPERATOR_MODEL_EXTRA=sonnet\n")
    assert _resolve("OPERATOR_MODEL", "opus", env_file=f) == "opus"


def test_repo_env_configures_the_operator_pass():
    # The settings operator_run.sh reads must actually be present, or the pass
    # silently falls back to the script's defaults.
    text = (Path(__file__).resolve().parent.parent / ".env").read_text()
    rows = [ln.strip() for ln in text.splitlines()
            if ln.strip().startswith(("OPERATOR_MODEL=", "OPERATOR_EFFORT="))]
    assert any(r.startswith("OPERATOR_MODEL=") for r in rows)
    assert any(r.startswith("OPERATOR_EFFORT=") for r in rows)
