"""Pins for M6 chunked layout authoring: uid identity for duplicate-named
items, occurrence-ordered multi-splice, the chunk plan, and the chunked
authoring pass end-to-end."""
from __future__ import annotations

from pipeline_core.application.author_layout import AuthorLayoutDeps, author_layout
from pipeline_core.domain.authored_layout import (
    component_uids,
    layout_coverage,
    parse_authored_layout,
)
from pipeline_core.domain.layout_chunking import plan_author_chunks
from pipeline_core.domain.private_surface import (
    PrivateSurface,
    PrivateSurfaceItem,
    SurfaceItemKind,
)
from pipeline_core.domain.scaffold_rewrite import replace_definitions


def _fn(name, file, definition, refs=frozenset()):
    return PrivateSurfaceItem(kind=SurfaceItemKind.FN_SIG, name=name,
                              file=file, definition=definition,
                              references=frozenset(refs))


def _ty(name, file, definition, refs=frozenset()):
    return PrivateSurfaceItem(kind=SurfaceItemKind.STRUCT, name=name,
                              file=file, definition=definition,
                              references=frozenset(refs))


# --- component_uids -----------------------------------------------------

def test_uids_bare_when_unique_suffixed_when_duplicated():
    comp = (_ty("Inner", "a.rs", "struct Inner;"),
            _fn("as_ref", "a.rs", "fn as_ref(&self) -> &A"),
            _fn("as_ref", "a.rs", "fn as_ref(&self) -> &B"),
            _fn("erase", "a.rs", "fn erase(&self)"))
    assert component_uids(comp) == ("Inner", "as_ref.1", "as_ref.2", "erase")


def test_coverage_requires_every_duplicate_slot():
    comp = (_fn("as_ref", "a.rs", "fn as_ref(&self) -> &A"),
            _fn("as_ref", "a.rs", "fn as_ref(&self) -> &B"))
    # one bare-name block covers NEITHER uid slot
    layout = parse_authored_layout(
        '<LAYOUT_ITEM name="as_ref" kind="fn">fn as_ref(&self) -> &A '
        '{ unimplemented!() }</LAYOUT_ITEM>')
    missing, extra = layout_coverage(comp, layout)
    assert missing == frozenset({"as_ref.1", "as_ref.2"})
    assert extra == frozenset({"as_ref"})


# --- replace_definitions -------------------------------------------------

def test_identical_duplicate_defs_bind_successive_occurrences():
    # the context.rs case: the SAME frozen sig on two impls. Sequential
    # single replaces re-match inside the first splice and nest garbage.
    text = ("impl A for X { fn source(&self) -> Opt; }\n"
            "impl A for Y { fn source(&self) -> Opt; }\n")
    res = replace_definitions(text, [
        ("fn source(&self) -> Opt", "fn source(&self) -> Opt { body_x() }"),
        ("fn source(&self) -> Opt", "fn source(&self) -> Opt { body_y() }"),
    ])
    assert res.replaced
    assert "body_x" in res.text.splitlines()[0]
    assert "body_y" in res.text.splitlines()[1]


def test_missing_occurrence_rejects_whole_batch():
    text = "impl A for X { fn source(&self) -> Opt; }\n"
    res = replace_definitions(text, [
        ("fn source(&self) -> Opt", "one"),
        ("fn source(&self) -> Opt", "two"),  # no 2nd occurrence
    ])
    assert not res.replaced
    assert res.text == text


# --- plan_author_chunks --------------------------------------------------

def _big_comp(n_types=3, n_fns=25):
    types = tuple(_ty(f"T{i}", "a.rs", f"struct T{i};") for i in range(n_types))
    fns = tuple(_fn(f"f{i}", "a.rs", f"fn f{i}()") for i in range(n_fns))
    return types + fns


def test_small_component_stays_single_shot():
    comp = _big_comp(2, 5)
    assert plan_author_chunks(comp, single_shot_max=14, fns_per_chunk=10) == (
        tuple(range(7)),)


