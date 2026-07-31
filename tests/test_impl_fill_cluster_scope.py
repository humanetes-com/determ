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
    cluster_inactive_stub_names,
    cluster_scoped_splice,
)
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


def test_inactive_names_are_the_difference_between_every_stub_and_the_active_ones():
    got = cluster_inactive_stub_names(_SRC, _Cluster(), _scan)
    assert got == frozenset({("wrapper.rs", "provide")})


def test_a_name_with_one_active_occurrence_is_never_convicted():
    # `error.rs::context` is gated in one impl and ungated in another. A
    # by-name refusal would block the fn nothing else can fill.
    assert ("error.rs", "context") not in cluster_inactive_stub_names(
        _SRC, _Cluster(), _scan)


def test_no_cluster_means_no_verdict():
    assert cluster_inactive_stub_names(_SRC, None, _scan) == frozenset()


def test_an_absent_extractor_enforces_nothing():
    assert cluster_inactive_stub_names(
        _SRC, _Cluster(), lambda _d, _c: None) == frozenset()


def test_splice_refuses_a_body_for_a_fn_this_cluster_does_not_compile():
    calls: list[str] = []
    logs: list[str] = []
    guarded = cluster_scoped_splice(
        _accepting_splice(calls),
        inactive=frozenset({("wrapper.rs", "provide")}),
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
        inactive=frozenset({("wrapper.rs", "provide")}),
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
        _splice, inactive=frozenset({("wrapper.rs", "provide")}),
        target_src_dir=_SRC, log=lambda _m: None)
    guarded(_SRC / "error.rs", "impl Error", "context", "x", "where T: Send",
            allow_nonstub=True, sig_text="fn context()")
    assert seen == {"where_clause": "where T: Send",
                    "allow_nonstub": True, "sig_text": "fn context()"}


def test_nothing_inactive_returns_the_splice_unwrapped():
    base = _accepting_splice([])
    assert cluster_scoped_splice(
        base, inactive=frozenset(), target_src_dir=_SRC,
        log=lambda _m: None) is base
