"""Emit the crate's doc examples as oracle tests, verified on the original
(use case; runs once per module, before strip).

`domain.doc_example_test` says why and how one example is rendered. This
module is the bake step: it reads the extractor's per-item doc records of the
ORIGINAL tree, renders each runnable example to `tests/doc_<stem>_gen_<N>.rs`,
and keeps a file only when cargo shows it passing on the original — in the
world it is offered to first, and gated to every world it passes in
(`application.test_worlds.settle_test_gate`, the same measurement a generated
test gets). An example that passes in no world is removed and counted; it
never enters the oracle.

Offered to each build world in turn, default first, until one takes it: an
example that needs a cfg only one world sets (a nightly feature) fails in the
default world and is taken by the world that sets it.

Idempotent: the files this step wrote on an earlier pass are removed first,
recognised by their stem and the pipeline's origin marker, so a re-entry
neither duplicates an example nor renumbers past a stale file.

Hard Rule 11: this reads original source, before strip, to bake a test
artefact. What it writes is the crate's own published documentation examples,
which are public API usage by construction.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pipeline_core.domain.doc_example_test import (
    doc_example_stem,
    doc_examples,
    render_doc_example_test,
)
from pipeline_core.domain.generation import (
    has_pipeline_gen_test_origin,
    parse_gen_tests_integration_filename,
)
from pipeline_core.domain.models import DocItemRecord


@dataclass(frozen=True, slots=True)
class DocExampleDeps:
    """Collaborators, injected by the composition root."""

    # Per-item doc records of the tree at a crate root (None: no extractor).
    enumerate_doc_items: Callable[[Path], Sequence[DocItemRecord] | None]
    # A world's own gate, and the measured settle of a written file
    # (`application.test_worlds`): the gate now on the file, or None when the
    # test does not pass in the world it was offered to.
    world_gate: Callable[[Any], str]
    settle: Callable[[Path, str, Path, str, Any], str | None]
    # Whether a run under this world can verify anything (a cluster that
    # turns on no build flag cannot; an example is never offered to it,
    # because "kept" here means "shown to pass on the original").
    measurable: Callable[[Any], bool]
    log: Callable[[str], None]
    emit: Callable[..., Any] = lambda *_a, **_k: None


@dataclass(frozen=True, slots=True)
class DocExampleReading:
    """What one emission pass did."""

    examples: int = 0
    kept: tuple[str, ...] = ()          # file names, in emission order
    dropped: tuple[str, ...] = ()       # "<file>:<line> #k" of examples no world passed


def _is_ours(path: Path) -> bool:
    parsed = parse_gen_tests_integration_filename(path.name)
    if parsed is None or not parsed[0].startswith("doc_"):
        return False
    try:
        return has_pipeline_gen_test_origin(path.read_text(encoding="utf-8"))
    except OSError:
        return False


def emit_doc_example_tests(
    project_dir: Path,
    module_name: str,
    clusters: Sequence[Any],
    deps: DocExampleDeps,
) -> DocExampleReading:
    """Render, verify and keep the original's doc examples as tests."""
    records = deps.enumerate_doc_items(project_dir)
    tests_dir = project_dir / "tests"
    if not records or not clusters:
        return DocExampleReading()
    tests_dir.mkdir(parents=True, exist_ok=True)
    for old in sorted(tests_dir.glob("doc_*_gen_*.rs")):
        if _is_ours(old):
            old.unlink()
    # Default world first: an example every world passes is then written
    # under the default gate and ungated by the settle.
    order = sorted(clusters, key=lambda c: (not getattr(c, "is_default", False),
                                            str(c.name)))
    counters: dict[str, int] = {}
    kept: list[str] = []
    dropped: list[str] = []
    total = 0
    for rec in sorted(records, key=lambda r: (r.file, r.line)):
        if not rec.file.startswith("src/"):
            continue
        for k, example in enumerate(doc_examples(rec.doc)):
            total += 1
            stem = doc_example_stem(rec.file)
            n = counters.get(stem, 0) + 1
            path = tests_dir / f"{stem}_gen_{n}.rs"
            fn_name = f"{stem}_example_{n}"
            body = render_doc_example_test(
                example, fn_name=fn_name, source=f"{rec.file} (item at line {rec.line})")
            taken = False
            for cluster in order:
                if not deps.measurable(cluster):
                    continue
                own = deps.world_gate(cluster)
                path.write_text((f"#![cfg({own})]\n" if own else "") + body,
                                encoding="utf-8")
                try:
                    gate = deps.settle(project_dir, module_name, path, body, cluster)
                except Exception as exc:
                    deps.log(f"  [doc-examples] {path.name}: no reading in "
                             f"world '{cluster.name}' ({exc}).")
                    gate = None
                if gate is not None:
                    taken = True
                    break
            if taken:
                counters[stem] = n
                kept.append(path.name)
            else:
                path.unlink(missing_ok=True)
                dropped.append(f"{rec.file}:{rec.line} #{k}")
    deps.log(f"  [doc-examples] {module_name}: {total} runnable doc example(s) "
             f"— {len(kept)} pass on the original as tests and join the "
             f"oracle, {len(dropped)} dropped (pass in no world as rendered)"
             + (f": {', '.join(dropped[:6])}"
                + (f", … (+{len(dropped) - 6})" if len(dropped) > 6 else "")
                if dropped else "") + ".")
    deps.emit("task0d.doc_examples", data={
        "module": module_name, "examples": total, "kept": list(kept),
        "dropped": list(dropped)})
    return DocExampleReading(total, tuple(kept), tuple(dropped))
