"""Behaviour-suite storage — persist harvested/synthesized behaviours as a baked
artifact under ``.skeleton/``.

Written ONCE in the pre-strip window (Hard Rule 11), read by every post-strip
phase that needs behaviours: the fill prompt (few-shot RIGHT usage) and the t3
oracle. One file per cfg-cluster mirrors the skeleton's per-cluster layout.
"""
from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from pathlib import Path

from pipeline_core.domain.behaviour import SUITE_FILE_PREFIX, BehaviourSpec, BehaviourSuite
from pipeline_core.domain.behaviour_select import resolve_identity_subjects
from pipeline_core.log import emit, log

_SKELETON_DIR = ".skeleton"


def _suite_path(project_dir: Path, cfg_cluster: str) -> Path:
    return Path(project_dir) / _SKELETON_DIR / f"{SUITE_FILE_PREFIX}{cfg_cluster}.json"


def save_behaviour_suite(project_dir: Path, suite: BehaviourSuite) -> Path:
    """Write `suite` to ``.skeleton/_behaviours_<cluster>.json``; return the path."""
    p = _suite_path(project_dir, suite.cfg_cluster)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(suite.to_dict(), indent=2, sort_keys=True),
                 encoding="utf-8")
    return p


def write_feature_files(project_dir: Path, cfg_cluster: str, files: dict[str, str]) -> Path:
    """Write the book's Gherkin view to ``.skeleton/_features_<cluster>/``,
    replacing whatever a previous pass wrote there (the view is generated;
    a stale file would describe a book that no longer exists). Returns the
    directory."""
    d = Path(project_dir) / _SKELETON_DIR / f"_features_{cfg_cluster}"
    d.mkdir(parents=True, exist_ok=True)
    for old in d.iterdir():
        if old.is_file() and old.name not in files:
            old.unlink()
    for name, text in files.items():
        (d / name).write_text(text, encoding="utf-8")
    return d


def load_behaviour_suite(project_dir: Path, cfg_cluster: str) -> BehaviourSuite:
    """Read a cluster's baked behaviour suite. Absent → empty suite, silently;
    present-but-unusable → empty suite, LOUDLY.

    Both cases return an empty suite, because behaviours are an ADDITIVE oracle
    and their absence must never break a run. They are not the same fact, and
    until this function separated them a corrupt
    ``.skeleton/_behaviours_<cluster>.json`` was indistinguishable from a file
    that had never been written: the whole behaviour channel simply read as "not
    harvested yet", t0d would re-harvest over it, t1.5 would author reporting
    ``0 behaviour(s) in the authoring prompt``, and nothing anywhere said the
    file on disk could not be read. A missing file is the legitimate first-run
    case and stays quiet; anything else — unreadable bytes, invalid JSON, or
    JSON of the wrong shape — logs and emits before failing open.
    """
    p = _suite_path(project_dir, cfg_cluster)
    try:
        raw = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return BehaviourSuite(cfg_cluster=cfg_cluster)
    except OSError as exc:
        return _unreadable(p, cfg_cluster, exc)
    try:
        suite = BehaviourSuite.from_dict(json.loads(raw))
    except Exception as exc:  # invalid JSON, or JSON that is not a suite
        return _unreadable(p, cfg_cluster, exc)
    if suite.is_legacy:
        # Read as it was written, so a tree baked before the format change
        # keeps working; never converted. The next strip bake re-harvests it
        # in the current shape, keeps its probe rows (`merge_suites`) and
        # builds the book (`BakeArtifactsDeps.build_book`). `redo.sh --full`
        # resets to the augmented tag and takes that same path, so naming it
        # promised a different outcome than it delivers.
        log(f"  [behaviour] ‼ {p} is behaviour-suite format {suite.format_version} "
            f"(every test example carries its whole test item as setup, no "
            f"rules) — the next strip bake (a restart from the augmented "
            f"tag re-runs it) re-harvests it in the current format, keeps "
            f"its probe rows and builds the behaviour book")
        emit("behaviour.suite.legacy",
             data={"path": str(p), "cfg_cluster": cfg_cluster,
                   "format": suite.format_version})
    return suite


def _unreadable(path: Path, cfg_cluster: str, exc: BaseException) -> BehaviourSuite:
    """Report a behaviour-suite file that exists and cannot be used, then fail open."""
    detail = f"{type(exc).__name__}: {exc}"
    log(f"  [behaviour] ‼ unreadable behaviour suite {path}: {detail} — "
        f"continuing with 0 behaviour(s) for cfg-cluster {cfg_cluster}")
    emit("behaviour.suite.unreadable",
         data={"path": str(path), "cfg_cluster": cfg_cluster, "error": detail})
    return BehaviourSuite(cfg_cluster=cfg_cluster)


def load_fill_specs(
    project_dir: Path, cfg_cluster: str,
    enumerate_fns: Callable[[Path], Sequence[object] | None],
) -> tuple[BehaviourSpec, ...]:
    """A cluster's baked examples as the fills read them: each identity
    example carries the crate type it is asked through, resolved against
    the records of the cluster's skeleton (`resolve_identity_subjects`).
    The skeleton keeps every impl header and signature after strip, so the
    owner of `downcast` is readable without any original body. No skeleton
    records: the examples as stored."""
    specs = load_behaviour_suite(project_dir, cfg_cluster).specs
    if not specs:
        return specs
    try:
        records = enumerate_fns(Path(project_dir) / _SKELETON_DIR / cfg_cluster) or ()
    except (OSError, ValueError) as exc:
        log(f"  [behaviours] {cfg_cluster}: skeleton records unreadable "
            f"({exc}); identity subjects stay unresolved.")
        records = ()
    return resolve_identity_subjects(specs, records) if records else specs


def fill_suite_for_prompt(
    project_dir: Path, cfg_cluster: str,
    enumerate_fns: Callable[[Path], Sequence[object] | None],
) -> BehaviourSuite:
    """The suite a prompt's identity instruction is built from.

    `load_fill_specs` is the one resolver. The fix loop unions one of
    these per cluster. A raw `load_behaviour_suite` leaves every subject
    empty, and the text harvest on the recorded stable suite is `Box`,
    which admits no constructor of the type the examples are asked through.
    """
    return BehaviourSuite(
        cfg_cluster=cfg_cluster,
        specs=load_fill_specs(project_dir, cfg_cluster, enumerate_fns))
