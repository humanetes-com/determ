"""The compile frontier reads the BUILD phase of a `cargo test` capture only.

A test that compiles something itself (a compile-fail harness rebuilding the
crate in another cfg world) prints rustc diagnostics after `running N tests`.
Those are what the test said, not the sweep's build failing. Before the cut,
`extract_tdd_compile_frontier` parsed the whole stream and put the bodies those
nested diagnostics pointed at on the frontier under `<compile>` while the tree
compiled and its tests ran (run `603120-24aabfc5`, 2026-09-16).

The crate below is invented; nothing here names a live test or unit.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from pipeline_core.application.reconstruct import attribution as attr
from pipeline_core.domain.models import FnLocator

_LIB = (
    "pub struct Widget { a: u8 }\n"
    "pub fn make_widget() -> Widget {\n"
    "    Widget { a: 1 }\n"
    "}\n"
    "pub fn other() -> u8 { 0 }\n"
)


class _Structure:
    def __init__(self, recs: list[dict[str, Any]]) -> None:
        self._recs = recs

    def enumerate_fns(self, _src_dir: Path) -> list[dict[str, Any]]:
        return self._recs


def _crate(tmp_path: Path) -> tuple[Path, attr.AttributionDeps]:
    root = tmp_path / "crate"
    (root / "src").mkdir(parents=True)
    raw = _LIB.encode("utf-8")
    (root / "src/lib.rs").write_bytes(raw)
    mk = raw.index(b"pub fn make_widget")
    ot = raw.index(b"pub fn other")
    recs = [
        {"file": "src/lib.rs", "fn_name": "make_widget", "container": "",
         "sig_byte_start": mk, "body_byte_end": ot},
        {"file": "src/lib.rs", "fn_name": "other", "container": "",
         "sig_byte_start": ot, "body_byte_end": len(raw)},
    ]
    deps = attr.AttributionDeps(
        structure=_Structure(recs),
        classify=lambda **_k: {},
        locate_fn=lambda *_a, **_k: None,
        run_check_diag=lambda *_a, **_k: (0, "", []),
        derive_check_only_cmd=lambda c: c,
        is_gen_tests_file=lambda _f: False,
        log=lambda _m: None,
    )
    return root, deps


def _nested(root: Path) -> str:
    """A clean build, then a harness test whose nested build fails in a cfg
    world the sweep does not compile — the shape cargo relays verbatim."""
    return (
        "   Compiling widgets v0.1.0\n"
        "    Finished `test` profile [unoptimized + debuginfo] target(s)\n"
        "     Running unittests src/lib.rs (target/debug/deps/widgets-1)\n"
        "\nrunning 0 tests\n\ntest result: ok. 0 passed; 0 failed\n\n"
        "     Running tests/harness.rs (target/debug/deps/harness-2)\n"
        "\nrunning 1 test\n"
        "error[E0063]: missing field `b` in initializer of `Widget`\n"
        f"   --> {root}/src/lib.rs:3:5\n"
        "error: could not compile `widgets` (lib) due to 1 previous error\n"
        "test nested ... FAILED\n"
        "\ntest result: FAILED. 0 passed; 1 failed\n"
    )


def _red(root: Path) -> str:
    """A genuine red build: cargo runs no binary at all."""
    return (
        "   Compiling widgets v0.1.0\n"
        "error[E0063]: missing field `b` in initializer of `Widget`\n"
        f"   --> {root}/src/lib.rs:3:5\n"
        "error: could not compile `widgets` (lib) due to 1 previous error\n"
    )


def test_nested_build_diagnostic_after_running_is_not_a_compile_frontier(
        tmp_path: Path) -> None:
    root, deps = _crate(tmp_path)
    frontier, sites = attr.extract_tdd_compile_frontier(deps, _nested(root), root)
    assert frontier == {} and sites == {}


def test_negative_control_whole_stream_reading_attributes_the_nested_error(
        tmp_path: Path, monkeypatch: Any) -> None:
    """The pre-fix reader: the whole stream parsed, the nested diagnostic owns
    `make_widget` under the `<compile>` sentinel."""
    root, deps = _crate(tmp_path)
    monkeypatch.setattr(attr, "build_phase_output", lambda out: out)
    frontier, _ = attr.extract_tdd_compile_frontier(deps, _nested(root), root)
    assert frontier == {FnLocator("src/lib.rs", "make_widget"): {"<compile>"}}


def test_genuine_red_build_still_attributes(tmp_path: Path) -> None:
    root, deps = _crate(tmp_path)
    frontier, _ = attr.extract_tdd_compile_frontier(deps, _red(root), root)
    assert frontier == {FnLocator("src/lib.rs", "make_widget"): {"<compile>"}}
