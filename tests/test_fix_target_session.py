"""FixTarget verdict logic + FixSession context (P3.C domain VOs)."""
from __future__ import annotations

from pathlib import Path

from pipeline_core.domain.fix_session import FixSession
from pipeline_core.domain.fix_target import FixTarget, FnTarget, ImplTarget
from pipeline_core.domain.models import FnRecord, ImplBlock


def test_fn_target_is_clean_when_off_frontier() -> None:
    t = FnTarget("src/lib.rs", "cap_limit")
    assert isinstance(t, FixTarget)
    assert t.member_names() == frozenset({"cap_limit"})
    # build fails but this fn is not in the frontier → clean
    assert t.is_clean(rc=101, frontier={("src/lib.rs", "other"): {"t"}})
    # build fails and this fn IS in the frontier → dirty
    assert not t.is_clean(rc=101, frontier={("src/lib.rs", "cap_limit"): {"t"}})
    # build passes → always clean
    assert t.is_clean(rc=0, frontier={("src/lib.rs", "cap_limit"): {"t"}})


def test_impl_target_dirty_if_any_member_in_frontier() -> None:
    block = ImplBlock(
        file="src/m.rs", container="impl Foo",
        members=(FnRecord(file="src/m.rs", fn_name="a"),
                 FnRecord(file="src/m.rs", fn_name="b")),
        related_containers=("impl Bar",),
    )
    t = ImplTarget("src/m.rs", "impl Foo", block)
    assert isinstance(t, FixTarget)
    assert t.member_names() == frozenset({"a", "b"})
    assert t.siblings() == ("impl Bar",)
    assert not t.is_clean(rc=101, frontier={("src/m.rs", "b"): {"t"}})
    assert t.is_clean(rc=101, frontier={("src/m.rs", "z"): {"t"}})
    assert t.is_clean(rc=0, frontier={("src/m.rs", "a"): {"t"}})


def test_session_writability_guards() -> None:
    s = FixSession.for_check_cmd(
        project_dir=Path("/p"), check_cmd="cargo test --all",
        derive_check_only=lambda c: c.replace("test", "check"),
        env={}, timeout=60, model="m",
        existing_test_files=frozenset({"tests/it.rs"}),
        allowed_dir_prefix="src/",
    )
    assert s.check_only_cmd == "cargo check --all"
    assert s.is_writable("src/lib.rs")
    assert not s.is_writable("tests/it.rs")        # preserved test file
    assert not s.is_writable("examples/e.rs")      # outside allowed prefix
