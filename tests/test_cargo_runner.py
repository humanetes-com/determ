"""Pins for application/reconstruct/cargo_runner.py (P2 slice 6). The runner
composes the Cargo PORT + diagnostics_from_json; a fake Cargo lets us assert
the flag insertion, rendered-text reconstruction, and rc=-1 parity without a
real cargo binary."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pipeline_core.application.reconstruct.cargo_runner import run_cargo, run_check_diag


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
