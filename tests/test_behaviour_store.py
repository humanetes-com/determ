"""`load_behaviour_suite` must not read a CORRUPT suite file as an ABSENT one.

Both return an empty suite (the loader fails open on purpose — behaviours are an
additive oracle), so the only thing that distinguishes them for a human reading a
run is the log line and the event. Before the split, a corrupt
`.skeleton/_behaviours_<cluster>.json` was silent: the channel looked like it had
simply never been harvested. These tests pin the difference in both directions —
missing is quiet, unusable is loud — plus the happy path as the negative control.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import pipeline_core.adapters.behaviour_store as bs
from pipeline_core.domain.behaviour import (
    BehaviourProvenance,
    BehaviourSpec,
    BehaviourSuite,
)


@pytest.fixture
def sink(monkeypatch):
    logs: list[str] = []
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(bs, "log", logs.append)
    monkeypatch.setattr(bs, "emit", lambda event, **f: events.append((event, f)))
    return logs, events


def _write(project_dir: Path, cluster: str, text: str) -> Path:
    p = bs._suite_path(project_dir, cluster)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def test_missing_file_is_an_empty_suite_and_says_nothing(tmp_path, sink):
    logs, events = sink
    suite = bs.load_behaviour_suite(tmp_path, "stable")
    assert suite == BehaviourSuite(cfg_cluster="stable")
    assert logs == [] and events == []


def test_invalid_json_is_empty_but_loud(tmp_path, sink):
    logs, events = sink
    p = _write(tmp_path, "stable", "{not json at all")
    suite = bs.load_behaviour_suite(tmp_path, "stable")
    assert suite == BehaviourSuite(cfg_cluster="stable")
    assert len(logs) == 1 and str(p) in logs[0]
    assert [e for e, _ in events] == ["behaviour.suite.unreadable"]
    data = events[0][1]["data"]
    assert data["path"] == str(p) and data["cfg_cluster"] == "stable"
    assert "JSONDecodeError" in data["error"]


def test_valid_json_of_the_wrong_shape_is_empty_but_loud(tmp_path, sink):
    logs, events = sink
    _write(tmp_path, "nightly", json.dumps({"specs": "notalist"}))
    suite = bs.load_behaviour_suite(tmp_path, "nightly")
    assert suite == BehaviourSuite(cfg_cluster="nightly")
    assert len(logs) == 1
    assert [e for e, _ in events] == ["behaviour.suite.unreadable"]
    assert events[0][1]["data"]["cfg_cluster"] == "nightly"


def test_json_that_is_not_an_object_is_empty_but_loud(tmp_path, sink):
    logs, events = sink
    _write(tmp_path, "stable", json.dumps(["a", "list"]))
    assert bs.load_behaviour_suite(tmp_path, "stable") == BehaviourSuite(cfg_cluster="stable")
    assert len(logs) == 1
    assert [e for e, _ in events] == ["behaviour.suite.unreadable"]


def test_wellformed_suite_round_trips_and_stays_quiet(tmp_path, sink):
    """Negative control: the happy path is untouched by the corrupt-file split."""
    logs, events = sink
    suite = BehaviourSuite(
        cfg_cluster="stable",
        specs=(
            BehaviourSpec(
                behaviour_id="b1",
                setup="let e = anyhow!(\"x\");",
                when="e.to_string()",
                source_ref="tests/test_context.rs:12",
                provenance=BehaviourProvenance.HARVESTED,
            ),
        ),
    )
    path = bs.save_behaviour_suite(tmp_path, suite)
    assert path.exists()
    assert bs.load_behaviour_suite(tmp_path, "stable") == suite
    assert logs == [] and events == []


def test_the_view_directory_holds_only_the_files_of_the_last_write(tmp_path):
    """The view is generated: a file a previous pass wrote and this one does
    not (a feature the book no longer has, the givens index of a book with no
    test-file item) would describe a book that no longer exists."""
    d = bs.write_feature_files(tmp_path, "stable", {"a.feature": "A", "_givens.md": "G"})
    assert sorted(p.name for p in d.iterdir()) == ["_givens.md", "a.feature"]
    bs.write_feature_files(tmp_path, "stable", {"b.feature": "B"})
    assert [p.name for p in d.iterdir()] == ["b.feature"]


def test_the_fill_loader_resolves_identity_subjects_from_the_skeleton(
        tmp_path: Path) -> None:
    """Both fill loaders read the cluster's skeleton records once per load
    and hand the fills examples whose identity names the crate type. No
    records (or unreadable ones): the examples as stored."""
    from pipeline_core.adapters.behaviour_store import (
        load_fill_specs,
        save_behaviour_suite,
    )
    from pipeline_core.domain.behaviour import (
        BehaviourClass,
        BehaviourSpec,
        BehaviourSubKind,
        BehaviourSuite,
        ObservableOutcome,
    )
    spec = BehaviourSpec(
        "a", "", "c.peek::<u8>()", ObservableOutcome.type_identity("u8", True),
        subkind=BehaviourSubKind.OUTCOME, klass=BehaviourClass.IDENTITY,
        cfg_cluster="stable")
    save_behaviour_suite(tmp_path, BehaviourSuite("stable", (spec,)))
    seen: list[Path] = []

    def _records(root: Path) -> list[dict[str, str]]:
        seen.append(root)
        return [{"fn_name": "peek", "container": "impl Counter"}]

    (got,) = load_fill_specs(tmp_path, "stable", _records)
    assert got.subject == "Counter"
    assert seen == [tmp_path / ".skeleton" / "stable"]

    def _broken(_root: Path) -> list[dict[str, str]]:
        raise OSError("no extractor")
    (plain,) = load_fill_specs(tmp_path, "stable", _broken)
    assert plain.subject == ""


# The bytes t0d wrote on 2026-08-27, and the stable skeleton's records
# (`enumerate_fns`, 2026-10-18). The same pair the identity-subject pin uses.
_RECORDED_STABLE = (Path(__file__).resolve().parent / "fixtures" / "behaviours"
                    / "behaviours_stable_2026-08-27.json")
_SKELETON_RECORDS = (Path(__file__).resolve().parent / "fixtures" / "behaviours"
                     / "stable_skeleton_records_2026-10-18.json")
_FIX_LOOP_CONSTRUCTORS = frozenset({
    "construct", "construct_from_adhoc", "construct_from_boxed",
    "construct_from_context", "construct_from_display", "construct_from_std",
    "context", "from_boxed", "msg", "new",
})


def test_the_fix_loop_union_resolves_each_cluster_before_it_unions(
        tmp_path: Path) -> None:
    """The fix loop walks `.skeleton` suite files and unions what its loader
    returns. That loader is `fill_suite_for_prompt`: on the recorded stable
    suite and the stable skeleton records the union's subject is `Error`
    and it admits the `impl Error` constructors. Negative control: the same
    walk over `load_behaviour_suite` has subject `Box` and admits none."""
    from pipeline_core.adapters.behaviour_store import (
        fill_suite_for_prompt,
        load_behaviour_suite,
    )
    from pipeline_core.domain.behaviour import (
        suite_cluster_of,
        union_cluster_suites,
    )
    from pipeline_core.domain.behaviour_select import identity_subject_from_specs
    from pipeline_core.domain.language_driver import language_driver

    # The recorded bytes, not a save round-trip: rewriting the suite drops
    # the setup text the unresolved harvest reads.
    sk = tmp_path / ".skeleton"
    sk.mkdir()
    (sk / "_behaviours_stable.json").write_bytes(
        _RECORDED_STABLE.read_bytes())
    records = json.loads(_SKELETON_RECORDS.read_text(encoding="utf-8"))

    def _records(root: Path) -> list:
        return records if root.name == "stable" else []

    def _union(load):
        sk = tmp_path / ".skeleton"
        return union_cluster_suites([
            load(tmp_path, name)
            for name in sorted(filter(None, (
                suite_cluster_of(path.name) for path in sk.iterdir())))])

    drv = language_driver()

    def _admitted(specs) -> set[str]:
        subject = identity_subject_from_specs(specs)
        return {r["fn_name"] for r in records
                if r["container"] == "impl Error"
                and drv.identity_must_admits(
                    r["fn_name"], r["container"], r["sig_text"] or "", subject)}

    resolved = _union(
        lambda pd, name: fill_suite_for_prompt(pd, name, _records))
    assert identity_subject_from_specs(resolved) == "Error"
    assert _admitted(resolved) == _FIX_LOOP_CONSTRUCTORS
    raw = _union(load_behaviour_suite)
    assert identity_subject_from_specs(raw) == "Box"
    assert _admitted(raw) == set()
