"""Compiler-cluster planning — pure derivation of `BuildCluster`s from cfg facts
(CODE_ARCHITECTURE.md §4 P5, Hard Rule 8).

A *cluster* is one set of source-compilation restrictions (which `#[cfg(...)]`
predicates are active). This module turns the static cfg facts extracted from the
source — `#[cfg]` occurrence counts and gated `mod` declarations — into the list
of clusters the pipeline iterates. It is the explicit home of rules that used to
be inlined as regex bookkeeping in ``pipeline.task0_cfg_scan``:

  * which predicates are build-target facets that never warrant a cluster
    (:data:`STABLE_KNOWN`);
  * a declared cargo feature needs ≥ :data:`NOISE_THRESHOLD` gated occurrences to
    earn its own cluster (so a feature-heavy crate doesn't explode), while a
    custom ``--cfg`` toggle always does (else its gated code lands in no cluster);
  * a cluster whose active cfg gates a nightly ``#![feature(...)]`` needs nightly.

Pure domain: stdlib only, no I/O. The facts arrive already extracted (by the syn
extractor, via :class:`pipeline_core.ports.cluster_facts.ClusterFactReader`); the
classification and `BuildCluster` construction live here.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from pipeline_core.domain.cfg import Leaf, parse_cfg_pred
from pipeline_core.domain.models import BuildCluster, CfgOccurrence, ModRecord

# A declared cargo feature must gate at least this many `#[cfg]` occurrences to
# warrant its own cluster (noise filter; custom `--cfg` toggles are exempt).
NOISE_THRESHOLD = 5

# Crate roots the gated-`mod` scan reads (a `#[cfg(X)] mod m;` here means `m.rs`
# is only compiled when X is active).
_CRATE_ROOTS = ("lib.rs", "main.rs")

# Predicates evaluated from the build TARGET / toolchain — never their own cluster.
STABLE_KNOWN: frozenset[str] = frozenset({
    "test", "doc", "debug_assertions", "proc_macro",
    "unix", "windows",
    "target_os", "target_arch", "target_env", "target_family",
    "target_pointer_width", "target_endian", "target_vendor",
    "target_has_atomic", "target_thread_local",
    "panic",
})

# Custom cfg → short, stable cluster name.
_KNOWN_CLUSTER_NAMES = {
    "error_generic_member_access": "nightly",
    "backtrace_in_std": "nightly",
    "provide_any": "nightly",
}

_STABLE_PROMPT = (
    "- Do NOT use `error_generic_member_access`, `.provide()`, `Request`, or import `crate::nightly`\n"
    "  — these are nightly-only features unavailable on the stable test toolchain. Skip any source\n"
    "  lines inside `#[cfg(error_generic_member_access)]` blocks — they are untestable on stable.\n"
    "- Do NOT use `FormattingOptions` — it is nightly-only. Skip source lines that use it."
)


def cluster_name_for_custom(pred: str) -> str:
    """Map a custom `--cfg` predicate to a short cluster name."""
    if pred in _KNOWN_CLUSTER_NAMES:
        return _KNOWN_CLUSTER_NAMES[pred]
    name = pred.replace("_feature", "").replace("_enabled", "").replace("_support", "")
    return name[:32]


def predicate_counts(occurrences: Iterable[CfgOccurrence]) -> dict[str, int]:
    """How many `#[cfg]` occurrences mention each candidate predicate. Keyed by
    the atom's cluster key — feature name for `feature = "X"`, bare ident for
    `nightly`; build-target `key = "v"` atoms contribute nothing. Insertion order
    is first-seen (deterministic cluster ordering)."""
    counts: dict[str, int] = {}
    for occ in occurrences:
        for atom in occ.atoms:
            key = atom.cluster_key
            if key:
                counts[key] = counts.get(key, 0) + 1
    return counts


def mod_gate_map(mod_records: Iterable[ModRecord]) -> dict[str, set[str]]:
    """Predicate → source files gated entirely behind it via a crate-root
    `#[cfg(PRED)] mod m;`. Only simple single-leaf gates count (matching the
    legacy scan): `any/all/not` and multi-cfg decls are left to per-file cfg
    evaluation downstream."""
    out: dict[str, set[str]] = {}
    for rec in mod_records:
        if rec.declared_in not in _CRATE_ROOTS or len(rec.cfg_predicates) != 1:
            continue
        tree = parse_cfg_pred(rec.cfg_predicates[0])
        if not isinstance(tree, Leaf):
            continue
        key = tree.atom.cluster_key
        if key:
            out.setdefault(key, set()).add(f"src/{rec.name}.rs")
    return out


def _classify(counts: Mapping[str, int], cargo_features: frozenset[str],
              default_features: frozenset[str]) -> list[tuple[str, str]]:
    """Predicates that earn a cluster, as (predicate, kind) with kind in
    {"feature", "custom"}; build-target/default/noise predicates are dropped."""
    interesting: list[tuple[str, str]] = []
    for pred, count in counts.items():
        if pred in STABLE_KNOWN:
            continue
        if pred in cargo_features:
            if pred in default_features or count < NOISE_THRESHOLD:
                continue                      # always-on default, or below noise floor
            interesting.append((pred, "feature"))
        else:
            interesting.append((pred, "custom"))   # custom --cfg toggle: always a cluster
    return interesting


def plan_clusters(
    counts: Mapping[str, int],
    mod_gates: Mapping[str, set[str]],
    cargo_features: Iterable[str],
    default_features: Iterable[str],
    feature_gate_cfgs: Iterable[str],
) -> list[BuildCluster]:
    """Build the stable cluster plus one cluster per interesting predicate."""
    interesting = _classify(counts, frozenset(cargo_features), frozenset(default_features))
    nightly_gates = frozenset(feature_gate_cfgs)

    def gated_for(excluded: list[str]) -> set[str]:
        out: set[str] = set()
        for p in excluded:
            out |= mod_gates.get(p, set())
        return out

    all_preds = [p for p, _ in interesting]
    clusters: list[BuildCluster] = [BuildCluster(
        name="stable",
        env_extra={},
        tarpaulin_exclude_files=["*/nightly.rs"],
        excluded_cfg=all_preds,
        active_cfg=[],
        prompt_nightly_rule=_STABLE_PROMPT,
        is_default=True,
        gated_files=gated_for(all_preds),
    )]

    for pred, kind in interesting:
        excl = [p for p in all_preds if p != pred]
        if kind == "feature":
            cluster = BuildCluster(
                name=f"feature/{pred}",
                env_extra={},
                tarpaulin_exclude_files=[],
                excluded_cfg=excl,
                active_cfg=[pred],
                prompt_nightly_rule=(
                    f"- COMPILER CLUSTER: feature/{pred}\n"
                    f"  Cargo feature `{pred}` IS enabled. Code inside `#[cfg(feature=\"{pred}\")]` IS active.\n"
                    f"  Focus your tests on lines inside these cfg blocks."
                ),
                is_default=False,
                gated_files=gated_for(excl),
            )
        else:
            name = cluster_name_for_custom(pred)
            cluster = BuildCluster(
                name=name,
                env_extra={"RUSTFLAGS": f"--cfg {pred}"},
                tarpaulin_exclude_files=[],
                excluded_cfg=excl,
                active_cfg=[pred],
                prompt_nightly_rule=(
                    f"- COMPILER CLUSTER: {name}\n"
                    f"  `{pred}` IS active (RUSTFLAGS=--cfg {pred}). All `#[cfg({pred})]` blocks ARE compiled.\n"
                    f"  You MAY use any symbols gated behind `#[cfg({pred})]`.\n"
                    f"  Only write tests for lines inside `#[cfg({pred})]` blocks — stable lines are already covered."
                ),
                is_default=False,
                gated_files=gated_for(excl),
            )
        if set(cluster.active_cfg) & nightly_gates:
            cluster.requires_nightly = True
        clusters.append(cluster)

    return clusters
