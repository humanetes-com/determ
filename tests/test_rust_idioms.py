"""Byte-equivalence pins for `pipeline_core.domain.rust_idioms`.

Each detector replaced an `_IDIOM_*_RE` regex in pipeline.py; the tests compare
against the exact original pattern (projected onto the groups the consumer used).
"""
from __future__ import annotations

import re

from pipeline_core.domain.rust_idioms import (
    fn_pointer_field_calls,
    self_constructor_calls,
    struct_literals,
    unsafe_block_chains,
)

_SELF = re.compile(r"\bSelf::([A-Za-z_][A-Za-z_0-9]*)\s*\(([^()]{0,200}?)\)", re.DOTALL)
_FNPTR = re.compile(r"\(\s*([A-Za-z_][\w.]+?)\s*\)\s*\(")
_UNSAFE = re.compile(r"unsafe\s*\{\s*([^\n{}]{1,160})\s*\}")
_STRUCT = re.compile(r"\b([A-Z][A-Za-z0-9_]{2,40})\s*\{\s*([^{}]{1,400})\s*\}", re.DOTALL)

_CASES = [
    "Self::new(x)",
    "Self::with(a, b)\nSelf::from(())",
    "xSelf::new(y)",                       # not word-boundary → no Self match
    "Self::new()",                          # empty args (0-len group2)
    "(self.vtable)(args)",                  # fn-ptr field
    "(a.b.c)(x) (plain)(y)",
    "unsafe { *ptr.add(1) }",
    "unsafe {\n  inner()\n}",               # newline in body → group1 stops at \n
    "unsafe {   }",                         # whitespace-only inner
    "Wrapper { inner: x, flag: true }",
    "Foo { } Bar {a}",
    "Self::method(Self::other(z))",         # nested paren → outer non-greedy fails
    "VeryLongTypeNameThatExceedsFortyOneCharactersTotally { x }",  # >41 → no match
    "Ab { y }",                             # name len 2 (<3) → no match
    "",
]


def test_self_constructor_calls_matches_regex():
    for t in _CASES:
        assert self_constructor_calls(t) == [(m.group(0), m.group(1)) for m in _SELF.finditer(t)], t


def test_fn_pointer_field_calls_matches_regex():
    for t in _CASES:
        assert fn_pointer_field_calls(t) == [m.group(1) for m in _FNPTR.finditer(t)], t


def test_unsafe_block_chains_matches_regex():
    for t in _CASES:
        assert unsafe_block_chains(t) == [m.group(1) for m in _UNSAFE.finditer(t)], t


def test_struct_literals_matches_regex():
    for t in _CASES:
        assert struct_literals(t) == [(m.group(1), m.group(2)) for m in _STRUCT.finditer(t)], t