def test_big_component_chunks_types_first_then_fn_groups():
    comp = _big_comp(3, 25)
    chunks = plan_author_chunks(comp, single_shot_max=14, fns_per_chunk=10)
    assert chunks[0] == (0, 1, 2)                      # all types, one chunk
    assert chunks[1] == tuple(range(3, 13))            # 10 fns
    assert chunks[2] == tuple(range(13, 23))
    assert chunks[3] == tuple(range(23, 28))
    assert sorted(i for c in chunks for i in c) == list(range(28))


# --- chunked authoring end-to-end ---------------------------------------

def _surface_from(items):
    return PrivateSurface(cfg_cluster="stable", replaceable=tuple(items),
                          frozen_public=frozenset())


class _ChunkDeps:
    """Fake model that answers each chunk prompt with exactly the uids it
    lists (parsed back out of ITEMS TO AUTHOR), recording every prompt."""

    def __init__(self, files):
        self.files = dict(files)
        self.prompts: list[str] = []
        self.logs: list[str] = []

    def _reply_for(self, prompt):
        blocks = []
        listing = prompt.split("=== ITEMS TO AUTHOR ===")[1]
        for line in listing.splitlines():
            if not line.startswith("- "):
                continue
            uid = line[2:].split(" ")[0]
            name = uid.split(".")[0]
            kind = "struct" if "(struct)" in line else "fn"
            body = (f"struct {name} {{ marker_{uid.replace('.', '_')}: u8 }}"
                    if kind == "struct" else
                    f"fn {name}(&self) -> u8 {{ unimplemented!() }}")
            blocks.append(f'<LAYOUT_ITEM name="{uid}" kind="{kind}">{body}'
                          f"</LAYOUT_ITEM>")
        return "\n".join(blocks)

    def deps(self, **over):
        def run_model(p):
            self.prompts.append(p)
            return self._reply_for(p)
        kw = {"run_model": run_model,
              "read_skeleton": lambda rel: self.files.get(rel),
              "write_skeleton": lambda rel, t: self.files.__setitem__(rel, t),
              "compile_check": lambda: (True, ""),
              "log": self.logs.append, "max_attempts": 1,
              "single_shot_max": 3, "fns_per_chunk": 2}
        kw.update(over)
        return AuthorLayoutDeps(**kw)


def _items_and_file():
    items = (_ty("Inner", "src/x.rs", "struct Inner { v: u8 }"),
             _ty("Holder", "src/x.rs", "struct Holder { v: u8 }", {"Inner"}),
             _fn("get", "src/x.rs", "fn get(&self) -> u8", {"Inner"}),
             _fn("put", "src/x.rs", "fn put(&self) -> u8", {"Holder"}),
             _fn("erase", "src/x.rs", "fn erase(&self) -> u8", {"Holder"}))
    file_text = ("struct Inner { v: u8 }\nstruct Holder { v: u8 }\n"
                 "fn get(&self) -> u8;\nfn put(&self) -> u8;\n"
                 "fn erase(&self) -> u8;\n")
    return items, {"src/x.rs": file_text}


def test_chunked_pass_carries_scheme_and_authors_all():
    items, files = _items_and_file()
    d = _ChunkDeps(files)
    res = author_layout(_surface_from(items), module_name="m",
                        public_surface="pub struct P;",
                        select_pins=lambda c: [], symbol_inventory="",
                        deps=d.deps())
    assert res.all_authored
    # 3 chunks: [types], [get, put], [erase]
    assert len(d.prompts) == 3
    # later chunks carry the authored scheme, marked fixed
    assert "ALREADY AUTHORED THIS PASS" in d.prompts[1]
    assert "marker_Inner" in d.prompts[1]
    # every definition landed
    assert "marker_Holder" in d.files["src/x.rs"]
    assert "fn erase(&self) -> u8 { unimplemented!() }" in d.files["src/x.rs"]


