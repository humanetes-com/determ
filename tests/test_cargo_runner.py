"""Pins for application/reconstruct/cargo_runner.py (P2 slice 6). The runner
composes the Cargo PORT + diagnostics_from_json; a fake Cargo lets us assert
the flag insertion, rendered-text reconstruction, and rc=-1 parity without a
real cargo binary."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pipeline_core.application.reconstruct.cargo_runner import (
    run_cargo,
    run_check_diag,
    run_scoped_tests,
    u2_test_gate,
)


@dataclass
class _Proc:
    returncode: int
    stdout: str
    stderr: str


class _FakeCargo:
    """Records the cmd it was handed and returns a canned Process."""

    def __init__(self, proc: _Proc) -> None:
        self.proc = proc
        self.seen_cmd: str | None = None

    def run(self, cmd: str, cwd: Path, *, env: dict[str, str], timeout: int = 180) -> _Proc:
        self.seen_cmd = cmd
        return self.proc


_ERR_JSON = (
    '{"reason":"compiler-message","message":{"level":"error",'
    '"code":{"code":"E0308"},"message":"mismatched","spans":'
    '[{"is_primary":true,"file_name":"src/e.rs","line_start":10}],'
    '"rendered":"error[E0308]: mismatched\\n --> src/e.rs:10"}}'
)


def test_inserts_message_format_after_subcommand() -> None:
    fake = _FakeCargo(_Proc(0, "", ""))
    run_check_diag(fake, "cargo +nightly check --all", Path("."), {})
    assert fake.seen_cmd is not None
    toks = fake.seen_cmd.split()
    assert toks[:3] == ["cargo", "+nightly", "check"]
    assert toks[3] == "--message-format=json-diagnostic-rendered-ansi"


def test_does_not_double_insert_flag() -> None:
    fake = _FakeCargo(_Proc(0, "", ""))
    run_check_diag(fake, "cargo check --message-format=json", Path("."), {})
    assert fake.seen_cmd is not None
    assert fake.seen_cmd.count("--message-format") == 1


def test_parses_diagnostics_and_rebuilds_rendered() -> None:
    fake = _FakeCargo(_Proc(101, _ERR_JSON + "\nnon-json tail line", ""))
    rc, rendered, diags = run_check_diag(fake, "cargo check", Path("."), {})
    assert rc == 101
    assert len(diags) == 1 and diags[0].code == "E0308" and diags[0].file == "src/e.rs"
    # non-JSON lines kept; each diagnostic's rendered field appended
    assert "non-json tail line" in rendered
    assert "error[E0308]: mismatched" in rendered
    # the raw JSON line must NOT survive into the rendered view
    assert '"reason"' not in rendered


def test_timeout_and_not_found_parity() -> None:
    # adapter encodes timeout / binary-not-found as rc=-1 with the message in stderr
    fake = _FakeCargo(_Proc(-1, "", "TIMEOUT after 180s"))
    rc, text, diags = run_check_diag(fake, "cargo check", Path("."), {})
    assert (rc, text, diags) == (-1, "TIMEOUT after 180s", [])


def test_run_cargo_combined_layout() -> None:
    fake = _FakeCargo(_Proc(0, "out text", "warn text"))
    assert run_cargo(fake, "cargo build", Path("."), {}) == (0, "out text\n--- stderr ---\nwarn text")
    # no stderr → no separator block
    fake2 = _FakeCargo(_Proc(101, "only out", ""))
    assert run_cargo(fake2, "cargo build", Path("."), {}) == (101, "only out")
    # rc=-1 → bare stderr message, no combine
    fake3 = _FakeCargo(_Proc(-1, "", "FILE NOT FOUND: cargo"))
    assert run_cargo(fake3, "cargo build", Path("."), {}) == (-1, "FILE NOT FOUND: cargo")


def _const_run_cargo(rc: int, out: str):
    """A fake `run_cargo` callable: records the cmd, returns canned (rc, out)."""
    seen: dict[str, str] = {}

    def _fn(cmd: str, cwd: Path, env: dict[str, str], *, timeout: int = 180) -> tuple[int, str]:
        seen["cmd"] = cmd
        return rc, out

    _fn.seen = seen  # type: ignore[attr-defined]
    return _fn


def test_run_scoped_tests_pass_fail_timeout() -> None:
    # all pass: rc==0, no FAILED lines
    assert run_scoped_tests(_const_run_cargo(0, "test a ... ok\ntest b ... ok"),
                            "cargo test", Path("."), {}, err_cap=500) == (True, 0, "", set())
    # failures: rc!=0 + FAILED rows → not passed, detail tail, names parsed
    f = _const_run_cargo(101, "test a ... ok\ntest b ... FAILED\npanic: boom")
    passed, n, detail, names = run_scoped_tests(f, "cargo test", Path("."), {}, err_cap=500)
    assert passed is False and n == 1 and names == {"b"} and detail.endswith("panic: boom")
    assert f.seen["cmd"] == "cargo test"  # type: ignore[attr-defined]
    # rc==0 but a FAILED line present → still not passed (defensive)
    p2, n2, _, names2 = run_scoped_tests(_const_run_cargo(0, "test x ... FAILED"),
                                         "cargo test", Path("."), {}, err_cap=500)
    assert p2 is False and n2 == 1 and names2 == {"x"}
    # timeout sentinel (rc=-1) → inconclusive
    assert run_scoped_tests(_const_run_cargo(-1, ""), "cargo test", Path("."), {},
                            err_cap=500) == (False, -1, "scoped test run timed out", set())
    # err_cap truncates the detail tail
    _, _, d, _ = run_scoped_tests(_const_run_cargo(101, "x" * 100 + "\ntest z ... FAILED"),
                                  "cargo test", Path("."), {}, err_cap=10)
    assert len(d) == 10


def _gate(run_cargo_fn, fn_tests, test_cmd="cargo test", enabled=True, guard=None):
    return u2_test_gate(
        run_cargo_fn, fn_tests, test_cmd, Path("."), {},
        enabled=enabled, timeout=120, err_cap=500,
        guard=guard or (lambda *_: None))


def test_u2_gate_accepts_when_disabled_or_no_tests() -> None:
    boom = _const_run_cargo(101, "test a ... FAILED")
    assert _gate(boom, {"a"}, enabled=False) is None      # gate off
    assert _gate(boom, set()) is None                     # no fn_tests
    assert _gate(boom, {"a"}, test_cmd=None) is None       # no cmd


def test_u2_gate_accepts_when_tests_pass() -> None:
    assert _gate(_const_run_cargo(0, "test a ... ok"), {"a"}) is None


def test_u2_gate_blocks_on_own_failing_test() -> None:
    out = "test a ... ok\ntest b ... FAILED\nassert failed"
    res = _gate(_const_run_cargo(101, out), {"a", "b"})
    assert res is not None
    sig, msg = res
    assert sig == frozenset({"b"}) and "still FAIL" in msg and msg.endswith("assert failed")


def test_u2_gate_ignores_unrelated_failures() -> None:
    # a failing test NOT in fn_tests must not block this unit
    assert _gate(_const_run_cargo(101, "test other ... FAILED"), {"a"}) is None


def test_u2_gate_failsafe_and_inconclusive() -> None:
    # run_cargo raises → guard fires, accept (None)
    fired = []

    def _raise(*_a, **_k):
        raise RuntimeError("cargo blew up")

    assert _gate(_raise, {"a"}, guard=lambda loc, m: fired.append(loc)) is None
    assert fired == ["phase-c/u2_skip"]
    # timeout (rc=-1) → empty failed set → accept
    assert _gate(_const_run_cargo(-1, ""), {"a"}) is None
