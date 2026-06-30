"""Cluster-membership use-cases — fake-injected collaborators, no monkeypatch."""
from pipeline_core.application.reconstruct import cluster_membership as cm
from pipeline_core.application.reconstruct.cleanup_residuals import (
    filter_test_exercised,
)


def test_line_declares_mod_token_exact():
    assert cm._line_declares_mod(" mod nightly ;", "nightly")
    assert cm._line_declares_mod("pub(crate) mod ptr;", "ptr")
    # not a whole word
    assert not cm._line_declares_mod("mod nightlyx ;", "nightly")
    # 'mod' must be a word
    assert not cm._line_declares_mod("kommode nightly ;", "nightly")
    # needs the semicolon
    assert not cm._line_declares_mod("mod nightly { }", "nightly")


def test_file_cfg_predicate_regex_fallback_adjacent_mods(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "lib.rs").write_text(
        " # [cfg (gated_feature)] mod nightly ;\n"
        " mod ptr ;\n"
        " mod wrapper ;\n",
        encoding="utf-8",
    )
    f = cm.file_cfg_predicate_regex_fallback
    assert f(tmp_path, "src/nightly.rs") == "gated_feature"
    assert f(tmp_path, "src/ptr.rs") is None
    assert f(tmp_path, "src/wrapper.rs") is None


def test_filter_test_exercised():
    stubs = [("src/a.rs", "used"), ("src/a.rs", "unused")]
    tests = [("tests/t.rs", "fn t() { used(); }")]
    assert filter_test_exercised(stubs, tests) == [("src/a.rs", "used")]
    assert filter_test_exercised([], tests) == []
    assert filter_test_exercised(stubs, []) == []
