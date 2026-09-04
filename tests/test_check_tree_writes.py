"""The tree-write gate: a bare `write_text`/`write_bytes` in a tree-touching
module fails the build unless it is the transaction module or an allowlisted
site with a reason.

Plan sentence: a fixture writer module with a bare write on a tree path fails
the gate; the same write routed through TreeTxn passes. Negative control: the
gate with the fixture's function allowlisted passes, which is the only other
way in — a reviewable line, not silence.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path


def _gate():
    path = Path(__file__).resolve().parents[1] / "tools" / "check_tree_writes.py"
    spec = importlib.util.spec_from_file_location("check_tree_writes", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gate = _gate()

_BARE = '''
from pathlib import Path

def reseed(src_dir: Path, text: str) -> None:
    (src_dir / "src" / "a.rs").write_text(text, encoding="utf-8")
'''

_THROUGH_TXN = '''
from pathlib import Path
from pipeline_core.application.reconstruct.tree_txn import TreeTxn

def reseed(src_dir: Path, text: str) -> None:
    txn = TreeTxn(src_dir)
    txn.write("src/a.rs", text)
    txn.close()
'''


def _run_on(tmp_path: Path, monkeypatch, source: str, allow: dict | None = None) -> int:
    pkg = tmp_path / "pipeline_core"
    (pkg / "application" / "reconstruct").mkdir(parents=True)
    (pkg / "application" / "reconstruct" / "fixture_writer.py").write_text(
        source, encoding="utf-8")
    (pkg / "application" / "reconstruct" / "tree_txn.py").write_text(
        "def x(p):\n    p.write_bytes(b'')\n", encoding="utf-8")
    monkeypatch.setattr(gate, "PKG", pkg)
    monkeypatch.setattr(gate, "SCANNED", ())
    monkeypatch.setattr(gate, "ALLOWLIST", dict(allow or {}))
    return gate.main()


def test_a_bare_write_in_a_tree_module_fails_the_gate(tmp_path, monkeypatch, capsys) -> None:
    assert _run_on(tmp_path, monkeypatch, _BARE) == 1
    out = capsys.readouterr().out
    assert "fixture_writer.py" in out and "`reseed`" in out and "tree_txn" in out


def test_the_same_write_through_the_transaction_passes(tmp_path, monkeypatch) -> None:
    assert _run_on(tmp_path, monkeypatch, _THROUGH_TXN) == 0


def test_the_transaction_module_itself_is_exempt(tmp_path, monkeypatch) -> None:
    # The fixture tree_txn.py above carries a bare write_bytes and is the
    # only module allowed to.
    assert _run_on(tmp_path, monkeypatch, _THROUGH_TXN) == 0


def test_an_allowlist_entry_is_the_only_other_way_in(tmp_path, monkeypatch) -> None:
    allow = {("application/reconstruct/fixture_writer.py", "reseed"):
             "test: a byte restore of a pre-image captured in the same function"}
    assert _run_on(tmp_path, monkeypatch, _BARE, allow) == 0


def test_a_stale_allowlist_entry_fails_too(tmp_path, monkeypatch, capsys) -> None:
    allow = {("application/reconstruct/fixture_writer.py", "gone"): "no such write"}
    assert _run_on(tmp_path, monkeypatch, _THROUGH_TXN, allow) == 1
    assert "stale" in capsys.readouterr().out


def test_the_live_tree_passes_the_gate() -> None:
    """The gate over the real package: every remaining bare write is either
    the transaction module or allowlisted with a reason (the migration this
    gate ships with). A regression here is a new unguarded writer."""
    assert gate.main() == 0
