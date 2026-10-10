"""A child process's output that is not valid UTF-8 is read, not raised on.

Run `39786-fd38e72f` (2026-10-09, cluster `stable`, fill 5): the cluster was
aborted at 137/165 by `UnicodeDecodeError: 'utf-8' codec can't decode byte
0xc0 in position 508`, one fill after the sweep found a heap-use-after-free:
a test that reads freed memory prints arbitrary bytes, and every runner in
`pipeline_core` decoded child output strictly (`text=True` with no
`errors=`). Replacement keeps every valid byte, so libtest's lines, panic
messages and sanitizer reports parse unchanged."""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

from pipeline_core.adapters.coverage_owner import _run as coverage_run
from pipeline_core.adapters.process import run_bounded
from pipeline_core.adapters.task2_roles import CargoBackendAdapter

_ROOT = Path(__file__).resolve().parent.parent
_PRINT_BAD = [sys.executable, "-c",
              "import sys; sys.stdout.buffer.write(b'ok\\xc0\\xff\\n'); "
              "sys.stderr.buffer.write(b'err\\xc0\\n')"]


def test_run_bounded_reads_invalid_utf8_with_replacement(tmp_path: Path) -> None:
    r = run_bounded(_PRINT_BAD, cwd=tmp_path, env=None, timeout=30)
    assert r.returncode == 0
    assert r.stdout == "ok��\n" and r.stderr == "err�\n"


def test_the_untimed_coverage_runner_reads_it_too(tmp_path: Path) -> None:
    r = coverage_run(_PRINT_BAD, tmp_path)
    assert r.returncode == 0 and "ok�" in r.stdout


def test_run_raw_reads_invalid_utf8_with_replacement(tmp_path: Path) -> None:
    def _unused(*_a: object, **_k: object) -> object:
        raise AssertionError("run_raw does not use the injected callables")

    adapter = CargoBackendAdapter(
        run_fn=_unused, run_check_diag_fn=_unused, build_test_cmd_fn=_unused,
        env_fn=_unused, cluster_env_fn=_unused,
        implicated_fns_from_check_fn=_unused, attribute_failures_fn=_unused,
        filter_test_targets_fn=_unused)
    code, out, err = adapter.run_raw(
        _PRINT_BAD, tmp_path, env={}, timeout=30)
    assert code == 0 and out == "ok��\n" and err == "err�\n"


def test_control_a_strict_decode_of_the_same_output_raises(tmp_path: Path) -> None:
    """The defect, reproduced: the call the runners used to make."""
    with pytest.raises(UnicodeDecodeError):
        subprocess.run(_PRINT_BAD, cwd=tmp_path, capture_output=True,
                       text=True, encoding="utf-8")


def _strict_text_calls(path: Path) -> list[str]:
    out: list[str] = []
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        kws = {k.arg: k for k in node.keywords if k.arg}
        t = kws.get("text") or kws.get("universal_newlines")
        if (t is not None and isinstance(t.value, ast.Constant)
                and t.value.value is True and "errors" not in kws):
            out.append(f"{path.relative_to(_ROOT)}:{node.lineno}")
    return out


def _strict_child_decodes(path: Path) -> list[str]:
    """`.decode(` whose receiver is an attribute named `stdout` or `stderr`
    and that names no `errors=`. A name that happens to hold those bytes
    is not child output: source round-trips and `TimeoutExpired` args are
    not this crash."""
    out: list[str] = []
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr != "decode":
            continue
        recv = func.value
        if not isinstance(recv, ast.Attribute) or recv.attr not in (
                "stdout", "stderr"):
            continue
        if not any(k.arg == "errors" for k in node.keywords):
            out.append(f"{path.relative_to(_ROOT)}:{node.lineno}")
    return out


def test_no_call_in_the_pipeline_decodes_child_output_strictly() -> None:
    """Gate: every `text=True` call names `errors=`, and every `.decode(`
    of a child's `stdout` or `stderr` does too. A new strict decode of a
    child's output is the crash this file exists for."""
    files = [_ROOT / "pipeline.py", *sorted((_ROOT / "pipeline_core").rglob("*.py"))]
    strict = [hit for f in files for hit in _strict_text_calls(f)]
    strict.extend(hit for f in files for hit in _strict_child_decodes(f))
    assert not strict, "strict child-output decode(s): " + ", ".join(strict)
