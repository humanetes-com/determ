"""Pins for the cluster-cfg scope of the per-impl fill's SPLICE.

`collect_impl_blocks_for_cluster` narrows a block's `stub_members` to the
members this cluster compiles, and the prompt asks for those only. That
decision never reached the splice: the model is shown the whole block, answers
a `#[cfg]`-gated sibling too, `route_fn_bodies` routes it to a real container,
and the splice fills whatever stub it finds. Live consequence on `anyhow`:
`stable` filled `src/wrapper.rs::provide` inside
`#[cfg(error_generic_member_access)]` — one log line, `FILLED 2 fn(s)`, for a
block with one cluster-active stub — and the seed then handed that never-
compiled body to `nightly`.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from pipeline_core.application.reconstruct.impl_fill import (
    cluster_live_stubs,
    cluster_scoped_splice,
    file_first_guidance_keys,
)
from pipeline_core.domain.cfg_dead_sites import CfgDeadSites
from pipeline_core.domain.reconstruction import SpliceOutcome

_SRC = Path("/x/.skeleton-filled/stable/src")

_RECORDS = [
    {"file": "wrapper.rs", "fn_name": "source", "cfg_predicates": []},
    {"file": "wrapper.rs", "fn_name": "provide",
     "cfg_predicates": ["error_generic_member_access"]},
    # Same NAME, two gates: one of them this cluster does build.
    {"file": "error.rs", "fn_name": "context", "cfg_predicates": []},
    {"file": "error.rs", "fn_name": "context",
     "cfg_predicates": ["error_generic_member_access"]},
]


class _Cluster:
    name = "stable"


def _scan(_dir: Path, cluster: Any) -> list[dict[str, Any]] | None:
    """The adapter's contract: `cluster=None` returns every record, a cluster
    returns the active subset."""
    if cluster is None:
        return list(_RECORDS)
    return [r for r in _RECORDS
            if "error_generic_member_access" not in r["cfg_predicates"]]


def _accepting_splice(calls: list[str]):
    def _splice(file_path, container, fn_name, body, where_clause=None, **kw):
        calls.append(fn_name)
        return SpliceOutcome(True, "ok", len(body))
    return _splice


def test_the_live_stubs_are_what_the_cluster_compiles():
    # The dead-site verdict lives in `cluster_membership.cluster_dead_sites`
    # (tests/test_cluster_dead_sites.py); what stays here is the reader the
    # regression gate's `stub_free` asks.
    from pipeline_core.domain.models import FnLocator
    live = cluster_live_stubs(_SRC, _Cluster(), _scan, CfgDeadSites())
    assert live.keys() == frozenset({FnLocator("wrapper.rs", "source"),
                                     FnLocator("error.rs", "context")})
    assert not live.free()


def test_no_cluster_or_no_extractor_is_unknown_not_zero():
    assert cluster_live_stubs(_SRC, None, _scan, CfgDeadSites()).keys() is None
    gone = cluster_live_stubs(_SRC, _Cluster(), lambda _d, _c: None,
                              CfgDeadSites())
    assert gone.keys() is None and not gone.free()


def test_stub_free_is_read_when_the_gate_asks_not_when_the_call_began():
    """A route call runs for hours. Its `stub_free` used to be a count taken
    as the call began, so a crash written after the last cluster-active stub
    was filled was kept until the next sweep instead of reverted at once."""
    recs = [{"file": "lib.rs", "fn_name": "only"}]
    live = cluster_live_stubs(_SRC, _Cluster(), lambda _d, _c: list(recs),
                              CfgDeadSites())
    assert not live.free()
    recs.clear()   # a unit of the same call filled the last stub
    assert live.free()


def test_splice_refuses_a_body_for_a_fn_this_cluster_does_not_compile():
    calls: list[str] = []
    logs: list[str] = []
    guarded = cluster_scoped_splice(
        _accepting_splice(calls),
        inactive=CfgDeadSites(frozenset({("wrapper.rs", "provide")})),
        target_src_dir=_SRC, log=logs.append)
    out = guarded(_SRC / "wrapper.rs", "impl StdError for BoxedError",
                  "provide", "request.provide_ref(&*self.0)")
    assert out.ok is False
    assert out.reason == "not_in_cluster_cfg"
    assert calls == []                     # never reached the real splice
    assert any("cfg-gated out of this cluster" in m for m in logs)


def test_the_sibling_in_the_same_block_still_lands():
    calls: list[str] = []
    guarded = cluster_scoped_splice(
        _accepting_splice(calls),
        inactive=CfgDeadSites(frozenset({("wrapper.rs", "provide")})),
        target_src_dir=_SRC, log=lambda _m: None)
    assert guarded(_SRC / "wrapper.rs", "impl StdError for BoxedError",
                   "source", "(&*self.0).source()").ok
    assert calls == ["source"]


def test_keyword_arguments_of_the_wrapped_splice_survive():
    # `allow_nonstub` (refills) and `sig_text` (free-fn reroute) are what the
    # two call sites pass; a wrapper that swallowed them would silently turn a
    # refill into a `target_not_stub` refusal.
    seen: dict[str, Any] = {}

    def _splice(file_path, container, fn_name, body, where_clause=None, **kw):
        seen.update(kw)
        seen["where_clause"] = where_clause
        return SpliceOutcome(True, "ok", 0)

    guarded = cluster_scoped_splice(
        _splice, inactive=CfgDeadSites(frozenset({("wrapper.rs", "provide")})),
        target_src_dir=_SRC, log=lambda _m: None)
    guarded(_SRC / "error.rs", "impl Error", "context", "x", "where T: Send",
            allow_nonstub=True, sig_text="fn context()")
    assert seen == {"where_clause": "where T: Send",
                    "allow_nonstub": True, "sig_text": "fn context()"}


def test_nothing_inactive_returns_the_splice_unwrapped():
    base = _accepting_splice([])
    assert cluster_scoped_splice(
        base, inactive=CfgDeadSites(), target_src_dir=_SRC,
        log=lambda _m: None) is base


def test_file_first_guidance_keys_include_the_per_fn_stamps() -> None:
    """File-first looked up only `file::*impl*::container`. The live store
    (2026-08-26) held 10 per-fn records the constructor route never asked
    for. The lookup is the fill-plan unit keys the per-fn route already
    stamps, plus the *impl* record so the 32 block records still honour."""
    from pipeline_core.domain.models import FnRecord, ImplBlock
    from pipeline_core.prompts.fix_prompts import BlockFillPlan
    rec = FnRecord(file="src/error.rs", fn_name="construct_from_std")
    block = ImplBlock(file="src/error.rs", container="impl Error",
                      refill_members=(rec,))
    keys = file_first_guidance_keys("src/error.rs", [block])
    assert ("src/error.rs", "*impl*", "impl Error") in keys
    assert ("src/error.rs", "construct_from_std", "impl Error") in keys
    extra = [BlockFillPlan("", (), ("object_drop",), installable=("object_drop",))]
    with_free = file_first_guidance_keys(
        "src/error.rs", [block], extra_plans=extra)
    assert ("src/error.rs", "object_drop", "") in with_free
    # NEGATIVE CONTROL: *impl* alone is the pre-fix lookup.
    assert keys != (("src/error.rs", "*impl*", "impl Error"),)
