"""Characterization for the extracted task3 inline-retry use-case (chunk H).

Proves `run_task3_inline_retry` works through CONSTRUCTOR-injected fakes (no
monkeypatch of module globals) — the DI rail the pipeline.py shim wires the
resident collaborators into. The end-to-end monkeypatch path stays covered by
tests/test_e2e_build_fix.py + test_e2e_routing.py.
"""

from __future__ import annotations

from pathlib import Path

from pipeline_core.application.reconstruct.per_fn_retry import (
    RetryDeps,
    Task3Attribution,
    Task3Cargo,
    Task3Enumerate,
    Task3RetryDeps,
    run_task3_inline_retry,
)


def _failed(n: int) -> str:
    rows = "".join(f"test t_{i} ... FAILED\n" for i in range(n))
    return f"running {n} tests\n{rows}\ntest result: FAILED. 0 passed; {n} failed\n"


def _deps(*, run=None, check_diag=None) -> Task3RetryDeps:
    def _unused(*_a, **_k):  # noqa: ANN002, ANN003
        raise AssertionError("collaborator should not be called on this path")
    return Task3RetryDeps(
        cargo=Task3Cargo(
            env=lambda: {},
            run=run or _unused,
            check_diag=check_diag or _unused,
        ),
        attribution=Task3Attribution(
            attribute_failures=_unused, build_test_reverse_map=_unused,
            frontier_from_diags=_unused, filter_test_targets=_unused,
            expand_frontier_by_calls=_unused),
        enumerator=Task3Enumerate(
            cluster_filter_type_methods=lambda recs, cl: [],
            enumerate_types=lambda root: [], fn_container_index=lambda root: {},
            fn_occurrence_index=lambda root: {},
            collect_crate_macros=lambda root: "",
            compute_cluster_macro_invariants=lambda root, cl: "",
            detect_crate_no_std_invariant=lambda root: ""),
        retry_fix=RetryDeps(
            locate_fn=_unused, locate_all_fns=_unused,
            gather_fix_context=_unused,
            build_per_fn_fix_prompt=_unused, ask_per_fn_body=_unused,
            splice_fn_body=_unused, splice_body_scoped=_unused,
            find_tests_for_fn=_unused,
            focus_cargo_errors=_unused, is_gen_tests_file=lambda n: False,
            log=lambda *_: None),
        log=lambda *_: None,
        test_timeout=10,
    )


