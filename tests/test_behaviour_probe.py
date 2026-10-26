"""Pins for the behaviour probe: reply parsing, stdout normalisation, target
selection (domain) and the example-file runner (adapter, subprocess faked)."""

from __future__ import annotations

from pathlib import Path

from pipeline_core.adapters.behaviour_probe import (
    PROBE_PREFIX,
    materialized_probe,
    run_probe,
    safe_probe_name,
)
from pipeline_core.domain.behaviour import (
    BehaviourClass,
    BehaviourProvenance,
    BehaviourSpec,
    OutcomeKind,
)
from pipeline_core.domain.behaviour_probe import (
    batches,
    normalize_stdout,
    parse_behaviour_programs,
    probe_spec,
    probe_targets,
    probe_unit_label,
)
from pipeline_core.domain.models import BuildCluster, FnRecord

_REPLY = """<think>plan</think>
Here are the programs.
<BEHAVIOUR unit="Error::msg">
```rust
use anyhow::Error;
fn main() { println!("{}", Error::msg("x")); }
```
</BEHAVIOUR>
<BEHAVIOUR unit='root_cause'>
fn main() { println!("{}", anyhow::anyhow!("a").root_cause()); }
</BEHAVIOUR>
<BEHAVIOUR unit="empty">
</BEHAVIOUR>
"""


def test_parse_programs_strips_think_and_fences_and_drops_empty():
    got = parse_behaviour_programs(_REPLY)
    assert [u for u, _ in got] == ["Error::msg", "root_cause"]
    assert got[0][1].startswith("use anyhow::Error;") and "```" not in got[0][1]
    assert got[1][1].startswith("fn main()")
    assert parse_behaviour_programs("no blocks here") == []
    assert parse_behaviour_programs('<BEHAVIOUR unit="x"') == []
    assert parse_behaviour_programs('<BEHAVIOUR unit="x">fn main() {}') == []
    assert parse_behaviour_programs("<BEHAVIOUR>fn main() { 1 }</BEHAVIOUR>") == [("", "fn main() { 1 }")]
    assert parse_behaviour_programs("<BEHAVIOUR unit=x>fn main() {}</BEHAVIOUR>") == [("", "fn main() {}")]
    assert parse_behaviour_programs('<BEHAVIOUR unit="x>fn main() {}</BEHAVIOUR>') == [("", "fn main() {}")]
    assert parse_behaviour_programs('<BEHAVIOUR unit="x">```</BEHAVIOUR>') == []
    assert parse_behaviour_programs('<BEHAVIOUR unit="x">```rust\nfn main() {}\n```</BEHAVIOUR>') == [
        ("x", "fn main() {}")
    ]
    assert parse_behaviour_programs("<BEHAVIOUR unit>fn main() { 2 }</BEHAVIOUR>") == [("", "fn main() { 2 }")]
    spaced = '<BEHAVIOUR unit= "z">fn main() { 3 }</BEHAVIOUR>'
    assert parse_behaviour_programs(spaced) == [("z", "fn main() { 3 }")]


def test_normalize_stdout_strips_trailing_ws_and_blank_tail():
    assert normalize_stdout("a  \nb\t\n\n\n") == "a\nb"
    assert normalize_stdout("") == ""


def _rec(
    name: str, *, pub: bool = True, container: str = "", file: str = "src/lib.rs", is_test: bool = False
) -> FnRecord:
    return FnRecord(file=file, fn_name=name, is_pub=pub, is_exactly_pub=pub, container=container, is_test=is_test)


