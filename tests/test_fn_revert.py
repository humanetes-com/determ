"""Pins for the surgical fn-revert adapter."""
from __future__ import annotations

from pipeline_core.adapters.fn_revert import revert_fns_to_stub
from pipeline_core.application.reconstruct.tree_txn import TreeTxn
from pipeline_core.domain.models import FnLocator


def test_reverts_named_fn_to_unimplemented(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "error.rs").write_text(
        "impl Debug for Error {\n"
        "    fn fmt(&self, f: &mut Formatter) -> fmt::Result {\n"
        "        write!(f, \"oops\")\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )
    n = revert_fns_to_stub([FnLocator("error.rs", "fmt")], TreeTxn(tmp_path / "src"))
    assert n == 1
    out = (src / "error.rs").read_text(encoding="utf-8")
    assert "unimplemented!()" in out
    assert "oops" not in out


def test_every_same_name_occurrence_reverted(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "error.rs").write_text(
        "impl Debug for A { fn fmt(&self, f: &mut Formatter) -> fmt::Result { write!(f, \"a\") } }\n"
        "impl Debug for B { fn fmt(&self, f: &mut Formatter) -> fmt::Result { write!(f, \"b\") } }\n",
        encoding="utf-8",
    )
    n = revert_fns_to_stub([FnLocator("error.rs", "fmt")], TreeTxn(tmp_path / "src"))
    assert n == 2
    out = (src / "error.rs").read_text(encoding="utf-8")
    assert out.count("unimplemented!()") == 2


def test_trait_declaration_of_same_name_is_not_reverted(tmp_path):
    # `fn not(self) -> bool;` declares, it does not define. Treating it as a
    # locatable fn made the revert splice over the trait's closing brace and
    # the following impl header, so the file stopped parsing and the whole
    # cluster lost its build.
    src = tmp_path / "src"
    src.mkdir()
    (src / "lib.rs").write_text(
        "pub trait Bool {\n"
        "    fn not(self) -> bool;\n"
        "}\n"
        "impl Bool for bool {\n"
        "    #[inline]\n"
        "    fn not(self) -> bool {\n"
        "        !self\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )
    n = revert_fns_to_stub([FnLocator("lib.rs", "not")], TreeTxn(tmp_path / "src"))
    assert n == 1
    out = (src / "lib.rs").read_text(encoding="utf-8")
    assert out.count("{") == out.count("}")
    assert "fn not(self) -> bool;" in out
    assert "impl Bool for bool {" in out
    assert out.count("unimplemented!()") == 1


def test_already_stub_not_counted(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "error.rs").write_text(
        "fn foo() -> u8 { unimplemented!() }\n", encoding="utf-8")
    n = revert_fns_to_stub([FnLocator("error.rs", "foo")], TreeTxn(tmp_path / "src"))
    assert n == 0


def test_missing_file_is_a_noop(tmp_path):
    n = revert_fns_to_stub([FnLocator("missing.rs", "foo")], TreeTxn(tmp_path / "src"))
    assert n == 0


def test_multiple_fn_names_same_file(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "ptr.rs").write_text(
        "impl Own<T> {\n"
        "    fn boxed(self) -> Box<T> { unsafe { Box::from_raw(self.ptr.as_ptr()) } }\n"
        "    fn by_ref(&self) -> Ref<T> { Ref::from_raw(self.ptr) }\n"
        "}\n",
        encoding="utf-8",
    )
    n = revert_fns_to_stub(
        [FnLocator("ptr.rs", "boxed"), FnLocator("ptr.rs", "by_ref")], TreeTxn(tmp_path / "src"))
    assert n == 2
    out = (src / "ptr.rs").read_text(encoding="utf-8")
    assert out.count("unimplemented!()") == 2
