"""P35 fill-wide premise refusal, P36 bound-only input drop, P37 owner-less
abort.

The recorded line shapes are run `497151-a6fa205b`'s (2026-09-22, cluster
`stable`): fill 1's gate line, fill 3's worker error on `src/ensure.rs::new`,
and iter 4's coverage line with 28 binaries at rc=-6 printed as hung. Each
detector has its negative control: the post-fix line of the same event, and
for P37 a real hang (rc=-1) that IS a hang.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path


def _census():
    path = Path(__file__).resolve().parents[1] / "scripts" / "pattern_census.py"
    spec = importlib.util.spec_from_file_location("pattern_census_p35", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


census = _census()


def _run(*lines: str) -> object:
    run = census.Run("r", "g", "c", "ts", "start:1")
    run.lines = [(f"log:{i}", ln) for i, ln in enumerate(lines, 1)]
    return run


_P35_PRE = ("  [phase-c/schedule] fill 1: premise moved after a keep — 13 test(s) "
            "no longer fail where the plan read them (a, b, c, d (+9 more)); "
            "no further unit is claimed this fill, units in flight complete")
_P35_POST = ("  [phase-c/schedule] fill 1: premise moved after a keep — 13 test(s) "
             "no longer fail where the plan read them (a, b, c, d (+9 more)); "
             "13 moved in this fill. Claims planned on a moved test are "
             "refused, the rest of the plan continues, units in flight complete")
_P36 = ("  [phase-c/per-fn] worker error on src/ensure.rs::new: input too long "
        "(real_tokens=33950)")
_P37_ABORT = ("  [coverage] 2 binary(ies) did not complete (killed by a signal or "
              "no profile written): error_gen_1 (rc=-6), test_boxed (rc=-6); "
              "hung inside error_gen_1::t (killed after 120s)")
_P37_HANG = ("  [coverage] 1 binary(ies) did not complete (killed by a signal or "
             "no profile written): lib_gen_1 (rc=-1); hung inside "
             "lib_gen_1::t_hang (killed after 120s)")


def test_p35_counts_the_fill_wide_refusal_and_not_the_scoped_gate() -> None:
    hits = census.detect_p35(_run(_P35_PRE, _P35_PRE.replace("fill 1", "fill 2")), [])
    assert len(hits) == 1 and "2 fill(s)" in hits[0]["evidence"]
    assert census.detect_p35(_run(_P35_POST), []) == []


def test_p36_keys_the_dropped_unit() -> None:
    hits = census.detect_p36(_run(_P36), [])
    assert [h["key"] for h in hits] == [["src/ensure.rs::new"]]
    assert census.detect_p36(_run("  [phase-c/per-fn] worker error on x: boom"), []) == []


def test_p37_counts_a_signal_death_printed_as_a_hang_and_not_a_real_hang() -> None:
    assert len(census.detect_p37(_run(_P37_ABORT), [])) == 1
    assert census.detect_p37(_run(_P37_HANG), []) == []


def test_every_new_class_is_registered_with_a_name() -> None:
    for cls in ("P35", "P36", "P37"):
        assert cls in census.DETECTORS and cls in census.CLASS_NAMES
        assert cls in census.REACTIONS