def test_probe_targets_are_public_fns_no_spec_names():
    specs = (BehaviourSpec("a", 'let e = anyhow!("x");', "e.root_cause()", None),)
    recs = [
        _rec("root_cause"),
        _rec("msg", container="impl Error"),
        _rec("private", pub=False),
        _rec("t", is_test=True),
        _rec("helper", file="tests/common/mod.rs"),
        _rec("chain", container="impl Error"),
    ]
    assert probe_targets(recs, specs, max_targets=12) == ("Error::chain", "Error::msg")
    assert probe_targets(recs, specs, max_targets=1) == ("Error::chain",)
    assert probe_targets(recs, specs, max_targets=0) == ()
    assert probe_unit_label(_rec("fmt", container="impl Display for Error")) == "Error::fmt"


def test_probe_targets_honour_module_reachability_and_hidden_modules():
    # the live shape: pub methods on pub structs behind a private `mod ptr;`,
    # a cfg-gated private `mod nightly;`, and `pub mod __private` helpers.
    from pipeline_core.domain.models import ModRecord
    from pipeline_core.domain.module_reachability import externally_reachable_files

    recs = [
        _rec("msg", container="impl Error", file="src/error.rs"),
        _rec("by_ref", container="impl Own < T >", file="src/ptr.rs"),
        _rec("provide", file="src/nightly.rs"),
        FnRecord(
            file="src/lib.rs",
            fn_name="format_err",
            is_pub=True,
            is_exactly_pub=True,
            container="mod __private",
            mod_path="__private",
        ),
    ]
    mods = [
        ModRecord(name="error", file="error.rs", declared_in="src/lib.rs", vis="public"),
        ModRecord(name="ptr", file="ptr.rs", declared_in="src/lib.rs", vis="private"),
        ModRecord(
            name="nightly",
            file="nightly.rs",
            declared_in="src/lib.rs",
            vis="private",
            cfg_predicates=("error_generic_member_access",),
        ),
    ]
    # anyhow's shape: every `mod` is private, `Error` is declared at the root
    # and implemented in `mod error;`, `Own` is declared in `mod ptr;`.
    mods = [
        ModRecord(name=n, file=f"{n}.rs", declared_in="src/lib.rs", vis="private") for n in ("error", "ptr", "nightly")
    ]
    type_files = {"Error": "src/lib.rs", "Own": "src/ptr.rs"}
    reachable = externally_reachable_files(mods, frozenset(r.file for r in recs) | frozenset(type_files.values()))
    assert reachable == frozenset({"src/lib.rs"})
    assert probe_targets(recs, (), max_targets=12, reachable_files=reachable, type_files=type_files) == ("Error::msg",)
    # negative controls: visibility flags alone offer every one of them; the
    # impl's FILE alone offers none (the real API lives in private modules)
    assert probe_targets(recs, (), max_targets=12) == ("Error::msg", "Own::by_ref", "provide")
    assert probe_targets(recs, (), max_targets=12, reachable_files=reachable) == ()


def test_probe_targets_skip_fns_whose_cfg_is_false_in_the_cluster():
    from pipeline_core.domain.cfg import CfgSet

    recs = [
        _rec("msg", container="impl Error"),
        FnRecord(
            file="src/error.rs",
            fn_name="thiserror_provide",
            is_pub=True,
            is_exactly_pub=True,
            container="impl Error",
            cfg_predicates=("error_generic_member_access",),
        ),
        FnRecord(
            file="src/error.rs",
            fn_name="from_boxed",
            is_pub=True,
            is_exactly_pub=True,
            container="impl Error",
            cfg_predicates=('any (feature = "std" , not (anyhow_no_core_error))',),
        ),
    ]
    stable = CfgSet.of(set(), {"error_generic_member_access"})
    assert probe_targets(recs, (), max_targets=12, cfg_set=stable) == ("Error::from_boxed", "Error::msg")
    # negative control: without the cluster's cfg world the nightly-only fn is offered
    assert "Error::thiserror_provide" in probe_targets(recs, (), max_targets=12)


def test_batches_of_at_most_six():
    assert batches(tuple("abcdefgh"), 6) == [tuple("abcdef"), ("g", "h")]


