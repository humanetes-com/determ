"""What one cluster contributes to a cargo env — the one rule (pure domain).

A cluster is a build world: the cfg atoms it sets hold for everything cargo
builds in it. cargo does not make that so by itself. It hands `RUSTFLAGS` to
rustc and `RUSTDOCFLAGS` to rustdoc, and a doc test is compiled by rustdoc, so
a cluster that delivers its cfg as `RUSTFLAGS=--cfg N` compiles the `#[cfg(N)]`
items and runs the doc tests of the `not(N)` world. A doc example its crate
gates on `N` then compiles as whatever the `not(N)` arm leaves of it.

Measured on the merged anyhow tree of run `292191-d60a5308` (2026-10-02),
cluster `nightly`, env `RUSTFLAGS=--cfg error_generic_member_access`: the one
doc example asserting that a boxed error still provides its backtrace opens
`#[cfg_attr(not(error_generic_member_access), doc = "# _ = stringify! {")]`,
so its whole body was a `stringify!` argument and it passed; with the same
cfg also in `RUSTDOCFLAGS` it failed on that assertion. The reconstruction
had lost the behaviour, the run's suite read 401 passed, and the reference
measurement of the original, taken under the same env, was the same empty
shell.

So the cluster's contribution is computed here and nowhere else:
:func:`world_env_extra` is `env_extra` with every `--cfg` of its RUSTFLAGS
also delivered to rustdoc, and :func:`overlay_cluster_env` is the only way a
cluster's env reaches a cargo env. A cfg set for rustc and not for rustdoc is
not a state a caller can build. `env_extra` itself is frozen in the baked
cluster plan, which is why the rule lives at the overlay and not where the
plan is written.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# The variable that names which cluster an env was built for. cargo ignores
# it; the compile check reads it (`cargo_runner.WorldChecks`) to know which
# OTHER worlds the tree it just built must also build in. Carried by the env
# because the env is what every check call already receives — a cluster
# argument threaded to each of them is a call a new site can forget.
WORLD_VAR = "DETERM_CLUSTER_WORLD"

# The variable that says a test run judges the CRATE, not one world: the run
# is repeated under every other baked world and the captures are joined
# (`cargo_runner.PortBoundCargoRunner.run`). Set by the one-tree loop's
# identity (`cluster_plan.one_tree_cluster`) through its env, so every sweep,
# gate and keep site the loop runs inherits it, and augmentation, which judges
# one world by design, never carries it.
ALL_WORLDS_VAR = "DETERM_ALL_WORLDS"

# The two variables cargo hands to a compiler front end. A cluster APPENDS to
# them (the base env's `-A warnings` survives); every other key is set.
_FLAG_VARS = ("RUSTFLAGS", "RUSTDOCFLAGS")


def cfg_flags(flags: str) -> list[str]:
    """The `--cfg` values one RUSTFLAGS/RUSTDOCFLAGS string sets, in order
    (`--cfg N` and `--cfg=N`; a `key="value"` cfg keeps its whole spelling).
    Split on whitespace, as cargo splits these variables."""
    tokens = flags.split()
    out: list[str] = []
    for i, tok in enumerate(tokens):
        if tok == "--cfg" and i + 1 < len(tokens):
            out.append(tokens[i + 1])
        elif tok.startswith("--cfg="):
            out.append(tok[len("--cfg="):])
    return out


def world_env_extra(env_extra: Mapping[str, str] | None) -> dict[str, str]:
    """A cluster's `env_extra` as its build world means it: every cfg it sets
    for rustc is set for rustdoc too. Idempotent; a cfg the cluster already
    hands rustdoc is not repeated."""
    out = dict(env_extra or {})
    have = set(cfg_flags(out.get("RUSTDOCFLAGS", "")))
    missing: list[str] = []
    for name in cfg_flags(out.get("RUSTFLAGS", "")):
        if name not in have:
            have.add(name)
            missing.append(name)
    if missing:
        out["RUSTDOCFLAGS"] = " ".join(
            [out.get("RUSTDOCFLAGS", "").strip()]
            + [f"--cfg {name}" for name in missing]).strip()
    return out


def overlay_cluster_env(env: dict[str, str], cluster: Any | None) -> None:
    """Layer `cluster`'s contribution onto a cargo env, in place.

    RUSTFLAGS and RUSTDOCFLAGS are appended to what `env` already carries;
    every other key is set; `RUSTUP_TOOLCHAIN=nightly` is set when the cluster
    requires the nightly toolchain. `None` is the default build: no change.

    Every site that builds an env for a named cluster calls this. The sites
    that used `dict.update(env_extra)` instead replaced RUSTFLAGS (dropping
    `-A warnings`) and never set the toolchain — run `216855-03b2a21b`
    (2026-09-02) — and would each have had to remember rustdoc as well."""
    if cluster is None:
        return
    for key, value in world_env_extra(getattr(cluster, "env_extra", None)).items():
        if key in _FLAG_VARS:
            env[key] = f"{env.get(key, '')} {value}".strip()
        else:
            env[key] = value
    if getattr(cluster, "requires_nightly", False):
        env["RUSTUP_TOOLCHAIN"] = "nightly"
    name = str(getattr(cluster, "name", "") or "")
    if name:
        env[WORLD_VAR] = name


def judges_every_world(env: Mapping[str, str] | None) -> bool:
    """True when a test run under `env` is the crate's reading in every world."""
    return str((env or {}).get(ALL_WORLDS_VAR, "")) == "1"


def world_of_env(env: Mapping[str, str] | None) -> str:
    """The cluster a cargo env was built for, or "" for the default build."""
    return str((env or {}).get(WORLD_VAR, ""))


def world_key(cluster: Any | None) -> tuple[tuple[tuple[str, str], ...], str | None]:
    """What makes this cluster's cargo run differ from another's: its env
    contribution as the overlay delivers it, and its toolchain. Two clusters
    with equal keys run the same build, so a result under one is not a second
    reading. A cluster whose cfg is a cargo feature has an empty `env_extra`
    and the default toolchain: its key is the default cluster's."""
    return (tuple(sorted(world_env_extra(getattr(cluster, "env_extra", None)).items())),
            getattr(cluster, "toolchain", None))
