"""The pull protocol: REQUEST parsing, budget arithmetic, resolver containment
(no path may escape the reconstruction tree and reach upstream source), and the
bounded ask loop."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from pipeline_core.application.reconstruct.pull_resolver import (
    PullSources,
    make_request_answerer,
    resolve_pull,
    workspace_file_reader,
)
from pipeline_core.application.reconstruct.unit_conversation import (
    ConversationPorts,
    UnitConversation,
)
from pipeline_core.domain.context_pull import (
    ContextPull,
    PullBudget,
    PullKind,
    extract_context_pull,
    extract_context_pulls,
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


def test_extract_all_requests_in_document_order() -> None:
    """The reply shape that lost the src/error.rs budget on 2026-08-03: two
    artifacts named in one batch, of which only the last used to be read."""
    r = ('<REQUEST kind="file" arg="src/ext.rs"/>'
         '<REQUEST kind="test_source" arg="test_root_cause"/>')
    assert extract_context_pulls(r) == (
        ContextPull(PullKind.FILE, "src/ext.rs"),
        ContextPull(PullKind.TEST_SOURCE, "test_root_cause"))


def test_extract_all_dedupes_and_skips_malformed() -> None:
    r = ('<REQUEST kind="type_def" arg="Buf"/> '
         '<REQUEST kind="original_source" arg="x"/> '   # unknown kind
         '<REQUEST kind="file" arg=""/> '               # argless
         '<REQUEST kind="type_def" arg="Buf"/>')        # repeat
    assert extract_context_pulls(r) == (ContextPull(PullKind.TYPE_DEF, "Buf"),)


def test_extract_malformed_tail_no_longer_hides_a_good_request() -> None:
    """The scan skips a bad tag instead of ending on it — a stray `<REQUEST`
    in prose after a real request used to make the whole reply requestless."""
    r = '<REQUEST kind="type_def" arg="Buf"/> then I wrote <REQUEST kind=file'
    assert extract_context_pull(r) == ContextPull(PullKind.TYPE_DEF, "Buf")


def test_extract_all_none_when_no_tag() -> None:
    assert extract_context_pulls("no tag at all") == ()


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
    assert read("../secret.rs") is None            # no walk to upstream source
    assert read("src/a.txt") is None               # .rs only
    assert read("src/missing.rs") is None


def _dual_root_workspace(tmp_path: Path) -> tuple[Path, Path]:
    """A task2a-shaped workspace: a cluster tree whose `src/` holds the fills
    and whose `tests` is a symlink into the project's real `tests/`, and a
    project dir whose own `src/` is only the post-strip shell."""
    project = tmp_path / "proj"
    (project / "tests").mkdir(parents=True)
    (project / "tests/common.rs").write_text("pub fn bail_literal() {}")
    (project / "src").mkdir()
    (project / "src/error.rs").write_text("// one-line shell")   # stripped
    cluster = project / ".skeleton-filled" / "stable"
    (cluster / "src").mkdir(parents=True)
    (cluster / "src/error.rs").write_text("pub fn construct() { /* filled */ }")
    (cluster / "tests").symlink_to(project / "tests")            # as the pipeline builds it
    return cluster, project


def test_single_root_reader_cannot_reach_the_oracle_tests(tmp_path: Path) -> None:
    # NEGATIVE CONTROL for reconstruction_file_reader: a reader locked to the
    # cluster tree alone refuses `tests/common.rs`, because the cluster's
    # `tests` symlink resolves outside the cluster root before containment.
    # This is the `4911-9ab8852e` `no such file in this crate` refusal.
    cluster, _project = _dual_root_workspace(tmp_path)
    cluster_only = workspace_file_reader(cluster)
    assert cluster_only("tests/common.rs") is None                # the bug
    assert "filled" in (cluster_only("src/error.rs") or "")       # src ok already


def test_reconstruction_reader_serves_cluster_src_and_project_tests(
    tmp_path: Path,
) -> None:
    from pipeline_core.application.reconstruct.pull_resolver import (
        reconstruction_file_reader,
    )
    cluster, project = _dual_root_workspace(tmp_path)
    read = reconstruction_file_reader(cluster, project)
    # tests/ now served (from the project root) — the fix.
    assert read("tests/common.rs") == "pub fn bail_literal() {}"
    # src/ still served from the CLUSTER tree, never the project shell.
    assert "filled" in (read("src/error.rs") or "")
    assert "shell" not in (read("src/error.rs") or "")
    # containment still holds on both roots; still `.rs`-only.
    assert read("../secret.rs") is None
    assert read("tests/common.txt") is None
    # Fall-through is on ABSENCE only: an EMPTY cluster file is the
    # reconstruction's answer for that path and must not be replaced by the
    # project root's version of it.
    (cluster / "src/empty.rs").write_text("")
    (project / "src/empty.rs").write_text("// project-side text")
    assert read("src/empty.rs") == ""


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


def _loop(responses, tmp_path, max_pulls=2, transcripts=None):
    """One conversation opened with "PROMPT" over a scripted transport;
    `asked[i]` is the user turn call i sent."""
    seq = list(responses)
    asked: list[str] = []

    def ask_turns(messages, **_k) -> str:
        asked.append(str(messages[-1]["content"]))
        if transcripts is not None:
            transcripts.append([dict(m) for m in messages])
        return str(seq.pop(0) if len(seq) > 1 else seq[0])

    conv = UnitConversation(
        ConversationPorts(ask_turns=ask_turns, log=lambda _m: None),
        label="unit", route="test",
        requests=make_request_answerer(
            _sources(tmp_path, type_def=lambda n: f"struct {n};"),
            log=lambda _m: None, emit=lambda *_a, **_k: None,
            max_pulls=max_pulls))
    out = conv.open("PROMPT", has_deliverable=lambda r: "<FN " in r)
    return out, asked


def test_the_request_turn_stays_on_the_transcript(tmp_path: Path) -> None:
    """The answer follows the model's own request turn; the prompt is not
    re-sent as a new document with the answer glued on."""
    req = '<REQUEST kind="type_def" arg="Buf"/>'
    seen: list[list[dict]] = []
    out, _ = _loop([req, _BODY], tmp_path, transcripts=seen)
    assert out == _BODY
    assert [m["role"] for m in seen[1]] == ["user", "assistant", "user"]
    assert seen[1][0]["content"] == "PROMPT"
    assert seen[1][1]["content"] == req
    assert "struct Buf;" in seen[1][2]["content"]
    assert "PROMPT" not in seen[1][2]["content"]


def test_control_the_pre_conversation_loop_dropped_the_request_turn() -> None:
    """Negative control: the loop this replaced (`ask_with_pulls`, deleted
    2026-09-24) sent `prompt + answer` as one document — the request the
    model wrote is nowhere in what it was sent next."""
    req = '<REQUEST kind="type_def" arg="Buf"/>'
    sent: list[str] = []
    replies = [req, _BODY]

    def ask(p: str) -> str:
        sent.append(p)
        return replies.pop(0)

    current = "PROMPT"
    reply = ask(current)
    current = current + "\n<REQUESTED_CONTEXT>struct Buf;</REQUESTED_CONTEXT>"
    ask(current)
    assert reply == req and req not in sent[1]


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
    """The spent budget buys ONE more ask, and it says the budget is spent."""
    req = '<REQUEST kind="type_def" arg="Buf"/>'
    out, asked = _loop([req, req, req, "ignored"], tmp_path, max_pulls=1)
    # ask 1 → request; ask 2 (answered) → request again; ask 3 carries the
    # refusal notice. Its reply is returned whatever it holds.
    assert out == req and len(asked) == 3
    assert "no context requests left" in asked[2]
    assert "Do NOT request anything further" in asked[2]


def test_ask_loop_spent_budget_recovers_the_body(tmp_path: Path) -> None:
    """The convicting pin. A model that asks once too often is not refusing to
    answer — told so, it answers, and pre-fix that body did not exist: the
    request-only reply was returned and the caller logged `empty/no FN blocks`
    (nightly src/error.rs, 2026-08-03 12:02 UTC)."""
    req = '<REQUEST kind="type_def" arg="Buf"/>'
    out, asked = _loop([req, _BODY], tmp_path, max_pulls=0)
    assert out == _BODY and len(asked) == 2
    assert "no context requests left" in asked[1]


def test_ask_loop_warns_once_and_stops(tmp_path: Path) -> None:
    """A model that ignores the notice ends the loop — the notice is not a new
    round-trip budget."""
    req = '<REQUEST kind="type_def" arg="Buf"/>'
    out, asked = _loop([req], tmp_path, max_pulls=0)
    assert out == req and len(asked) == 2


def test_ask_loop_answers_every_request_in_one_reply(tmp_path: Path) -> None:
    """The shape that lost the budget: two artifacts named in one reply. Both
    are served in ONE round-trip; pre-fix only `Own` was, and `Buf` was dropped
    without a log line."""
    two = ('<REQUEST kind="type_def" arg="Buf"/>'
           '<REQUEST kind="type_def" arg="Own"/>')
    out, asked = _loop([two, _BODY], tmp_path, max_pulls=2)
    assert out == _BODY and len(asked) == 2
    assert "struct Buf;" in asked[1] and "struct Own;" in asked[1]
    # One task, not two — the closing instruction is rendered once.
    assert asked[1].count("Now complete the ORIGINAL task") == 1


def test_ask_loop_serves_a_batch_only_as_far_as_the_budget(tmp_path: Path) -> None:
    """Batching cannot buy more context than asking one at a time: the cap is
    on artifacts served. The unaffordable tail is refused, not silently lost."""
    three = ('<REQUEST kind="type_def" arg="A"/>'
             '<REQUEST kind="type_def" arg="B"/>'
             '<REQUEST kind="type_def" arg="C"/>')
    out, asked = _loop([three, three, _BODY], tmp_path, max_pulls=2)
    assert out == _BODY and len(asked) == 3
    assert "struct A;" in asked[1] and "struct B;" in asked[1]
    assert "struct C;" not in asked[1]
    assert 'arg="C"' in asked[2] and "no context requests left" in asked[2]


def test_ask_loop_plain_response_passes_through(tmp_path: Path) -> None:
    out, asked = _loop(["gibberish no tag"], tmp_path)
    assert out == "gibberish no tag" and len(asked) == 1


# --- ladder integration (per_fn_ask + make_request_answerer) ------------------


from pipeline_core.adapters.per_fn_ask import (  # noqa: E402
    ask_per_fn_body,
    conversation_factory,
)


class _LadderClient:
    """Protocol-complete scripted transcript client."""

    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    def ask_turns(self, messages, *, model=None, max_tokens=None, think=None,
                  temperature=None, followup_think=False) -> str:
        self.calls.append({
            "prompt": str(messages[-1]["content"]), "think": think,
            "transcript": "\n\n".join(str(m["content"]) for m in messages)})
        return (self.responses.pop(0) if len(self.responses) > 1
                else self.responses[0])

    def fit(self, prompt: str) -> str:
        return prompt


def _ladder(client: _LadderClient, tmp_path: Path, max_pulls: int | None):
    conv = conversation_factory(
        client, model="m", log=lambda _m: None, emit=lambda *_a, **_k: None,
        max_transcript_chars=0)(
        "foo", sources=(None if max_pulls is None else _sources(
            tmp_path, type_def=lambda n: f"struct {n};")),
        max_pulls=max_pulls or 0)
    return ask_per_fn_body("PROMPT", "foo", [], conversation=conv,
                           sig_text="fn foo()")


def test_ladder_answers_request_on_same_rung(tmp_path: Path) -> None:
    """A bodyless REQUEST reply is answered on the transcript at the SAME
    rung — no think=False rung burned."""
    client = _LadderClient(
        ['<REQUEST kind="type_def" arg="Buf"/>', "<BODY>\n42\n</BODY>"])
    body, _ = _ladder(client, tmp_path, 2)
    assert body == "42"
    assert len(client.calls) == 2
    assert client.calls[1]["think"] is None            # still the DEFAULT rung
    assert "struct Buf;" in client.calls[1]["prompt"]  # answer in the re-ask


def test_ladder_budget_falls_through_to_next_rung(tmp_path: Path) -> None:
    client = _LadderClient(['<REQUEST kind="type_def" arg="Buf"/>'])
    body, _ = _ladder(client, tmp_path, 1)
    assert body is None
    # 1 answered turn on rung 1, then ONE refusal notice turn on the same
    # rung, then rungs 2 and 3: 5 calls. The notice is what makes rungs 2
    # and 3 worth spending — without it the model just asks again.
    assert len(client.calls) == 5
    assert "no context requests left" in client.calls[2]["prompt"]
    assert client.calls[3]["think"] is False           # ladder did move on


def test_ladder_notice_is_issued_once_across_rungs(tmp_path: Path) -> None:
    """The conversation's request budget and its notice both persist across
    rungs, so a model that keeps asking cannot re-buy an extra ask on every
    rung. The notice sits once in the transcript every later call carries."""
    client = _LadderClient(['<REQUEST kind="type_def" arg="Buf"/>'])
    _ladder(client, tmp_path, 0)
    assert client.calls[-1]["transcript"].count("no context requests left") == 1
    assert len(client.calls) == 4      # 3 rungs + the single notice turn


def test_ladder_without_answerer_unchanged(tmp_path: Path) -> None:
    client = _LadderClient([""])
    body, _ = _ladder(client, tmp_path, None)
    assert body is None and len(client.calls) == 3


def test_ladder_never_splices_request_text_as_body(tmp_path: Path) -> None:
    """With the budget spent, a request-only reply must NOT raw-fallback into
    a 'body' (extract_body_response would return the tag text verbatim)."""
    client = _LadderClient(['<REQUEST kind="type_def" arg="Buf"/>'])
    body, _ = _ladder(client, tmp_path, 0)
    assert body is None


# --- windows, continuation, repeats -------------------------------------------
#
# The shape these pin (run `603120-24aabfc5`, 2026-09-16): a session asked twice
# for a file longer than the FILE cap and was served the same head both times;
# what it needed sat past the cut and the second copy spent the transcript
# bound. The file below is invented; only its length matters.


from pipeline_core.application.reconstruct import pull_resolver  # noqa: E402
from pipeline_core.domain.context_pull import LineWindow, window_lines  # noqa: E402


def _long_file(tmp_path: Path, lines: int = 3_000) -> Path:
    (tmp_path / "src").mkdir(exist_ok=True)
    body = "".join(f"// filler line {i:05d} padding\n" for i in range(1, lines))
    (tmp_path / "src/long.rs").write_text(body + "pub fn tail_marker() {}\n")
    return tmp_path / "src/long.rs"


def test_window_is_line_aligned_and_addresses_the_rest() -> None:
    w = window_lines("aaaa\nbbbb\ncccc\n", 1, 11)
    assert w == LineWindow("aaaa\nbbbb\n", 1, 2, 3)
    assert w.truncated
    assert window_lines("aaaa\nbbbb\ncccc\n", 3, 11) == LineWindow("cccc\n", 3, 3, 3)
    assert window_lines("aaaa\n", 2, 11) is None


def test_window_cuts_inside_a_line_longer_than_the_cap() -> None:
    w = window_lines("x" * 20, 1, 8)
    assert w is not None and w.text == "x" * 8 and w.truncated and w.cut_inside


def test_from_line_parses_and_round_trips() -> None:
    p = extract_context_pull('<REQUEST kind="file" arg="src/a.rs" from_line="812"/>')
    assert p == ContextPull(PullKind.FILE, "src/a.rs", 812)
    assert extract_context_pull(p.tag()) == p
    bad = extract_context_pull('<REQUEST kind="file" arg="src/a.rs" from_line="x"/>')
    assert bad == ContextPull(PullKind.FILE, "src/a.rs", 1)
    assert ContextPull(PullKind.FILE, "src/a.rs").tag() == \
        '<REQUEST kind="file" arg="src/a.rs"/>'


def test_from_line_past_the_end_is_unavailable(tmp_path: Path) -> None:
    _long_file(tmp_path, lines=10)
    ans = resolve_pull(ContextPull(PullKind.FILE, "src/long.rs", 99), _sources(tmp_path))
    assert not ans.ok and "past the end" in ans.payload


def _session_like(tmp_path: Path, replies: list[str], max_pulls: int = 5):
    """What a slice session does with the answerer: every follow-up lands in
    the one growing transcript."""
    answer = make_request_answerer(
        _sources(tmp_path), log=lambda _m: None, emit=lambda *_a, **_k: None,
        max_pulls=max_pulls)
    transcript = ""
    for r in replies:
        add = answer(r, conversation=transcript)
        if add:
            transcript += add
    return transcript


def test_a_cut_file_names_its_continuation_and_the_tail_is_reachable(
        tmp_path: Path) -> None:
    _long_file(tmp_path)
    answer = make_request_answerer(
        _sources(tmp_path), log=lambda _m: None, emit=lambda *_a, **_k: None,
        max_pulls=10)
    reply = '<REQUEST kind="file" arg="src/long.rs"/>'
    seen = ""
    starts: list[int] = []
    for _ in range(10):
        add = answer(reply) or ""
        seen += add
        marker = "To read them, reply "
        if marker not in add:
            break
        cont = extract_context_pull(add[add.index(marker):])
        assert cont is not None
        starts.append(cont.from_line)
        reply = cont.tag()
    assert "tail_marker" in seen
    assert starts and starts == sorted(starts) and starts[0] > 1
    # Every line is served exactly once across the windows.
    assert seen.count("filler line 01500 ") == 1


def test_negative_control_head_cut_serves_the_same_head_and_never_the_tail(
        tmp_path: Path, monkeypatch: Any) -> None:
    """The pre-fix cap: `text[:cap]` whatever the request asked for."""
    _long_file(tmp_path)

    def head_cut(text: str, _from_line: int, cap: int) -> LineWindow:
        return LineWindow(text[:cap], 1, 1, 2)

    monkeypatch.setattr(pull_resolver, "window_lines", head_cut)
    out = _session_like(tmp_path, [
        '<REQUEST kind="file" arg="src/long.rs"/>',
        '<REQUEST kind="file" arg="src/long.rs" from_line="2000"/>'])
    assert "tail_marker" not in out


def test_an_identical_repeat_is_not_re_sent(tmp_path: Path) -> None:
    _long_file(tmp_path)
    req = '<REQUEST kind="file" arg="src/long.rs"/>'
    once = _session_like(tmp_path, [req])
    twice = _session_like(tmp_path, [req, req])
    assert twice.count("filler line 00001") == 1
    assert "not repeated" in twice
    assert len(twice) < len(once) + 1_000


def test_a_changed_artifact_is_re_sent(tmp_path: Path) -> None:
    """The repeat is keyed on the bytes served, not on the request: a file the
    session rewrote between two requests is new information."""
    f = _long_file(tmp_path, lines=5)
    answer = make_request_answerer(
        _sources(tmp_path), log=lambda _m: None, emit=lambda *_a, **_k: None,
        max_pulls=5)
    req = '<REQUEST kind="file" arg="src/long.rs"/>'
    a1 = answer(req) or ""
    f.write_text(f.read_text() + "pub fn added_later() {}\n")
    a2 = answer(req, conversation=a1) or ""
    assert "added_later" in a2 and "not repeated" not in a2 and a1 != a2


def test_a_repeat_still_spends_the_budget(tmp_path: Path) -> None:
    out, asked = _loop(['<REQUEST kind="full_error" arg=""/>'] * 3 + [_BODY],
                       tmp_path, max_pulls=2)
    assert out == _BODY
    assert "no context requests left" in asked[-1]


# ── a whole-file request for a file the conversation already holds whole ─────

from pipeline_core.domain.prompt_holdings import FileView, file_block  # noqa: E402

_HELD = "pub struct A;\nimpl A {\n    pub fn get(&self) -> u8 { unimplemented!() }\n}\n"


def _held_workspace(tmp_path: Path, disk: str = _HELD) -> PullSources:
    (tmp_path / "src").mkdir(exist_ok=True)
    (tmp_path / "src/a.rs").write_text(disk)
    return _sources(tmp_path)


def test_a_request_for_a_file_held_whole_is_answered_by_pointer(tmp_path: Path) -> None:
    """2026-09-24 census: half the per-fn requests asked for what the prompt
    already rendered. The answer names where it is and outlines it; the file
    is not sent again. The budget is still spent — the cap is on round-trips."""
    events: list[dict[str, Any]] = []
    answer = make_request_answerer(
        _held_workspace(tmp_path), log=lambda _m: None,
        emit=lambda _e, data: events.append(data), max_pulls=2)
    conv = file_block("src/a.rs", _HELD, view=FileView.WHOLE)
    out = answer('<REQUEST kind="file" arg="src/a.rs"/>', conversation=conv)
    assert out is not None
    # The outline names the items; no body is re-sent.
    assert "It is not sent again." in out and "unimplemented!()" not in out
    assert "L3: pub fn get(&self) -> u8" in out
    assert events[-1]["held_whole"] is True and events[-1]["remaining"] == 1


def test_a_held_file_rewritten_since_carries_the_new_body(tmp_path: Path) -> None:
    disk = _HELD.replace("unimplemented!()", "7")
    answer = make_request_answerer(_held_workspace(tmp_path, disk),
                                   log=lambda _m: None, emit=lambda *_a, **_k: None)
    out = answer('<REQUEST kind="file" arg="src/a.rs"/>',
                 conversation=file_block("src/a.rs", _HELD, view=FileView.WHOLE))
    assert out is not None and "pub fn get(&self) -> u8 { 7 }" in out


def test_a_part_a_window_or_no_conversation_is_answered_with_the_file(
        tmp_path: Path) -> None:
    """The negative controls: a part is not the file, a from_line asks for
    lines on purpose, and with no conversation (the pre-change call) the file
    is sent as before."""
    src = _held_workspace(tmp_path)
    part = file_block("src/a.rs", "pub struct A;", view=FileView.PART)
    whole = file_block("src/a.rs", _HELD, view=FileView.WHOLE)
    for reply, conv in (('<REQUEST kind="file" arg="src/a.rs"/>', part),
                        ('<REQUEST kind="file" arg="src/a.rs" from_line="2"/>', whole),
                        ('<REQUEST kind="file" arg="src/a.rs"/>', "")):
        answer = make_request_answerer(src, log=lambda _m: None,
                                       emit=lambda *_a, **_k: None)
        out = answer(reply, conversation=conv)
        assert out is not None and "It is not sent again." not in out
        assert "impl A {" in out


def test_a_conversation_points_at_a_file_its_own_opening_holds(tmp_path: Path) -> None:
    sent: list[str] = []
    replies = iter(['<REQUEST kind="file" arg="src/a.rs"/>', '<FN name="get">1</FN>'])

    def _turns(messages, **_k) -> str:
        sent.append("\n\n".join(str(m["content"]) for m in messages))
        return next(replies)

    conv = UnitConversation(
        ConversationPorts(ask_turns=_turns, log=lambda _m: None),
        label="u", route="t",
        requests=make_request_answerer(
            _held_workspace(tmp_path), log=lambda _m: None,
            emit=lambda *_a, **_k: None))
    out = conv.open(file_block("src/a.rs", _HELD, view=FileView.WHOLE) + "\ntask",
                    has_deliverable=lambda r: "<FN" in r)
    assert out.startswith("<FN")
    assert "It is not sent again." in sent[1]
    assert sent[1].count("unimplemented!()") == 1


def test_a_type_the_prompt_already_defines_is_answered_with_where_it_is(
        tmp_path: Path) -> None:
    conv = "<TYPES_IN_SCOPE>\nstruct Buf\n  fields:\n</TYPES_IN_SCOPE>"
    answer = make_request_answerer(
        _sources(tmp_path, type_def=lambda n: f"struct {n};"),
        log=lambda _m: None, emit=lambda *_a, **_k: None)
    out = answer('<REQUEST kind="type_def" arg="Buf"/>', conversation=conv)
    assert out is not None and "already defined in TYPES_IN_SCOPE above" in out
    assert "struct Buf;" in out
    other = make_request_answerer(
        _sources(tmp_path, type_def=lambda n: f"struct {n};"),
        log=lambda _m: None, emit=lambda *_a, **_k: None)
    plain = other('<REQUEST kind="type_def" arg="Other"/>', conversation=conv)
    assert plain is not None and "already defined" not in plain
