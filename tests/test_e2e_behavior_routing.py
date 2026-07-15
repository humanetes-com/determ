"""Behavioral failures (compile clean, no stub panics, assertions failing)
must route to the per-fn body repair — NOT the legacy whole-file rewrite.

Same observable trick as test_e2e_stub_routing: the scripted model returns
only a <FN> block, which the per-fn path can splice and the whole-file path
cannot parse. File changed ⇒ per-fn path taken.
"""

from __future__ import annotations

import pipeline
from tests.harness import install_app_seams

_WRONG_SRC = "pub fn add(a: u32, b: u32) -> u32 { 0 }\n"
_PER_FN_RESPONSE = '<FN name="add" block="any">\na + b\n</FN>\n'


def test_behavior_failure_routes_to_per_fn_not_whole_file(monkeypatch, tmp_path):
    ws = tmp_path / "ws"
    (ws / "src").mkdir(parents=True)
    lib = ws / "src" / "lib.rs"
    lib.write_text(_WRONG_SRC, encoding="utf-8")

    # No compile errors, no unimplemented stubs — only a behavioral failure.
    install_app_seams(
        monkeypatch,
        compile_error_frontier=lambda *a, **k: {},
        extract_tdd_frontier=lambda *a, **k: {},
        behavior_frontier=lambda *a, **k: (
            {("src/lib.rs", "add"): {"test_add"}},
            {"test_add": ("tests/t.rs",
                          "fn test_add() { assert_eq!(add(1, 2), 3); }")},
        ),
        collect_impl_blocks=lambda *a, **k: [],
    )
    monkeypatch.setattr(pipeline, "_ask_raw", lambda *a, **k: _PER_FN_RESPONSE)

    pipeline.generate_fix_loop(
        project_dir=ws,
        check_cmd="cargo test",
        source_paths=[lib],
        existing_test_files=set(),
    )

    text = lib.read_text(encoding="utf-8")
    assert "a + b" in text, (
        "Expected behavior route to splice 'a + b' into add; "
        "whole-file path was taken instead (file unchanged or corrupted)."
    )
