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

Beside the files it writes the origin of every fenced doc block it read, kept
or not (`domain.doc_stand_in.ORIGINS_STORE`): the file, item line, fence
offset, whether the doc was written in a macro body, and the file it became.
That record is how the reference floor knows which of the original's doc
tests a re-hosted example stands for; this loop is the only writer of both,
so they are rewritten together.

Hard Rule 11: this reads original source, before strip, to bake a test
artefact. What it writes is the crate's own published documentation examples,
which are public API usage by construction.
"""
from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from pipeline_core.domain.doc_example_test import (
    DocExample,
    doc_example_names,
    doc_example_stem,
    doc_fences,
    render_doc_example_test,
    rendered_sha,
)
from pipeline_core.domain.doc_stand_in import (
    ORIGINS_STORE,
    DocOrigin,
    origins_to_json,
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
    """Render, verify and keep the original's doc examples as tests, and
    record the origin of every fence read beside them."""
    records = deps.enumerate_doc_items(project_dir)
    tests_dir = project_dir / "tests"
    store = project_dir / ORIGINS_STORE
    # Nothing to emit (no extractor reading, no world): the files and the
    # store an earlier pass wrote stay as they are, together.
    if not records or not clusters:
        return DocExampleReading()
    tests_dir.mkdir(parents=True, exist_ok=True)
    store.unlink(missing_ok=True)
    for old in sorted(tests_dir.glob("doc_*_gen_*.rs")):
        if _is_ours(old):
            old.unlink()
    # Default world first: an example every world passes is then written
    # under the default gate and ungated by the settle.
    order = sorted(clusters, key=lambda c: (not getattr(c, "is_default", False),
                                            str(c.name)))
    run = _Emission(tests_dir, project_dir, module_name, order, deps)
    try:
        for rec in sorted(records, key=lambda r: (r.file, r.line)):
            if rec.file.startswith("src/"):
                run.record(rec)
    finally:
        # However the pass ends, the store names exactly the files on disk:
        # a file written for an example that never got its origin is removed.
        if run.in_flight is not None:
            run.in_flight.unlink(missing_ok=True)
        store.write_text(json.dumps(origins_to_json(run.origins), indent=2),
                         encoding="utf-8")
    kept, dropped, origins = run.kept, run.dropped, run.origins
    deps.log(f"  [doc-examples] {module_name}: {run.total} runnable doc example(s) "
             f"— {len(kept)} pass on the original as tests and join the "
             f"oracle, {len(dropped)} dropped (pass in no world as rendered)"
             + (f": {', '.join(dropped[:6])}"
                + (f", … (+{len(dropped) - 6})" if len(dropped) > 6 else "")
                if dropped else "")
             + f"; {len(origins)} origin(s) recorded in {ORIGINS_STORE}, "
             f"{sum(1 for o in origins if o.in_macro_body)} written in a macro body.")
    deps.emit("task0d.doc_examples", data={
        "module": module_name, "examples": run.total, "kept": list(kept),
        "dropped": list(dropped), "origins": len(origins)})
    return DocExampleReading(run.total, tuple(kept), tuple(dropped))


class _Emission:
    """One emission pass: numbers, renders and settles each example, and
    holds every origin it has decided. `in_flight` is the file written for
    the example being decided, None between examples."""

    def __init__(self, tests_dir: Path, project_dir: Path, module_name: str,
                 order: Sequence[Any], deps: DocExampleDeps) -> None:
        self.tests_dir, self.project_dir = tests_dir, project_dir
        self.module_name, self.order, self.deps = module_name, order, deps
        self.counters: dict[str, int] = {}
        self.kept: list[str] = []
        self.dropped: list[str] = []
        self.origins: list[DocOrigin] = []
        self.total = 0
        self.in_flight: Path | None = None

    def record(self, rec: DocItemRecord) -> None:
        k = -1
        for index, fence in enumerate(doc_fences(rec.doc)):
            where = DocOrigin(file=rec.file, item_line=rec.line, index=index,
                              offset=fence.offset, in_macro_body=rec.in_macro_body)
            if not fence.runs:
                self.origins.append(replace(
                    where, reason=f"not re-hostable: rustdoc `{fence.skip_tag}`"))
                continue
            k += 1
            self.total += 1
            self._example(rec, where, k,
                          DocExample(fence.code, fence.should_panic, fence.offset))

    def _example(self, rec: DocItemRecord, where: DocOrigin, k: int,
                 example: DocExample) -> None:
        stem = doc_example_stem(rec.file)
        n = self.counters.get(stem, 0) + 1
        file_name, fn_name = doc_example_names(stem, n)
        path = self.tests_dir / file_name
        body = render_doc_example_test(
            example, fn_name=fn_name, source=f"{rec.file} (item at line {rec.line})")
        self.in_flight = path
        taken = False
        for cluster in self.order:
            if not self.deps.measurable(cluster):
                continue
            own = self.deps.world_gate(cluster)
            path.write_text((f"#![cfg({own})]\n" if own else "") + body,
                            encoding="utf-8")
            try:
                gate = self.deps.settle(self.project_dir, self.module_name, path,
                                        body, cluster)
            except Exception as exc:
                self.deps.log(f"  [doc-examples] {path.name}: no reading in "
                              f"world '{cluster.name}' ({exc}).")
                gate = None
            if gate is not None:
                taken = True
                break
        if taken:
            sha = rendered_sha(path.read_text(encoding="utf-8"))
            self.counters[stem] = n
            self.kept.append(file_name)
            self.origins.append(replace(where, kept_as=file_name, test_fn=fn_name,
                                        sha=sha))
        else:
            path.unlink(missing_ok=True)
            self.dropped.append(f"{rec.file}:{rec.line} #{k}")
            self.origins.append(replace(
                where, reason="dropped at emission: passes in no world as rendered"))
        self.in_flight = None
