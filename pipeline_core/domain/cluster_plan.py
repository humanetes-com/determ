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

from pipeline_core import log as _log
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


def _custom_cfg_is_reachable(
    pred: str, bs_set_default: frozenset[str],
    bs_set_nightly: frozenset[str], bs_declared: frozenset[str],
) -> bool:
    """Does a custom `--cfg` predicate correspond to a REACHABLE build (#23)?

    The crate's `build.rs` is the authority on which custom cfgs it sets, captured
    per available toolchain:
      * set under the DEFAULT build → intrinsic to stable, not a separate toggle → DROP;
      * set only under NIGHTLY → a real toolchain-switched cluster → KEEP;
      * DECLARED (`rustc-check-cfg`) but set on no available toolchain → build-script
        controlled and off (e.g. anyhow's old-rustc / probe-only / CI-only cfgs) → DROP;
      * not mentioned by `build.rs` at all → a user-supplied `--cfg` knob → KEEP.
    When no build-script facts exist (no `build.rs`), every custom pred is KEPT —
    preserving the pre-#23 behaviour exactly."""
    if pred in bs_set_default:
        return False
    if pred in bs_set_nightly:
        return True
    return pred not in bs_declared    # declared-but-unset ⇒ unreachable; unmentioned ⇒ user knob


def _classify(counts: Mapping[str, int], cargo_features: frozenset[str],
              default_features: frozenset[str], *,
              bs_set_default: frozenset[str] = frozenset(),
              bs_set_nightly: frozenset[str] = frozenset(),
              bs_declared: frozenset[str] = frozenset()) -> list[tuple[str, str]]:
    """Predicates that earn a cluster, as (predicate, kind) with kind in
    {"feature", "custom"}; build-target/default/noise/unreachable predicates dropped."""
    interesting: list[tuple[str, str]] = []
    for pred, count in counts.items():
        if pred in STABLE_KNOWN:
            continue
        if pred in cargo_features:
            if pred in default_features or count < NOISE_THRESHOLD:
                continue                      # always-on default, or below noise floor
            interesting.append((pred, "feature"))
        elif _custom_cfg_is_reachable(pred, bs_set_default, bs_set_nightly, bs_declared):
            interesting.append((pred, "custom"))   # reachable custom --cfg toggle
    return interesting


def plan_clusters(
    counts: Mapping[str, int],
    mod_gates: Mapping[str, set[str]],
    cargo_features: Iterable[str],
    default_features: Iterable[str],
    feature_gate_cfgs: Iterable[str],
    *,
    bs_set_default: Iterable[str] = (),
    bs_set_nightly: Iterable[str] = (),
    bs_declared: Iterable[str] = (),
) -> list[BuildCluster]:
    """Build the stable cluster plus one cluster per interesting predicate.

    ``bs_*`` are the build-script cfg facts (#23): cfgs `build.rs` sets under the
    default toolchain, sets only under nightly, and merely declares — used to drop
    custom `--cfg` atoms that no reachable build actually turns on. Empty (the
    default) ⇒ pre-#23 behaviour (every custom atom is a cluster)."""
    bs_nightly = frozenset(bs_set_nightly)
    interesting = _classify(
        counts, frozenset(cargo_features), frozenset(default_features),
        bs_set_default=frozenset(bs_set_default),
        bs_set_nightly=bs_nightly,
        bs_declared=frozenset(bs_declared))
    nightly_gates = frozenset(feature_gate_cfgs) | bs_nightly

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


def prune_redundant_clusters(
    clusters: list[BuildCluster],
    default_features: set[str],
    pred_counts: Mapping[str, int],
    *,
    min_gated_items: int = 5,
) -> list[BuildCluster]:
    """Drop clusters already covered by another cluster (pure decision).

    Facts are pre-gathered by the caller (an adapter): ``default_features`` is
    Cargo.toml's ``[features].default`` set; ``pred_counts`` maps each gated
    predicate name to how many ``src/`` items reference it. Two strategies:

      (P) Default-redundancy: a cluster whose distinguishing ``active_cfg`` is a
          subset of ``default_features`` is already exercised by the baseline →
          drop it.
      (Q) Empty-cluster: a non-default cluster whose distinguishing predicates
          gate fewer than ``min_gated_items`` items is too small to justify its
          own LLM run → drop it.

    Always keep at least one cluster (the baseline). Logs each prune decision.
    """
    if not clusters:
        return clusters

    keep: list[BuildCluster] = []
    dropped: list[tuple[str, str]] = []
    # Sort clusters: stable/default first so we always retain a baseline.
    ordered = sorted(clusters, key=lambda c: (not c.is_default, c.name))
    has_baseline = False
    for c in ordered:
        distinguishing = list(c.active_cfg or [])
        # (P) default-redundancy: drop if all distinguishing cfgs are in defaults.
        if distinguishing and all(p in default_features for p in distinguishing):
            if has_baseline:
                dropped.append((c.name, f"all cfgs {distinguishing} are in default features"))
                continue
        # (Q) empty-cluster: drop if distinguishing predicates gate too little.
        if distinguishing:
            total_gated = sum(pred_counts.get(p, 0) for p in distinguishing)
            if total_gated < min_gated_items and not c.is_default:
                if has_baseline:
                    dropped.append((c.name,
                                    f"distinguishing cfgs gate only {total_gated} item(s) "
                                    f"(< {min_gated_items})"))
                    continue
        keep.append(c)
        if c.is_default or not distinguishing:
            has_baseline = True

    if not keep:
        # Pathological: nothing survived. Keep the lex-first as safety baseline.
        keep = [sorted(clusters, key=lambda c: c.name)[0]]
        _log.log(f"  [prune] all clusters were redundant; keeping baseline '{keep[0].name}' as safety.")

    if dropped:
        for name, reason in dropped:
            _log.log(f"  [prune] dropped cluster '{name}': {reason}")
    else:
        _log.log(f"  [prune] no redundant clusters; keeping all {len(keep)}.")
    return keep


def fill_order(clusters: Iterable[BuildCluster]) -> list[BuildCluster]:
    """Deterministic task2a fill order (debt #36): the default cluster first —
    it owns every cfg-agnostic item, and each later cluster seeds its stubs
    from the bodies earlier clusters already accepted — then the rest by name."""
    return sorted(clusters, key=lambda c: (not c.is_default, c.name))
