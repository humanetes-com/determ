"""Integration: a fill prompt says what it holds, and a request for a file the
conversation already carries whole is answered by pointer — with every body
rewritten on disk since — instead of the file again.

The class, measured 2026-09-24 over every fill since 2026-09-01
(`scripts/prompt_sufficiency_census.py`): 302 of 690 context requests named a
file or type the prompt had already rendered. Per-fn prompt `956e2150…` held
all of `src/error.rs`, and its thinking said "Let me request the error module
to see how Error is created". Nothing told the model what the prompt
contained; the resolver re-sent the raw file, which carries the inactive cfg
arms the per-fn view had filtered out, so the ask bought the same code plus
noise and a re-send of the whole prompt.

Real pieces, no model and no cargo: the task2a reader
(`reconstruction_file_reader` over a cluster tree whose `tests` is a symlink
into the project), the per-fn builder, the per-fn ask ladder, the request
answerer, and the ask client with the contents hook the composition root
binds. The crate is invented; between rendering the prompt and answering the
request a sibling body is rewritten on disk, as a parallel unit's splice
would. The negative control is the same request with no conversation — the
pre-change call — which re-sends the file.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from pipeline_core.adapters.ask_client import AskClient
from pipeline_core.adapters.per_fn_ask import ask_per_fn_body, conversation_factory
from pipeline_core.application.reconstruct.pull_resolver import (
    PullSources,
    make_request_answerer,
    reconstruction_file_reader,
)
from pipeline_core.domain.prompt_holdings import CONTENTS_OPEN, contents_index
from pipeline_core.prompts.context import PerFnSections
from pipeline_core.prompts.per_fn import build_per_fn_prompt

pytestmark = pytest.mark.integration

_LIB = """\
pub struct Handle { id: u32 }
impl Handle {
    pub fn open(id: u32) -> Self { Handle { id } }
    pub fn id(&self) -> u32 { unimplemented!() }
    pub fn twice(&self) -> u32 { unimplemented!() }
}
"""


def _workspace(tmp_path: Path) -> tuple[Path, Path]:
    project = tmp_path / "proj"
    (project / "tests").mkdir(parents=True)
    (project / "src").mkdir()
    (project / "src" / "lib.rs").write_text("// stripped shell\n")
    cluster = project / ".skeleton-filled" / "main"
    (cluster / "src").mkdir(parents=True)
    (cluster / "src" / "lib.rs").write_text(_LIB)
    (cluster / "tests").symlink_to(project / "tests")
    return cluster, project


class _Model:
    """Asks for the file it was given, then answers with a body. Records the
    last user turn of each call (`seen`) and each call's messages."""

    def __init__(self) -> None:
        self.seen: list[str] = []
        self.calls: list[list[dict[str, Any]]] = []

    def __call__(self, messages: list[dict[str, Any]], **_k: object) -> str:
        self.calls.append([dict(m) for m in messages])
        self.seen.append(str(messages[-1]["content"]))
        if len(self.seen) == 1:
            return '<REQUEST kind="file" arg="src/lib.rs"/>'
        return "<BODY>\nself.id\n</BODY>"


def _client(model: _Model) -> AskClient:
    return AskClient(
        ask_raw=lambda *_a, **_k: "", hot_reload=lambda: None,
        emit=lambda *_a, **_k: None, guard=lambda _t, _m: None,
        store_prompt_content=lambda s: {"sha256": "sha", "char_count": len(s)},
        caller_site=lambda: "test", disclaimer=lambda: "DISCLAIMER",
        model_name="m", context_chars=1_000_000,
        single_call_char_limit=1_000_000, contents=contents_index,
        ask_turns_raw=model)


def _prompt(cluster: Path, sources: PullSources) -> str:
    text = (cluster / "src" / "lib.rs").read_text()
    return build_per_fn_prompt(
        file_text=text, fn_name="id", sig_text="pub fn id(&self) -> u32",
        test_blocks=[], module_name="m", cluster_name="main",
        file_rel="src/lib.rs", fr_path=None,
        sections=PerFnSections(
            macros="", notes="", container="impl Handle", types="",
            invariants="", siblings="", crate_idioms="", bound_methods="",
        ))


def test_the_ladder_points_at_the_held_file_and_carries_the_rewrite(
        tmp_path: Path) -> None:
    cluster, project = _workspace(tmp_path)
    sources = PullSources(
        read_file=reconstruction_file_reader(cluster, project),
        find_tests=lambda _n: "", type_def=lambda _n: "",
        full_error=lambda: "")
    prompt = _prompt(cluster, sources)
    # A parallel unit fills `twice` after this prompt was rendered.
    (cluster / "src" / "lib.rs").write_text(
        _LIB.replace("fn twice(&self) -> u32 { unimplemented!() }",
                     "fn twice(&self) -> u32 { self.id * 2 }"))
    logs: list[str] = []
    model = _Model()
    conv = conversation_factory(
        _client(model), model="m", log=logs.append,
        emit=lambda *_a, **_k: None, max_transcript_chars=1_000_000)(
        "src/lib.rs::id", sources=sources, max_pulls=2)
    body, _ = ask_per_fn_body(prompt, "id", [], conversation=conv)
    assert body == "self.id"
    first, second = model.seen
    # The answer follows the model's own request turn on one transcript.
    assert [m["role"] for m in model.calls[1]] == [
        "system", "user", "assistant", "user"]
    assert model.calls[1][2]["content"] == '<REQUEST kind="file" arg="src/lib.rs"/>'
    # The prompt opened with its index, and the index named the whole file.
    assert first.startswith(CONTENTS_OPEN)
    assert "src/lib.rs — the whole file" in first
    # The answer pointed at it, carried the sibling rewritten since, and did
    # not send the file again.
    assert "It is not sent again." in second
    assert "fn twice(&self) -> u32 { self.id * 2 }" in second
    sent_two = "\n".join(str(m["content"]) for m in model.calls[1])
    assert sent_two.count("pub fn open(id: u32) -> Self { Handle { id } }") == 1
    assert any("already held whole" in ln for ln in logs)


def test_negative_control_without_the_conversation_the_file_is_resent(
        tmp_path: Path) -> None:
    cluster, project = _workspace(tmp_path)
    sources = PullSources(
        read_file=reconstruction_file_reader(cluster, project),
        find_tests=lambda _n: "", type_def=lambda _n: "",
        full_error=lambda: "")
    answer = make_request_answerer(sources, log=lambda _m: None,
                                   emit=lambda *_a, **_k: None)
    out = answer('<REQUEST kind="file" arg="src/lib.rs"/>')
    assert out is not None and "It is not sent again." not in out
    assert "pub fn open(id: u32) -> Self { Handle { id } }" in out
