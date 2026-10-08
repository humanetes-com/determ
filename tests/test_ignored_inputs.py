"""A body that ignores its declared inputs is a decline unless a test vouches.

Fixtures are the two replies of run `486447-5207101c` (2026-10-07, file route,
`src/ensure.rs`): `render_uses_operands_87152d0d.txt` formats both operands
(it failed to compile on `format!`), `render_discard_f200fbd6.txt` is the
retry that dropped them and was kept on no evidence. The signature is the
skeleton's own.
"""
from __future__ import annotations

from pathlib import Path

from pipeline_core.domain.ignored_inputs import (
    declined_by_ignored_inputs,
    ignored_inputs,
    ignored_inputs_feedback,
    input_names,
    vouched_bodies,
)
from pipeline_core.domain.llm_output import extract_fn_bodies

_FIX = Path(__file__).parent / "fixtures" / "replies"
_RENDER_SIG = "fn render(msg: &'static str, lhs: &dyn Debug, rhs: &dyn Debug) -> Error"


def _body(name: str) -> str:
    parsed = extract_fn_bodies((_FIX / name).read_text(encoding="utf-8"))
    assert len(parsed) == 1
    return str(parsed[0]["body"])


def test_the_kept_discard_is_ignored_and_the_first_reply_is_not() -> None:
    assert ignored_inputs(_RENDER_SIG, _body("render_discard_f200fbd6.txt")) == (
        "lhs", "rhs")
    assert ignored_inputs(
        _RENDER_SIG, _body("render_uses_operands_87152d0d.txt")) == ()


def test_unvouched_discard_declines_and_a_vouched_one_does_not() -> None:
    bodies = [("render", _RENDER_SIG, _body("render_discard_f200fbd6.txt"))]
    assert declined_by_ignored_inputs(bodies, vouched=frozenset()) == {
        "render": ("lhs", "rhs")}
    # serde's IgnoredAny shape: ignoring the input IS the body, and a test
    # of that body turning green is what says so.
    assert declined_by_ignored_inputs(bodies, vouched={"render"}) == {}
    # A vouch is per body: another name's vouch does not cover `render`.
    assert declined_by_ignored_inputs(bodies, vouched={"fmt"}) == {
        "render": ("lhs", "rhs")}


def test_a_sibling_turning_green_in_one_file_reply_does_not_vouch_for_a_discard() -> None:
    """The file route's decision. One reply lands
    `render`'s `let _ = (lhs, rhs)` beside `fmt`; the keep's scoped run
    turned `fmt`'s own test green and left `render`'s red. The old flag
    (anything newly passing) kept the discard. Control: the same discard
    when a test attributed to `render` turned green is kept."""
    from pipeline_core.domain.test_report import U2Verdict, turned_green
    from pipeline_core.domain.test_results import BinaryResult, TestStateReading

    landed = [("render", _RENDER_SIG, _body("render_discard_f200fbd6.txt")),
              ("fmt", "fn fmt(&self, f: &mut Formatter) -> fmt::Result",
               'write!(f, "{}", self.msg)')]
    attributed = {"render": {"tests/test_ensure.rs::test_render"},
                  "fmt": {"tests/test_fmt.rs::test_display"}}
    baseline = TestStateReading(binaries=(BinaryResult(
        label="t", completed=True, passed=frozenset({"test_other"}),
        failed=frozenset({"test_render", "test_display"}),
        pass_count=1, fail_count=2),))
    sibling = U2Verdict.accept(frozenset({"test_display", "test_other"}))
    green = turned_green(baseline, sibling)
    assert green == {"test_display"}
    assert declined_by_ignored_inputs(
        landed, vouched=vouched_bodies(attributed, green)) == {
        "render": ("lhs", "rhs")}

    own = U2Verdict.accept(frozenset({"test_render"}))
    assert declined_by_ignored_inputs(
        landed, vouched=vouched_bodies(
            attributed, turned_green(baseline, own))) == {}
    # A test green before the keep is observed passing after any body: it
    # turned nothing green, and vouches for nobody.
    already = U2Verdict.accept(frozenset({"test_other"}))
    assert turned_green(baseline, already) == frozenset()
    assert vouched_bodies({"render": {"test_other"}},
                          turned_green(baseline, already)) == frozenset()
    # No baseline, or no scoped run: nothing was observed turning green.
    assert turned_green(None, own) == frozenset()
    assert turned_green(baseline, None) == frozenset()


