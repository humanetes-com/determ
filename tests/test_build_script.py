"""Pure parser for cargo build-script `output` files."""
from __future__ import annotations

from pipeline_core.domain.build_script import BuildScriptCfg, parse_build_script_output


def test_parses_set_and_declared_anyhow_shape() -> None:
    text = (
        "cargo:rustc-cfg=error_generic_member_access\n"
        "cargo:rustc-check-cfg=cfg(anyhow_build_probe)\n"
        "cargo:rustc-check-cfg=cfg(anyhow_nightly_testing)\n"
        "cargo:rustc-check-cfg=cfg(anyhow_no_clippy_format_args)\n"
        "cargo:rustc-check-cfg=cfg(error_generic_member_access)\n"
        "cargo:rerun-if-env-changed=RUSTC_BOOTSTRAP\n"
    )
    facts = parse_build_script_output(text)
    assert facts.set_cfgs == frozenset({"error_generic_member_access"})
    assert facts.declared_cfgs == frozenset({
        "anyhow_build_probe", "anyhow_nightly_testing",
        "anyhow_no_clippy_format_args", "error_generic_member_access"})


def test_handles_double_colon_and_keyvalue_and_empty() -> None:
    text = (
        'cargo::rustc-cfg=feature="x"\n'          # 1.77+ syntax + key=value → leading ident
        "cargo::rustc-check-cfg=cfg(feature, values(\"a\",\"b\"))\n"
        "unrelated line\n"
    )
    facts = parse_build_script_output(text)
    assert facts.set_cfgs == frozenset({"feature"})
    assert facts.declared_cfgs == frozenset({"feature"})
    assert parse_build_script_output("") == BuildScriptCfg.empty()