def test_probe_spec_is_synthesized_stdout_outcome_classified():
    s = probe_spec(
        cfg_cluster="stable", unit="Error::msg", n=0, program="fn main() {}", stdout="x \n\n", default_cluster="stable"
    )
    assert s.behaviour_id == "probe::stable::Error::msg::0"
    assert s.provenance is BehaviourProvenance.SYNTHESIZED
    assert s.then.kind is OutcomeKind.NORMALIZED_STDOUT and s.then.value == "x"
    assert s.setup == "fn main() {}" and s.when == "Error::msg"
    assert s.klass is BehaviourClass.OTHER and s.is_positive_oracle


# --- adapter, subprocess faked ---------------------------------------------------


def _cluster() -> BuildCluster:
    return BuildCluster(
        name="nightly",
        env_extra={"RUSTFLAGS": "--cfg x"},
        tarpaulin_exclude_files=[],
        excluded_cfg=[],
        active_cfg=[],
        prompt_nightly_rule="",
        is_default=False,
        requires_nightly=True,
    )


def test_run_probe_writes_runs_and_always_removes_the_example(tmp_path: Path) -> None:
    seen: dict[str, object] = {}

    def fake_run(cmd: list[str], cwd: Path, env: dict[str, str], timeout: int):
        seen["cmd"] = cmd
        seen["cwd"] = cwd
        seen["env"] = env
        target = cmd[-1]
        path = cwd / "examples" / f"{target}.rs"
        seen["file_during_run"] = path.read_text(encoding="utf-8")
        return 0, "hello\n", ""

    res = run_probe(tmp_path, _cluster(), 'fn main() { println!("hello"); }', "stable_1", run=fake_run)
    assert res.ok and res.stdout == "hello\n"
    assert seen["cmd"] == ["cargo", "run", "--quiet", "--example", f"{PROBE_PREFIX}stable_1"]
    assert seen["file_during_run"] == 'fn main() { println!("hello"); }'
    assert seen["cwd"] == tmp_path
    env = seen["env"]
    assert "--cfg x" in env["RUSTFLAGS"] and env["RUSTUP_TOOLCHAIN"] == "nightly"
    assert env["CARGO_TARGET_DIR"] == str(tmp_path / "target" / "t0d-nightly")
    assert not (tmp_path / "examples").exists(), "examples/ created by the probe is removed"


def test_run_probe_merges_extra_env_over_the_cluster_env(tmp_path: Path) -> None:
    seen: dict[str, object] = {}

    def fake_run(cmd: list[str], cwd: Path, env: dict[str, str], timeout: int):
        seen["env"] = env
        return 0, "ok\n", ""

    run_probe(tmp_path, _cluster(), "fn main() {}", "e", run=fake_run, extra_env={"RUST_BACKTRACE": "1"})
    env = seen["env"]
    assert env["RUST_BACKTRACE"] == "1"
    assert env["RUSTUP_TOOLCHAIN"] == "nightly"  # cluster env still present


def test_run_probe_keeps_a_preexisting_examples_dir_and_reports_failure(tmp_path: Path) -> None:
    (tmp_path / "examples").mkdir()
    (tmp_path / "examples" / "real.rs").write_text("fn main() {}", encoding="utf-8")

    def failing(cmd: list[str], cwd: Path, env: dict[str, str], timeout: int):
        return 101, "", "error[E0425]: cannot find"

    res = run_probe(tmp_path, None, "fn main() { nope }", "p/1", run=failing)
    assert not res.ok and "E0425" in res.stderr
    assert sorted(p.name for p in (tmp_path / "examples").iterdir()) == ["real.rs"]


def test_materialized_probe_removes_file_even_when_the_block_raises(tmp_path: Path) -> None:
    try:
        with materialized_probe(tmp_path, "fn main() {}", "x") as target:
            assert (tmp_path / "examples" / f"{target}.rs").exists()
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert not (tmp_path / "examples").exists()
    assert safe_probe_name("Error::msg/1") == "Error__msg_1"
