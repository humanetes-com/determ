"""P2 pull protocol: REQUEST parsing, budget arithmetic, resolver containment
(HR 11), and the bounded ask loop."""
from __future__ import annotations

from pathlib import Path

from pipeline_core.application.reconstruct.pull_resolver import (
    PullSources,
    ask_with_pulls,
    resolve_pull,
    workspace_file_reader,
)
from pipeline_core.domain.context_pull import (
    ContextPull,
    PullBudget,
    PullKind,
    extract_context_pull,
)


def test_extract_request_tag() -> None:
    r = 'thinking...\n<REQUEST kind="type_def" arg="Buf"/>\n'
    pull = extract_context_pull(r)
    assert pull == ContextPull(PullKind.TYPE_DEF, "Buf")


def test_extract_full_error_needs_no_arg_others_do() -> None:
    assert extract_context_pull('<REQUEST kind="full_error" arg=""/>') == \
        ContextPull(PullKind.FULL_ERROR, "")
    assert extract_context_pull('<REQUEST kind="file" arg=""/>') is None


def test_extract_rejects_unknown_kind_and_malformed() -> None:
    assert extract_context_pull('<REQUEST kind="original_source" arg="x"/>') is None
    assert extract_context_pull("<REQUEST kind=file") is None
    assert extract_context_pull("no tag at all") is None


def test_extract_takes_last_request() -> None:
    r = ('<REQUEST kind="file" arg="src/a.rs"/> hmm no actually\n'
         '<REQUEST kind="type_def" arg="Own"/>')
    assert extract_context_pull(r) == ContextPull(PullKind.TYPE_DEF, "Own")


def test_pull_budget_is_hard() -> None:
    b = PullBudget(1)
    b2 = b.spend()
    assert b2 == PullBudget(0)
    assert b2 is not None and b2.spend() is None


# --- resolver -----------------------------------------------------------------


def _sources(tmp_path: Path, *, type_def=None, full_error=None) -> PullSources:
    return PullSources(
        read_file=workspace_file_reader(tmp_path),
        find_tests=lambda name: "",
        type_def=type_def or (lambda name: ""),
        full_error=full_error or (lambda: "error[E0308]: full text"),
    )


def test_workspace_reader_contains_paths(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src/a.rs").write_text("pub struct A;")
    outside = tmp_path.parent / "secret.rs"
    outside.write_text("ORIGINAL SOURCE")
    read = workspace_file_reader(tmp_path)
    assert read("src/a.rs") == "pub struct A;"
    assert read("../secret.rs") is None            # HR 11: escape refused
    assert read("src/a.txt") is None               # .rs only
    assert read("src/missing.rs") is None


def test_resolve_file_and_unavailable(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src/a.rs").write_text("x")
    s = _sources(tmp_path)
    ok = resolve_pull(ContextPull(PullKind.FILE, "src/a.rs"), s)
    assert ok.ok and ok.payload == "x"
    bad = resolve_pull(ContextPull(PullKind.FILE, "../a.rs"), s)
    assert not bad.ok and "no such file" in bad.payload


def test_resolve_type_def_and_full_error(tmp_path: Path) -> None:
    s = _sources(tmp_path, type_def=lambda n: f"struct {n} {{ f: u8 }}")
    td = resolve_pull(ContextPull(PullKind.TYPE_DEF, "Buf"), s)
    assert td.ok and "struct Buf" in td.payload
    fe = resolve_pull(ContextPull(PullKind.FULL_ERROR, ""), s)
    assert fe.ok and fe.payload == "error[E0308]: full text"


def test_resolve_macro_expansion_unavailable_without_runner(tmp_path: Path) -> None:
    ans = resolve_pull(ContextPull(PullKind.MACRO_EXPANSION, "src/a.rs"),
                       _sources(tmp_path))
    assert not ans.ok and "not available" in ans.payload


def test_resolver_never_raises(tmp_path: Path) -> None:
    def boom(_n: str) -> str:
        raise RuntimeError("db down")
    ans = resolve_pull(ContextPull(PullKind.TYPE_DEF, "X"),
                       _sources(tmp_path, type_def=boom))
    assert not ans.ok and "lookup failed" in ans.payload


def test_resolve_caps_and_flags_truncation(tmp_path: Path) -> None:
    s = _sources(tmp_path, type_def=lambda n: "x" * 9_000)
    ans = resolve_pull(ContextPull(PullKind.TYPE_DEF, "Big"), s)
    assert ans.truncated and len(ans.payload) == 8_000


# --- ask loop -------------------------------------------------------------------


_BODY = '<FN name="f" block="impl A">1</FN>'


def _loop(responses, tmp_path, max_pulls=2):
    seq = list(responses)
    asked: list[str] = []

    def ask(p: str) -> str:
        asked.append(p)
        return str(seq.pop(0) if len(seq) > 1 else seq[0])

    out = ask_with_pulls(
        ask, "PROMPT", sources=_sources(
            tmp_path, type_def=lambda n: f"struct {n};"),
        has_deliverable=lambda r: "<FN " in r,
        log=lambda _m: None, emit=lambda *_a, **_k: None,
        max_pulls=max_pulls)
    return out, asked


def test_ask_loop_answers_request_then_gets_bodies(tmp_path: Path) -> None:
    out, asked = _loop(
        ['<REQUEST kind="type_def" arg="Buf"/>', _BODY], tmp_path)
    assert out == _BODY and len(asked) == 2
    assert "struct Buf;" in asked[1]            # the answer reached the re-ask
    assert "REQUESTED_CONTEXT" in asked[1]


def test_ask_loop_deliverable_short_circuits(tmp_path: Path) -> None:
    mixed = _BODY + '\n<REQUEST kind="file" arg="src/a.rs"/>'
    out, asked = _loop([mixed, "never"], tmp_path)
    assert out == mixed and len(asked) == 1     # bodies win over the stray tag


def test_ask_loop_budget_exhausts(tmp_path: Path) -> None:
    req = '<REQUEST kind="type_def" arg="Buf"/>'
    out, asked = _loop([req, req, req, "ignored"], tmp_path, max_pulls=1)
    assert out == req and len(asked) == 2       # 1 answered, then stop


def test_ask_loop_plain_response_passes_through(tmp_path: Path) -> None:
    out, asked = _loop(["gibberish no tag"], tmp_path)
    assert out == "gibberish no tag" and len(asked) == 1