def test_two_bodies_of_one_name_are_vouched_by_their_own_container() -> None:
    """Two `fmt` bodies in one keep. The sweep's convicting map names both
    containers, and only the Debug body's test turned green. That body is
    kept; the Display discard is declined. A bare-name vouch does not cover
    either distinguished body. When the map does not split the containers,
    the locator's tests stand for both, and one green test vouches both.
    A keep of one container stays keyed by the bare name."""
    from pipeline_core.domain.attribution import FailureFrontier
    from pipeline_core.domain.ignored_inputs import (
        body_identity,
        is_declined,
        vouched_for_keep,
    )
    from pipeline_core.domain.models import FnLocator, FnSite

    sig = "fn fmt(&self, f: &mut Formatter) -> fmt::Result"
    discard = "let _ = f;\nOk(())"
    debug = "impl fmt::Debug for Error"
    display = "impl fmt::Display for Error"
    bodies = [("fmt", sig, discard, debug), ("fmt", sig, discard, display)]
    loc = FnLocator("src/error.rs", "fmt")
    split = FailureFrontier.build(
        {loc: {"test_debug", "test_display"}},
        {FnSite("src/error.rs", "fmt", debug): {"test_debug"},
         FnSite("src/error.rs", "fmt", display): {"test_display"}},
    )
    attributed = {
        ("fmt", debug): split.tests_for_body("src/error.rs", "fmt", debug),
        ("fmt", display): split.tests_for_body("src/error.rs", "fmt", display),
    }
    assert attributed[("fmt", debug)] == {"test_debug"}
    assert attributed[("fmt", display)] == {"test_display"}
    vouched = vouched_for_keep(attributed, {"test_debug"})
    declined = declined_by_ignored_inputs(bodies, vouched=vouched)
    assert not is_declined("fmt", debug, declined)
    assert is_declined("fmt", display, declined)
    assert "fmt" not in declined
    bare = declined_by_ignored_inputs(bodies, vouched={"fmt"})
    assert set(bare) == {body_identity("fmt", debug),
                         body_identity("fmt", display)}

    whole = FailureFrontier.of_direct({loc: {"test_debug"}})
    standing = {
        ("fmt", c): whole.tests_for_body("src/error.rs", "fmt", c)
        for c in (debug, display)}
    assert standing[("fmt", debug)] == standing[("fmt", display)] == {"test_debug"}
    assert declined_by_ignored_inputs(
        bodies, vouched=vouched_for_keep(standing, {"test_debug"})) == {}

    assert vouched_for_keep(
        {("render", "impl Foo"): {"test_render"}}, {"test_render"}) == {"render"}
    assert declined_by_ignored_inputs(
        [("render", _RENDER_SIG, _body("render_discard_f200fbd6.txt"),
          "impl Foo")],
        vouched={"render"}) == {}


def test_reads_are_reads() -> None:
    sig = "fn f(buf: &mut String, lhs: &dyn Debug) -> fmt::Result"
    assert ignored_inputs(sig, 'let _ = write!(buf, "{:?}", lhs);\nOk(())') == ()
    assert ignored_inputs(sig, 'write!(buf, "{lhs:?}")') == ()
    assert ignored_inputs(sig, "let _ = &*lhs; Ok(())") == ("buf", "lhs")
    assert ignored_inputs(sig, "drop(buf); lhs.fmt_debug()") == ("buf",)
    # A word in a message or a comment is not a read.
    assert ignored_inputs(sig, '// lhs\nbuf.push_str("lhs"); Ok(())') == ("lhs",)
    # A field of the same name on another value is not a read of the input.
    assert ignored_inputs(sig, "buf.push_str(self.lhs); Ok(())") == ("lhs",)


def test_declared_names_only() -> None:
    assert input_names(_RENDER_SIG) == ("msg", "lhs", "rhs")
    assert input_names("fn m(&mut self, _unused: u8, mut n: usize) -> usize") == ("n",)
    assert input_names("fn g<T: Into<u8>>((a, b): (T, T)) -> u8") == ("a", "b")
    assert ignored_inputs("fn h(&self) -> u8", "0") == ()


def test_feedback_names_the_inputs() -> None:
    text = ignored_inputs_feedback("render", ("lhs", "rhs"))
    assert text.startswith("error: your body for `render` was not kept")
    assert "`lhs`, `rhs`" in text


def test_the_lexer_reads_comments_raw_strings_and_chars_as_not_code() -> None:
    sig = "fn f(a: u8, b: &str) -> String"
    # A block comment, a raw string and a char literal mention `a`/`b` and
    # read nothing; the capture inside the raw string is a read of `b`.
    body = ('/* a */ let s = r#"{b} and a"#; let c = \'a\'; let d = \'\\n\';\n'
            "s.to_string()")
    assert ignored_inputs(sig, body) == ("a",)
    # An unterminated block comment ends the code.
    assert ignored_inputs(sig, "b.len(); /* a") == ("a",)


def test_discard_forms_and_their_limits() -> None:
    sig = "fn g(a: u8, b: u8) -> u8"
    # `_ = a;` and a discard of an expression that computes from the inputs.
    assert ignored_inputs(sig, "_ = a; b") == ("a",)
    assert ignored_inputs(sig, "let _ = a + b; 0") == ()
    # `drop(` with no close and `let _ =` with no `;` are not discards, so
    # `a` there is a read; `b` is absent from both.
    assert ignored_inputs(sig, "drop(a") == ("b",)
    assert ignored_inputs(sig, "let _ = a") == ("b",)
    # A discard naming a non-input is not a discard of the inputs.
    assert ignored_inputs(sig, "let _ = (a, other); b") == ()


def test_malformed_signatures_declare_nothing() -> None:
    assert input_names("struct S") == ()
    assert input_names("fn f<T") == ()
    assert input_names("fn f") == ()
    assert input_names("fn f(a: u8") == ()