def test_chunk_omission_retries_only_that_chunk():
    items, files = _items_and_file()
    d = _ChunkDeps(files)
    real = d._reply_for
    dropped = {"done": False}

    def flaky(prompt):
        reply = real(prompt)
        if "- put " in prompt and not dropped["done"]:
            dropped["done"] = True
            return reply.replace('<LAYOUT_ITEM name="put"',
                                 '<LAYOUT_ITEM name="skipped"', 1)
        return reply

    d._reply_for = flaky
    res = author_layout(_surface_from(items), module_name="m",
                        public_surface="pub struct P;",
                        select_pins=lambda c: [], symbol_inventory="",
                        deps=d.deps())
    assert res.all_authored
    assert len(d.prompts) == 4  # one extra call for the flaky chunk only
    assert any("missing authored items: put" in p for p in d.prompts)


def test_chunk_exhausted_coverage_fails_component_without_writes():
    items, files = _items_and_file()
    d = _ChunkDeps(files)
    real = d._reply_for

    def never_puts(prompt):
        return real(prompt).replace('<LAYOUT_ITEM name="put"',
                                    '<LAYOUT_ITEM name="skipped"')

    d._reply_for = never_puts
    res = author_layout(_surface_from(items), module_name="m",
                        public_surface="pub struct P;",
                        select_pins=lambda c: [], symbol_inventory="",
                        deps=d.deps(chunk_coverage_retries=1))
    assert not res.all_authored
    assert d.files["src/x.rs"].count("marker_") == 0  # nothing spliced


def test_duplicate_sigs_route_to_distinct_slots_via_uids():
    items = (_fn("as_ref", "src/e.rs", "fn as_ref(&self) -> &A"),
             _fn("as_ref", "src/e.rs", "fn as_ref(&self) -> &B"))
    files = {"src/e.rs": "fn as_ref(&self) -> &A;\nfn as_ref(&self) -> &B;\n"}
    d = _ChunkDeps(files)

    def per_uid(prompt):
        out = []
        if "as_ref.1" in prompt.split("=== ITEMS TO AUTHOR ===")[1]:
            out.append('<LAYOUT_ITEM name="as_ref.1" kind="fn">'
                       "fn as_ref(&self) -> &A { imp_a() }</LAYOUT_ITEM>")
        if "as_ref.2" in prompt.split("=== ITEMS TO AUTHOR ===")[1]:
            out.append('<LAYOUT_ITEM name="as_ref.2" kind="fn">'
                       "fn as_ref(&self) -> &B { imp_b() }</LAYOUT_ITEM>")
        return "\n".join(out)

    d._reply_for = per_uid
    res = author_layout(_surface_from(items), module_name="m",
                        public_surface="pub struct P;",
                        select_pins=lambda c: [], symbol_inventory="",
                        deps=d.deps())
    assert res.all_authored
    lines = d.files["src/e.rs"].splitlines()
    assert "imp_a" in lines[0] and "imp_b" in lines[1]


def test_prompt_lists_uids_with_current_sig():
    items = (_fn("as_ref", "src/e.rs", "fn as_ref(&self) -> &A"),
             _fn("as_ref", "src/e.rs", "fn as_ref(&self) -> &B"))
    files = {"src/e.rs": "fn as_ref(&self) -> &A;\nfn as_ref(&self) -> &B;\n"}
    d = _ChunkDeps(files)
    author_layout(_surface_from(items), module_name="m",
                  public_surface="pub struct P;",
                  select_pins=lambda c: [], symbol_inventory="",
                  deps=d.deps())
    p = d.prompts[0]
    assert "- as_ref.1 (fn_sig) in src/e.rs — currently: fn as_ref(&self) -> &A" in p
    assert "- as_ref.2 (fn_sig) in src/e.rs — currently: fn as_ref(&self) -> &B" in p
