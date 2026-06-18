"""build_test_reverse_map must index inline `#[cfg(test)]` unit tests living in
src/ (not just tests/*.rs), so a failing inline test attributes to the production
fn it exercises instead of dying with `no_actionable_refs` (the hardcrate
`extra_cap_limit` → `cap_limit` churn)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from pipeline_core.application.reconstruct.attribution import (
    AttributionDeps,
    build_test_reverse_map,
)


class _FakeStructure:
    def __init__(self, recs: list[dict[str, Any]]) -> None:
        self._recs = recs

    def enumerate_fns(self, _src_dir: Path) -> list[dict[str, Any]]:
        return self._recs


def _deps(recs: list[dict[str, Any]]) -> AttributionDeps:
    return AttributionDeps(
        structure=_FakeStructure(recs),
        classify=lambda **_k: {},
        locate_fn=lambda *_a, **_k: None,
        run_check_diag=lambda *_a, **_k: (0, "", []),
        derive_check_only_cmd=lambda c: c,
        is_gen_tests_file=lambda _f: False,
        log=lambda _m: None,
    )


def test_inline_cfg_test_maps_to_production_fn(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    body = (
        "pub fn cap_limit(n: u32) -> u32 { n.min(255) }\n"
        "#[cfg(test)]\n"
        "mod tests {\n"
        "    use super::*;\n"
        "    #[test]\n"
        "    fn extra_cap_limit() { assert_eq!(cap_limit(999), 255); }\n"
        "}\n"
    )
    raw = body.encode("utf-8")
    (src / "lib.rs").write_bytes(raw)

    test_start = raw.index(b"    fn extra_cap_limit")
    recs = [
        {"file": "lib.rs", "fn_name": "cap_limit", "cfg_predicates": []},
        {
            "file": "lib.rs", "fn_name": "extra_cap_limit",
            "cfg_predicates": ["test"],
            "sig_byte_start": test_start,
            "body_byte_end": len(raw),
        },
    ]
    rmap = build_test_reverse_map(_deps(recs), [], src)

    assert ("lib.rs", "cap_limit") in rmap["extra_cap_limit"]
    # The test fn is never a candidate (immutable oracle, FR-018).
    assert ("lib.rs", "extra_cap_limit") not in rmap["extra_cap_limit"]


def test_no_src_tests_yields_empty(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "lib.rs").write_bytes(b"pub fn f() {}\n")
    recs = [{"file": "lib.rs", "fn_name": "f", "cfg_predicates": []}]
    assert build_test_reverse_map(_deps(recs), [], src) == {}
