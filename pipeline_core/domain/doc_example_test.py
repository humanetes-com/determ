"""A doc example as an integration test (pure domain).

A crate's doc examples are tests: `cargo test` runs each one. They are also
the one kind of test the reconstruction loop cannot work on. They live in doc
comments, cargo runs them through a separate target that cannot be combined
with the cluster sweep's targets (`can't mix --doc with other target
selecting options`), they have no `#[test]` record, and nothing attributes a
failing one to a body. Run `292191-d60a5308` (2026-10-02): the one assertion
that a boxed error still provides its backtrace is a doc example; the
reconstruction lost that behaviour, 401 tests passed, and no route could have
been aimed at it.

A doc example is an integration test by construction — rustdoc compiles it as
its own crate against the public API. So before strip, while the original is
on disk to verify against, each runnable example is rendered as a file under
`tests/`, run on the original, and kept only if it passes
(`application.doc_example_tests`). From then on it is an ordinary oracle test:
in the cluster sweep, counted, attributed by its text, scoped and isolated
like any other.

The rendering follows rustdoc where the rule is simple and does not try to be
rustdoc where it is not: an example it renders wrongly fails on the original
and is dropped, which is the same outcome as an example rustdoc marks
`ignore`. What it does:

- a fence is an example when rustdoc would run it: Rust, and none of
  `ignore`, `no_run`, `compile_fail` (`should_panic` becomes the test's own
  attribute);
- a line hidden with `# ` runs, so its marker is removed;
- crate-level inner attributes (`#![feature(…)]`) move to the top of the file;
- an example that defines `fn main` keeps its items at file scope and the
  test calls `main()`; otherwise the code is the body of a function;
- either way the outcome is checked through one trait, so an example that
  ends in `Ok::<(), E>(())` fails the test when it returns `Err`.
"""
from __future__ import annotations

from dataclasses import dataclass

from pipeline_core.domain.generation import PIPELINE_GEN_TEST_ORIGIN

# rustdoc fence attributes. A tag outside this set is a language, and a block
# in another language is documentation.
_RUN_ATTRS = frozenset({"rust", "should_panic", "allow_fail", "standalone",
                        "standalone_crate"})
_SKIP_ATTRS = frozenset({"ignore", "no_run", "compile_fail"})

_OUTCOME = """trait DocOutcome {
    fn check(self);
}
impl DocOutcome for () {
    fn check(self) {}
}
impl<T, E: core::fmt::Debug> DocOutcome for Result<T, E> {
    fn check(self) {
        self.unwrap();
    }
}
"""


@dataclass(frozen=True, slots=True)
class DocExample:
    """One runnable fenced block of an item's doc text."""

    code: str                 # hidden-line markers removed
    should_panic: bool = False


def _fence_tags(info: str) -> set[str]:
    return {t.strip().lower() for t in info.replace(",", " ").split() if t.strip()}


def doc_examples(doc: str) -> list[DocExample]:
    """The examples of one item's doc text that rustdoc would run, in order.

    Each item's doc is read on its own, as rustdoc reads it, so a fence left
    open in one item cannot swallow the next. A closing fence is never taken
    for an opening one (`doc_tests._count_rust_fences` records what that
    mistake cost)."""
    out: list[DocExample] = []
    open_fence = False
    keep = False
    should_panic = False
    lines: list[str] = []
    for raw in doc.splitlines():
        body = raw.strip()
        if body.startswith("```"):
            if open_fence:
                if keep and any(ln.strip() for ln in lines):
                    out.append(DocExample("\n".join(lines).rstrip() + "\n",
                                          should_panic))
                open_fence, keep, lines = False, False, []
                continue
            open_fence = True
            tags = _fence_tags(body[3:])
            edition = {t for t in tags if t.startswith("edition")}
            keep = not (tags & _SKIP_ATTRS) and (tags - edition) <= _RUN_ATTRS
            should_panic = "should_panic" in tags
            continue
        if open_fence and keep:
            # One leading space is the doc comment's own; a `# ` line is
            # hidden from the rendered docs and still compiled.
            line = raw[1:] if raw.startswith(" ") else raw
            stripped = line.lstrip()
            if stripped == "#":
                line = ""
            elif stripped.startswith("# "):
                line = line[:len(line) - len(stripped)] + stripped[2:]
            elif stripped.startswith("##"):
                line = line[:len(line) - len(stripped)] + stripped[1:]
            lines.append(line)
    return out


def _is_inner_attr(line: str) -> bool:
    return line.lstrip().startswith("#![")


def _defines_main(code: str) -> bool:
    return any(ln.lstrip().startswith(("fn main(", "pub fn main(", "async fn main("))
               for ln in code.splitlines())


def render_doc_example_test(example: DocExample, *, fn_name: str,
                            source: str) -> str:
    """The integration-test file for one example. `source` names where the
    example was documented (file and item line), for the reader of the file.

    The first line is the origin marker every pipeline-written test carries;
    a world gate, when the file gets one, goes above it."""
    lines = example.code.splitlines()
    inner = [ln.strip() for ln in lines if _is_inner_attr(ln)]
    rest = "\n".join(ln for ln in lines if not _is_inner_attr(ln)).strip("\n")
    attrs = "#[test]\n" + ("#[should_panic]\n" if example.should_panic else "")
    head = (f"// {PIPELINE_GEN_TEST_ORIGIN}\n"
            f"// doc example of {source}, run as a test\n"
            "#![allow(clippy::all, dead_code, unused, unused_imports, unused_mut)]\n"
            + "".join(f"{a}\n" for a in inner))
    if _defines_main(rest):
        return (f"{head}\n{rest}\n\n{_OUTCOME}\n{attrs}fn {fn_name}() {{\n"
                "    DocOutcome::check(main());\n}\n")
    body = "\n".join(("    " + ln) if ln.strip() else "" for ln in rest.splitlines())
    return (f"{head}\n{_OUTCOME}\n"
            f"fn doc_example_body() -> impl DocOutcome {{\n{body}\n}}\n\n"
            f"{attrs}fn {fn_name}() {{\n"
            "    DocOutcome::check(doc_example_body());\n}\n")


def doc_example_stem(src_file: str) -> str:
    """`src/error.rs` → `doc_error`: the stem the examples of one source file
    are numbered under (`tests/doc_error_gen_<N>.rs`)."""
    name = src_file.rsplit("/", 1)[-1]
    return "doc_" + (name[:-3] if name.endswith(".rs") else name)
