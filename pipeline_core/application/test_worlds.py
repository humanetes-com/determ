"""Which build worlds a generated test belongs to — measured, then gated.

Augmentation writes a test while one cluster is active and must then decide
which clusters compile it. Until 2026-10-02 that was predicted: a test was
shared only when written under the default cluster, its target lines sat in
no cfg region, and its text held none of the names of any fn some cluster
compiles out. On the anyhow workspace of run `292191-d60a5308` the name
predicate gated 26 of 26 files written under `stable`: 24 because their text
holds the word `fmt` and 8 the word `source`, each flagged because ONE
same-named fn sits behind a cfg no cluster sets at all. A test written under
any other cluster was never shared. With the gate line removed, 25 of those
26 files and 18 of the 21 written under `nightly` build and pass in the other
cluster (177 of 196 tests); the later cluster had generated 75 tests for lines
the earlier one's tests already covered, because those tests were compiled out
of its coverage run.

The question has an exact answer and cargo gives it: build and run the
accepted test under each other world's env. Its worlds are the one that
generated it plus every world where it builds and each of its own tests
passes, and its gate is rendered from that set (`cfg_worlds.gate_for_worlds`).
No predicate about the text is consulted, so a new shape of test cannot be
mis-gated by one. A later world's coverage run then compiles what earlier
worlds share, and what it still finds uncovered is its residue by measurement.

Until the measurement has run the file carries its generating world's own
gate: nothing has shown it true anywhere else.

The generating world is measured like the others. A write site that had only
compiled a block (the quick retry) could otherwise hand over a test that fails
at home and passes elsewhere, and it would come back with no gate at all. So
the settle takes no caller's word for the home world: it runs it, and a test
that does not pass where it was written has no worlds and is the caller's to
drop.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pipeline_core.domain.cfg_worlds import gate_for_worlds


@dataclass(frozen=True, slots=True)
class TestWorldsDeps:
    """Collaborators, injected by the composition root."""

    __test__ = False  # a deps record, not a pytest class

    # Every build world of this run.
    clusters: Callable[[], Sequence[Any]]
    # A world's own gate: true in it, false in the others ("" when it is the
    # only world).
    world_gate: Callable[[Any], str]
    # (project_dir, module, test binary stem, block, cluster) -> the test file
    # builds under that world's env and every one of the block's tests passes.
    passes_in: Callable[[Path, str, str, str, Any], bool]
    log: Callable[[str], None]
    # What makes a world's cargo run differ from another's (its env
    # contribution and toolchain, `cluster_env.world_key`). Two clusters with
    # the same key run the same build: a pass under one says nothing about
    # the other. A cluster whose cfg is a cargo feature contributes no flags
    # of its own (`cluster_plan`, the `feature` kind), so its run IS the
    # default run. Required: a default keyed on the name would count every
    # such cluster as its own world.
    world_key: Callable[[Any], object]
    emit: Callable[..., Any] = lambda *_a, **_k: None


def gate_line(gate: str) -> str:
    """The crate-level line that gates a test file, or "" for no gate."""
    return f"#![cfg({gate})]\n" if gate else ""


def own_world_gate(cluster: Any | None, deps: TestWorldsDeps) -> str:
    """The gate a test carries before its worlds are measured."""
    return deps.world_gate(cluster) if cluster is not None else ""


def world_is_measurable(cluster: Any | None, deps: TestWorldsDeps) -> bool:
    """Whether a run under `cluster` can show a test passing in it.

    False for a non-default cluster that sets cfg atoms and contributes
    nothing to cargo (its key is the no-cluster key): its own gate is false
    in every build the pipeline runs. The default cluster contributes
    nothing either, and its gate — the negation of the others' atoms — is
    true in exactly that build."""
    if cluster is None:
        return True
    return not (bool(own_world_gate(cluster, deps))
                and not getattr(cluster, "is_default", False)
                and deps.world_key(cluster) == deps.world_key(None))


def settle_test_gate(
    project_dir: Path,
    module_name: str,
    test_file: Path,
    block: str,
    generating: Any | None,
    deps: TestWorldsDeps,
) -> str | None:
    """Measure the worlds of the test at `test_file` and rewrite its gate to
    them. Returns the gate now on the file, or None when the test does not
    pass in the world that generated it: it then has no world, the file is
    left exactly as found, and the caller drops it.

    The file must be on disk under `own_world_gate(generating)`, as the write
    sites leave it. A world whose run cannot be taken (cargo missing, a
    timeout) is a world the test was not shown to pass in — the generating
    one included. Whatever happens, the file is left either under its
    measured gate or exactly as found."""
    own = own_world_gate(generating, deps)
    if not world_is_measurable(generating, deps):
        # No run can show a test passing in this world
        # (`world_is_measurable`). Not a failure of the test: there is no
        # reading to take. The file keeps the gate it was written under.
        deps.log(f"  [{module_name}] {test_file.name}: world "
                 f"'{getattr(generating, 'name', '')}' turns on no build flag of its own, so "
                 "its gate is false in the build it runs — not measured, "
                 "gate left as written.")
        return own
    try:
        at_home = deps.passes_in(project_dir, module_name, test_file.stem,
                                 block, generating)
    except Exception as exc:
        deps.log(f"  [{module_name}] {test_file.name}: no reading in its own "
                 f"world ({exc}).")
        at_home = False
    if not at_home:
        deps.log(f"  [{module_name}] {test_file.name}: does not pass in the "
                 "world that generated it — it has no world to be gated to.")
        deps.emit("task0c.test_worlds", file=test_file.name,
                  data={"generating": str(getattr(generating, "name", "")),
                        "worlds": [], "gate": None})
        return None
    clusters = list(deps.clusters())
    if generating is None or len(clusters) < 2:
        return own
    try:
        original = test_file.read_text(encoding="utf-8")
    except OSError:
        return own
    head = gate_line(own)
    if head and not original.startswith(head):
        # Not the shape the write sites produce: leave it alone.
        return own
    body = original[len(head):]
    atoms = {str(c.name): deps.world_gate(c) for c in clusters}
    name = str(generating.name)
    worlds = {name}
    own_key = deps.world_key(generating)
    settled = False
    try:
        test_file.write_text(body, encoding="utf-8")
        for c in sorted(clusters, key=lambda x: str(x.name)):
            if str(c.name) == name:
                continue
            if deps.world_key(c) == own_key:
                deps.log(f"  [{module_name}] {test_file.name}: world "
                         f"'{c.name}' builds under the same env and toolchain "
                         f"as '{name}' — a run there is the same run, so it "
                         "is not a reading and the test is not shared there.")
                continue
            try:
                if deps.passes_in(project_dir, module_name, test_file.stem, block, c):
                    worlds.add(str(c.name))
            except Exception as exc:  # a world that gave no reading
                deps.log(f"  [{module_name}] {test_file.name}: no reading in "
                         f"world '{c.name}' ({exc}) — not shared there.")
        gate = gate_for_worlds(worlds, atoms)
        test_file.write_text(gate_line(gate) + body, encoding="utf-8")
        settled = True
    finally:
        if not settled:
            test_file.write_text(original, encoding="utf-8")
    shared = sorted(worlds - {name})
    deps.log(f"  [{module_name}] {test_file.name}: written under '{name}', "
             + (f"also builds and passes in {', '.join(shared)}"
                if shared else "passes in no other world")
             + (" — ungated." if not gate else f" — gated `{gate}`."))
    deps.emit("task0c.test_worlds", file=test_file.name,
              data={"generating": name, "worlds": sorted(worlds), "gate": gate})
    return gate