def test_build_mode_no_reproducible_diags_bails(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    initial = "error: could not compile (no FAILED test lines)\n"
    deps = _deps(check_diag=lambda *a, **k: (1, "", []))  # build mode, no diags
    passed, out = run_task3_inline_retry(
        tmp_path, "cargo test", 1, initial, None,
        max_stalls=3, hard_cap=10, deps=deps)
    assert passed is False
    assert out == initial


def test_env_gate_disables_retry(tmp_path: Path, monkeypatch) -> None:
    """TASK3_INLINE_RETRY=0 short-circuits before touching any collaborator
    (the gate moved from the pipeline.py shim into the use-case, S27)."""
    monkeypatch.setenv("TASK3_INLINE_RETRY", "0")
    initial = _failed(1)
    passed, out = run_task3_inline_retry(
        tmp_path, "cargo test", 1, initial, None, deps=_deps())
    assert passed is False and out == initial


def test_env_default_stall_knobs(tmp_path: Path, monkeypatch) -> None:
    """Omitted max_stalls/hard_cap read TASK3_INLINE_* per call."""
    monkeypatch.setenv("TASK3_INLINE_MAX_STALLS", "1")
    (tmp_path / "src").mkdir()
    initial = "error: could not compile (no FAILED test lines)\n"
    deps = _deps(check_diag=lambda *a, **k: (1, "", []))  # build mode, no diags
    passed, out = run_task3_inline_retry(
        tmp_path, "cargo test", 1, initial, None, deps=deps)
    assert passed is False and out == initial


def test_build_mode_import_repair_fixes_without_llm(tmp_path: Path) -> None:
    """An E0425-class failure whose only errors carry rustc's unambiguous
    "consider importing" suggestion is repaired deterministically — no
    per-fn LLM fix attempt is burned (retry_fix collaborators would raise)."""
    from pipeline_core.domain.import_repair import ImportFix
    from pipeline_core.domain.models import Diagnostic

    (tmp_path / "src").mkdir()
    src = tmp_path / "src" / "lib.rs"
    src.write_text("fn f() -> u32 { helper() }\n")
    fix = ImportFix(file="src/lib.rs", byte_start=0, byte_end=0,
                    replacement="use crate::util::helper;\n")
    diag = Diagnostic(code="E0425", file="src/lib.rs", line=1,
                      message="cannot find function `helper`",
                      import_fixes=(fix,))
    calls = {"n": 0}

    def _check_diag(*a, **k):
        calls["n"] += 1
        # Errors present until the import lands in the file, then clean.
        if "use crate::util::helper;" in src.read_text():
            return 0, "", []
        return 1, "error[E0425]", [diag]

    initial = "error: could not compile (no FAILED test lines)\n"
    deps = _deps(check_diag=_check_diag)
    passed, _out = run_task3_inline_retry(
        tmp_path, "cargo test --no-run", 1, initial, None,
        max_stalls=3, hard_cap=10, deps=deps)
    # Build mode reports progress via RECURSE (task3 re-verifies) — what the
    # pin guards is the deterministic repair landing with zero LLM calls
    # (any retry_fix collaborator call would have raised in _deps()).
    assert passed is False
    assert "use crate::util::helper;" in src.read_text()


def test_test_mode_passes_immediately(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    initial = _failed(2)  # has FAILED lines → TEST mode
    ok_out = "test result: ok. 2 passed; 0 failed\n"
    deps = _deps(run=lambda *a, **k: (0, ok_out))  # first check → pass
    passed, out = run_task3_inline_retry(
        tmp_path, "cargo test", 1, initial, None,
        max_stalls=3, hard_cap=10, deps=deps)
    assert passed is True
    assert out == ok_out


def test_the_prompt_says_what_the_cluster_name_means(tmp_path: Path) -> None:
    """The retry named the cluster in its header and passed `invariants=""` /
    `macros=""`, so the prompt carried no CLUSTER_INVARIANTS block at all
    (verified on run 741610-02561583's prompt store, 97 671 chars). And the tree
    it repairs is compiled by EVERY cluster, so one cluster's invariants would
    still be the wrong statement — all of them are rendered, each named.
    """
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lib.rs").write_text("fn f() { unimplemented!() }\n")
    (tmp_path / "tests").mkdir()
    seen: dict[str, object] = {}

    class _C:
        def __init__(self, name: str) -> None:
            self.name, self.toolchain, self.env_extra = name, "", {}

    deps = _deps(run=lambda *a, **k: (101, _failed(1)))
    from dataclasses import replace
    enum = replace(
        deps.enumerator,
        compute_cluster_macro_invariants=lambda root, cl: (
            f"backtrace_if_absent!(...) active for {cl.name}"),
        detect_crate_no_std_invariant=lambda root: "crate is no_std",
        fn_container_index=lambda root: {},
        fn_occurrence_index=lambda root: {},
    )
    fix = replace(
        deps.retry_fix,
        locate_fn=lambda text, fn: ("fn f()", 0, 9, 26),
        locate_all_fns=lambda text, fn: [],
        gather_fix_context=lambda **kw: type(
            "C", (), {"container": "", "types_section": "",
                      "notes_section": "", "bound_methods_section": "",
                      "blueprint_section": ""})(),
        find_tests_for_fn=lambda *a, **kw: [],
        focus_cargo_errors=lambda out, rel: "",
        build_per_fn_fix_prompt=lambda **kw: seen.update(
            sections=kw["sections"]) or "prompt",
        ask_per_fn_body=lambda *a, **kw: (None, ""),
    )
    attr = replace(
        deps.attribution,
        build_test_reverse_map=lambda files, root: {},
        attribute_failures=lambda *a, **kw: type(
            "R", (), {"frontier": {("src/lib.rs", "f"): {"t_0"}}})(),
        filter_test_targets=lambda fr, root: fr,
        expand_frontier_by_calls=lambda fr, root, **kw: (fr, dict.fromkeys(fr, 0)),
    )
    deps = replace(deps, enumerator=enum, retry_fix=fix, attribution=attr)
    run_task3_inline_retry(
        tmp_path, "cargo test", 1, _failed(1), _C("nightly"),
        all_clusters=[_C("nightly"), _C("stable")],
        max_stalls=1, hard_cap=1, deps=deps)
    inv = seen["sections"].invariants  # type: ignore[union-attr]
    assert "nightly" in inv and "stable" in inv, \
        "a shared artifact's prompt must name every cfg world it must satisfy"
    assert "crate is no_std" in inv
    assert "[cluster: nightly]" in inv and "[cluster: stable]" in inv


def test_the_prompt_names_the_cfg_atoms_not_just_the_cluster_names(
        tmp_path: Path) -> None:
    """The names alone left the model inferring the atoms, and the recorded
    burn is an unguarded read of a `#[cfg(all(not(error_generic_member_access),
    feature = "std"))]` field: green in the default world, E0609 under the
    nightly cluster's RUSTFLAGS, re-spliced identically for three rounds with
    the mismatch never named in the prompt (run `3579062-476abc3b`,
    2026-08-19). The invariants block must carry each world's active/absent
    predicates and the same-predicate guard rule."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lib.rs").write_text("fn f() { unimplemented!() }\n")
    (tmp_path / "tests").mkdir()
    seen: dict[str, object] = {}

    class _C:
        def __init__(self, name: str, active: list[str],
                     excluded: list[str]) -> None:
            self.name, self.toolchain, self.env_extra = name, "", {}
            self.active_cfg, self.excluded_cfg = active, excluded

    deps = _deps(run=lambda *a, **k: (101, _failed(1)))
    from dataclasses import replace
    enum = replace(
        deps.enumerator,
        compute_cluster_macro_invariants=lambda root, cl: "",
        detect_crate_no_std_invariant=lambda root: "",
        fn_container_index=lambda root: {},
        fn_occurrence_index=lambda root: {},
    )
    fix = replace(
        deps.retry_fix,
        locate_fn=lambda text, fn: ("fn f()", 0, 9, 26),
        locate_all_fns=lambda text, fn: [],
        gather_fix_context=lambda **kw: type(
            "C", (), {"container": "", "types_section": "",
                      "notes_section": "", "bound_methods_section": "",
                      "blueprint_section": ""})(),
        find_tests_for_fn=lambda *a, **kw: [],
        focus_cargo_errors=lambda out, rel: "",
        build_per_fn_fix_prompt=lambda **kw: seen.update(
            sections=kw["sections"]) or "prompt",
        ask_per_fn_body=lambda *a, **kw: (None, ""),
    )
    attr = replace(
        deps.attribution,
        build_test_reverse_map=lambda files, root: {},
        attribute_failures=lambda *a, **kw: type(
            "R", (), {"frontier": {("src/lib.rs", "f"): {"t_0"}}})(),
        filter_test_targets=lambda fr, root: fr,
        expand_frontier_by_calls=lambda fr, root, **kw: (fr, dict.fromkeys(fr, 0)),
    )
    deps = replace(deps, enumerator=enum, retry_fix=fix, attribution=attr)
    run_task3_inline_retry(
        tmp_path, "cargo test", 1, _failed(1),
        _C("nightly", ["error_generic_member_access"], []),
        all_clusters=[_C("nightly", ["error_generic_member_access"], []),
                      _C("stable", [], ["error_generic_member_access"])],
        max_stalls=1, hard_cap=1, deps=deps)
    inv = seen["sections"].invariants  # type: ignore[union-attr]
    assert "`error_generic_member_access` IS active" in inv
    assert "DO NOT EXIST in this world" in inv
    assert "SAME predicate" in inv
