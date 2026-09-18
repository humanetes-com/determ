"""Fake-adapter unit tests for the static/LLM/file-list TASK-2 generators
(pinned at the point `generate` became `GenerationRequest`-driven).

One fake object satisfies the Telemetry/LLM/Workspace/ContextDocs roles; the
generators are driven through their happy paths + the key fallback branches with
no real LLM, cargo, or pipeline.py. The skeleton generator has its own suite
(test_skeleton_task2_generator.py)."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from pipeline_core.application.generators.task2_generator import (
    LLMTask2Generator,
    StaticFileListTask2Generator,
    StaticTask2Generator,
)
from pipeline_core.domain.generation import GenerationRequest, Task2Config

_CODE = 'pub fn foo() -> i32 {\n    let x = 1;\n    x + 1\n}\n'


def _file_block(path: str, body: str = _CODE) -> str:
    return f'<FILE path="{path}">\n{body}</FILE>\n'


@dataclass
class FakeAdapter:
    """Telemetry + LLM + Workspace + ContextDocs in one object."""
    ask_responses: list[str] = field(default_factory=list)
    structure: list[str] = field(default_factory=list)
    entry_point: str | None = None
    proc_macro: bool = False
    written: dict[str, str] = field(default_factory=dict)
    logs: list[str] = field(default_factory=list)
    prompts: list[str] = field(default_factory=list)

    # Telemetry
    def log(self, msg: str) -> None:
        self.logs.append(msg)

    # LLM
    def ask(self, prompt: str, *, model: str | None = None) -> str:
        self.prompts.append(prompt)
        return self.ask_responses.pop(0) if self.ask_responses else ""

    # Workspace
    def read(self, path: Path) -> str:
        return path.read_text(encoding="utf-8")

    def write_source_files(self, files_dict: dict[str, str], project_dir: Path,
                           manifest_path: Path, existing_test_files: Any) -> int:
        self.written.update(files_dict)
        return len(files_dict)

    def load_original_structure(self, project_dir: Path,
                                module_name: str) -> list[str]:
        return list(self.structure)

    def crate_entry_point(self, cargo_toml_path: Path, module_dir_rel: str,
                          file_path_set: set[str]) -> str | None:
        return self.entry_point

    def is_proc_macro_crate(self, cargo_toml_text: str) -> bool:
        return self.proc_macro

    # ContextDocs
    def scan_downstream_api(self, project_dir: Path, module_name: str) -> str:
        return "\nDownstream API: (none)\n"

    def compile_patterns_read(self, project_dir: Path, module: str) -> str:
        return "Known pitfalls: none"

    def genesis_rules_read(self, project_dir: Path, module_scope: str = "*") -> str:
        return "Rules: none"

    def extract_test_assertions(self, test_files: list[tuple[str, str]],
                                module_dir_rel: str = "") -> str:
        return "Assertions: none"

    def format_api_surface_section(self, api_surface: str) -> str:
        return f"\nAPI surface:\n{api_surface}\n" if api_surface else ""

    def split_api_surface_by_file(self, api_surface: str) -> list[tuple[str, str]]:
        return [(p, f"// sig for {p}") for p in self.structure]


def _cfg() -> Task2Config:
    return Task2Config(
        file_format_instruction='Use <FILE path="...">…</FILE> markers.',
        task2_model="m", thinking_directive="", verbosity_guard="",
        genesis_cap=100_000, strategy_cap=50_000, single_call_char_limit=200_000,
        model_label="fake",
    )


def _project(tmp_path: Path) -> Path:
    pd = tmp_path / "ws"  # NOT "crate" — must differ from module_dir_rel's leading seg
    (pd / "crate" / "src").mkdir(parents=True)
    (pd / "crate" / "Cargo.toml").write_text("[package]\nname='c'\n", encoding="utf-8")
    (pd / "Cargo.toml").write_text("[package]\nname='c'\n", encoding="utf-8")
    (pd / "genesis.md").write_text("# genesis\n", encoding="utf-8")
    (pd / "functional_requirements.md").write_text("# fr\n", encoding="utf-8")
    (pd / "tests").mkdir()
    (pd / "tests" / "it.rs").write_text("#[test] fn t() { assert_eq!(foo(), 2); }",
                                        encoding="utf-8")
    return pd


def _req(pd: Path, **over: Any) -> GenerationRequest:
    base = GenerationRequest(
        project_dir=pd, genesis_path=pd / "genesis.md",
        functional_req_path=pd / "functional_requirements.md",
        manifest_path=pd / "manifest.json", existing_test_files=["tests/it.rs"],
        current_module="crate", module_dir_rel="crate/", api_surface="pub fn foo();",
    )
    return replace(base, **over)


# --- StaticTask2Generator ---------------------------------------------------

def test_static_happy(tmp_path: Path) -> None:
    pd = _project(tmp_path)
    fake = FakeAdapter(ask_responses=[_file_block("crate/src/lib.rs")])
    gen = StaticTask2Generator(tel=fake, llm=fake, ws=fake, docs=fake, cfg=_cfg())
    assert gen.generate(_req(pd)) is True
    assert "crate/src/lib.rs" in fake.written


def test_static_proc_macro_note(tmp_path: Path) -> None:
    pd = _project(tmp_path)
    fake = FakeAdapter(ask_responses=[_file_block("crate/src/lib.rs")])
    gen = StaticTask2Generator(tel=fake, llm=fake, ws=fake, docs=fake, cfg=_cfg())
    # "proc-macro target" in workspace_structure triggers the PROC_MACRO_NOTE branch.
    assert gen.generate(_req(pd, workspace_structure="crate (proc-macro target)")) is True


def test_static_discards_out_of_scope(tmp_path: Path) -> None:
    pd = _project(tmp_path)
    # only an out-of-scope file → discarded → no in-scope files → retry → False
    fake = FakeAdapter(ask_responses=[_file_block("other/src/lib.rs")] * 5)
    gen = StaticTask2Generator(tel=fake, llm=fake, ws=fake, docs=fake, cfg=_cfg())
    assert gen.generate(_req(pd)) is False
    assert any("DISCARDED" in m for m in fake.logs)


def test_static_no_files_returned(tmp_path: Path) -> None:
    pd = _project(tmp_path)
    fake = FakeAdapter(ask_responses=[""] * 5)
    gen = StaticTask2Generator(tel=fake, llm=fake, ws=fake, docs=fake, cfg=_cfg())
    assert gen.generate(_req(pd)) is False
    assert any("returned no source files" in m for m in fake.logs)


# --- LLMTask2Generator ------------------------------------------------------

def test_llm_happy(tmp_path: Path) -> None:
    pd = _project(tmp_path)
    fake = FakeAdapter(ask_responses=[
        "crate/src/lib.rs\n",                      # plan
        _file_block("crate/src/lib.rs"),           # per-file
    ])
    gen = LLMTask2Generator(tel=fake, llm=fake, ws=fake, docs=fake, cfg=_cfg())
    assert gen.generate(_req(pd)) is True
    assert "crate/src/lib.rs" in fake.written


def test_llm_filename_match_fallback(tmp_path: Path) -> None:
    pd = _project(tmp_path)
    fake = FakeAdapter(ask_responses=[
        "crate/src/lib.rs\n",
        _file_block("lib.rs"),  # different path, same basename → matched by filename
    ])
    gen = LLMTask2Generator(tel=fake, llm=fake, ws=fake, docs=fake, cfg=_cfg())
    assert gen.generate(_req(pd)) is True
    assert any("by filename" in m for m in fake.logs)


def test_llm_empty_plan(tmp_path: Path) -> None:
    pd = _project(tmp_path)
    fake = FakeAdapter(ask_responses=[""])  # plan returns nothing
    gen = LLMTask2Generator(tel=fake, llm=fake, ws=fake, docs=fake, cfg=_cfg())
    assert gen.generate(_req(pd)) is False
    assert any("no file list" in m for m in fake.logs)


# --- StaticFileListTask2Generator -------------------------------------------

def test_filelist_happy(tmp_path: Path) -> None:
    pd = _project(tmp_path)
    fake = FakeAdapter(structure=["crate/src/lib.rs"],
                       entry_point="crate/src/lib.rs",
                       ask_responses=[_file_block("crate/src/lib.rs")])
    gen = StaticFileListTask2Generator(tel=fake, llm=fake, ws=fake, docs=fake, cfg=_cfg())
    assert gen.generate(_req(pd, coverage_strategy="use foo()")) is True
    assert "crate/src/lib.rs" in fake.written


def test_filelist_proc_macro(tmp_path: Path) -> None:
    pd = _project(tmp_path)
    fake = FakeAdapter(structure=["crate/src/lib.rs"], entry_point="crate/src/lib.rs",
                       proc_macro=True, ask_responses=[_file_block("crate/src/lib.rs")])
    gen = StaticFileListTask2Generator(tel=fake, llm=fake, ws=fake, docs=fake, cfg=_cfg())
    assert gen.generate(_req(pd)) is True
    # proc-macro note must have reached the per-file prompt
    assert any("PROC-MACRO CRATE" in p for p in fake.prompts)


def test_filelist_fallback_to_llm(tmp_path: Path) -> None:
    pd = _project(tmp_path)
    # empty structure → falls back to LLMTask2Generator (plan + per-file).
    fake = FakeAdapter(structure=[], ask_responses=[
        "crate/src/lib.rs\n", _file_block("crate/src/lib.rs"),
    ])
    gen = StaticFileListTask2Generator(tel=fake, llm=fake, ws=fake, docs=fake, cfg=_cfg())
    assert gen.generate(_req(pd)) is True
    assert any("falling back to LLM" in m for m in fake.logs)
