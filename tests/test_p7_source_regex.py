"""P7 — byte-equivalence pins for the 6 source-parsing regexes migrated to
structured tokenisers (see P7_PLAN.md). Each new function is compared against the
EXACT original regex it replaced, over targeted edge cases plus a seeded fuzz
corpus of Rust-ish text. The original regexes are embedded here as the oracle so a
future change to a tokeniser that diverges from the regex it claims to replace
fails loudly.
"""
from __future__ import annotations

import random
import re

from pipeline_core.domain import cfg as CFG
from pipeline_core.domain import generation as GEN
from pipeline_core.domain import rust_idents as RI

# --- original regexes (oracles) --------------------------------------------
_S1 = re.compile(
    r'(?:^|\n)[ \t]*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:unsafe\s+)?'
    r'(?:const\s+)?fn\s+([A-Za-z_]\w*)[^{]*\{',
    re.MULTILINE,
)
_S2 = re.compile(r'^\s*(?:pub\s+)?(?:async\s+)?(?:unsafe\s+)?fn\s+\w')
_S3 = re.compile(r'#\s*\[\s*test\s*\][^\n]*\n[^\n]*?fn\s+(\w+)\s*\(')
_IDENT = re.compile(r'\b([A-Za-z_][A-Za-z0-9_]*)\b')
_S4 = re.compile(r"#\[cfg\(((?:[^()]|\([^()]*\))+)\)\]\s*$", re.MULTILINE)
_PRED = re.compile(r'#\[cfg(?:_attr)?\(([^)]+(?:\([^)]*\)[^)]*)*)\)')
_FEAT = re.compile(r'\bfeature\s*=\s*"([^"]*)"')
_BARE = re.compile(r'\b([a-z_]\w*)(?!\s*=)')
_S6 = re.compile(
    r'\n+(?:// GRCOV_EXCL_START\n)?#\[cfg\(test\)\](?:\n#\[[^\]]*\])*\n'
    r'(?:pub )?mod gen_\w+\s*\{.*',
    re.DOTALL,
)
_SKIP = frozenset({"any", "all", "not", "cfg", "cfg_attr", "feature"})


def _s1_oracle(t: str) -> set[str]:
    return {m.group(1) for m in _S1.finditer(t)}


def _s4_oracle(t: str) -> str:
    ms = list(_S4.finditer(t))
    return ms[-1].group(1).strip() if ms else ""


def _s5_oracle(t: str) -> dict[str, int]:
    pc: dict[str, int] = {}
    for m in _PRED.finditer(t):
        inner = m.group(1)
        for fm in _FEAT.finditer(inner):
            if fm.group(1):
                pc[fm.group(1)] = pc.get(fm.group(1), 0) + 1
        for bm in _BARE.finditer(_FEAT.sub("", inner)):
            nm = bm.group(1)
            if nm and nm not in _SKIP:
                pc[nm] = pc.get(nm, 0) + 1
    return pc


def _s5_new(t: str) -> dict[str, int]:
    pc: dict[str, int] = {}
    for nm in CFG.gated_predicate_names(t, _SKIP):
        pc[nm] = pc.get(nm, 0) + 1
    return pc


# --- targeted edge cases ----------------------------------------------------
_CASES = [
    "",
    "pub fn foo() {}",
    "  pub(crate) async unsafe fn bar<T>(x: T) -> T {",
    "fn a()\nfn b() {",                      # brace-less decl swallows the next
    "const fn c() {}\nasync fn d() {}",
    "public fn nope() {}",                    # 'public' must not match pub
    "#[test]\nfn t_one() {}",
    "#[ test ]\n  fn t_two (x) {",            # flexible ws, name+( cross lines
    "#[test]\nlet myfn = 1;\nfn later() {",   # fn must be on the next line only
    '#[cfg(feature = "std")]\nmod m;',
    '#[cfg(all(feature="a", unix))]\nfn g() {}',
    '#[cfg_attr(feature = "x", derive(Debug))]',
    'feature="a" target_os = "linux" nightly',  # bare-flag quoting quirk
    'foo=bar baz',                            # bare backtrack-one-char quirk
    "\n\n#[cfg(test)]\n#[allow(x)]\nmod gen_tests_1 { fn z() {} }\ntrailing",
    "\n// GRCOV_EXCL_START\n#[cfg(test)]\npub mod gen_x {\n}\n",
    "café_ident Ünïcode abc_123",            # ASCII-only token boundary
]


def test_targeted_cases():
    for t in _CASES:
        assert _s1_oracle(t) == RI.fn_names_with_body(t), f"S1 {t!r}"
        assert [m.group(1) for m in _S3.finditer(t)] == RI.test_fn_names(t), f"S3 {t!r}"
        assert _IDENT.findall(t) == RI.ascii_word_tokens(t), f"IDENT {t!r}"
        assert _s4_oracle(t) == CFG.last_trailing_cfg_pred(t).strip(), f"S4 {t!r}"
        assert _s5_oracle(t) == _s5_new(t), f"S5 {t!r}"
        assert _S6.sub("", t) == GEN.strip_trailing_legacy_gen_block(t), f"S6 {t!r}"
        for ln in t.split("\n"):
            assert bool(_S2.match(ln)) == RI.is_fn_decl_line(ln), f"S2 {ln!r}"


_TOKENS = [
    'fn', 'pub', 'async', 'unsafe', 'const', 'mod', 'gen_x', 'gen_', '#[test]',
    '#', '[', 'test', ']', '#[cfg(', '#[cfg_attr(', '#[cfg(test)]', 'cfg',
    'feature', '=', '"std"', '"a"', '"linux"', '"', 'target_os', '(', ')', '{',
    '}', '\n', '\n\n', ' ', '  ', '\t', 'foo', 'Bar', 'x', 'crate', '(crate)',
    'all', 'any', 'not', ',', '// GRCOV_EXCL_START\n', 'x123', '_y', '->', 'self',
    '<', '>', 'i32', 'café', 'pub(crate)',
]


def test_fuzz_byte_equivalence():
    rng = random.Random(20260624)
    for _ in range(40_000):
        t = ''.join(rng.choice(_TOKENS) for _ in range(rng.randint(0, 40)))
        assert _s1_oracle(t) == RI.fn_names_with_body(t), f"S1 {t!r}"
        assert [m.group(1) for m in _S3.finditer(t)] == RI.test_fn_names(t), f"S3 {t!r}"
        assert _IDENT.findall(t) == RI.ascii_word_tokens(t), f"IDENT {t!r}"
        assert _s4_oracle(t) == CFG.last_trailing_cfg_pred(t).strip(), f"S4 {t!r}"
        assert _s5_oracle(t) == _s5_new(t), f"S5 {t!r}"
        assert _S6.sub("", t) == GEN.strip_trailing_legacy_gen_block(t), f"S6 {t!r}"
        for ln in t.split("\n"):
            assert bool(_S2.match(ln)) == RI.is_fn_decl_line(ln), f"S2 {ln!r}"
