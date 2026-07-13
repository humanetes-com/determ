"""Pins for the F11 layout-authoring use-case (t1.5 orchestration)."""
from __future__ import annotations

from pipeline_core.application.author_layout import (
    AuthorLayoutDeps,
    author_layout,
)
from pipeline_core.domain.models import TypeRecord
from pipeline_core.domain.private_surface import classify_private_surface


def _surface():
    types = [
        TypeRecord(file="src/lib.rs", kind="struct", name="Error", is_pub=True,
                   definition="pub struct Error { inner: Box<ErrorImpl> }"),
        TypeRecord(file="src/error.rs", kind="struct", name="ErrorImpl", is_pub=False,
                   definition="struct ErrorImpl { vtable: u8 }"),
    ]
    return classify_private_surface(types, [], cfg_cluster="stable")


class _Deps:
    def __init__(self, reply, compile_results):
        self.reply = reply
        self.compile_results = list(compile_results)
        self.files = {"src/error.rs": "struct ErrorImpl { vtable: u8 }\n"}
        self.logs: list[str] = []
        self.prompts: list[str] = []

    def deps(self):
        def run_model(p):
            self.prompts.append(p)
            return self.reply
        return AuthorLayoutDeps(
            run_model=run_model,
            read_skeleton=lambda rel: self.files.get(rel),
            write_skeleton=lambda rel, txt: self.files.__setitem__(rel, txt),
            compile_ok=lambda: self.compile_results.pop(0),
            log=self.logs.append, max_attempts=3)


_GOOD = ('<LAYOUT_ITEM name="ErrorImpl" kind="struct">'
         'struct ErrorImpl { inner: Box<dyn StdError> }</LAYOUT_ITEM>')


def test_happy_path_authors_and_commits():
    d = _Deps(_GOOD, [True])
    res = author_layout(_surface(), module_name="anyhow", public_surface="pub struct Error;",
                        layout_pins=[], symbol_inventory="Box", deps=d.deps())
    assert res.all_authored
    assert "Box<dyn StdError>" in d.files["src/error.rs"]


def test_compile_failure_reverts_scaffold():
    # authored def never compiles → skeleton must be restored to frozen state.
    d = _Deps(_GOOD, [False, False, False])
    res = author_layout(_surface(), module_name="anyhow", public_surface="",
                        layout_pins=[], symbol_inventory="", deps=d.deps())
    assert not res.all_authored
    assert d.files["src/error.rs"] == "struct ErrorImpl { vtable: u8 }\n"


def test_incomplete_coverage_retries_then_gives_up():
    d = _Deps("<LAYOUT_ITEM name=\"Other\" kind=\"struct\">struct Other;</LAYOUT_ITEM>",
              [True])
    res = author_layout(_surface(), module_name="anyhow", public_surface="",
                        layout_pins=[], symbol_inventory="", deps=d.deps())
    assert not res.all_authored
    assert any("missing authored items" in m for m in d.logs)
    # never wrote a partial scaffold.
    assert d.files["src/error.rs"] == "struct ErrorImpl { vtable: u8 }\n"


def test_second_attempt_recovers_after_compile_fail():
    d = _Deps(_GOOD, [False, True])
    res = author_layout(_surface(), module_name="anyhow", public_surface="",
                        layout_pins=[], symbol_inventory="", deps=d.deps())
    assert res.all_authored
    assert len(d.prompts) == 2  # retried once
