"""Markdown-in-Rust-doc-comment rendering helpers (P7 — replaces the doc-attr regexes
in `_doc_attr_string` / `_condense_doc_run`).

These operate on the text INSIDE Rust ``#[doc = "..."]`` attributes / doc comments when
condensing them to a one-line prompt summary. Pure: stdlib only, no regex.
"""

from __future__ import annotations


def doc_eq_value_offset(attr_inner: str) -> int | None:
    """If `attr_inner` begins with ``doc\\s*=\\s*`` (a ``doc = "..."`` attribute), return
    the offset just past that prefix (the old ``re.match(r'doc\\s*=\\s*', s).end()``);
    else None. ``doc`` is matched literally (no word boundary), as the regex did."""
    if not attr_inner.startswith("doc"):
        return None
    n = len(attr_inner)
    p = 3
    while p < n and attr_inner[p].isspace():  # \s*
        p += 1
    if p >= n or attr_inner[p] != "=":
        return None
    p += 1
    while p < n and attr_inner[p].isspace():  # \s*
        p += 1
    return p


def is_link_ref_def(s: str) -> bool:
    """True iff `s` starts with a markdown link-reference definition ``[label]:`` —
    byte-equivalent to ``re.match(r'\\[[^\\]]+\\]:', s)`` (label is ≥1 non-``]`` char)."""
    if not s.startswith("["):
        return False
    close = s.find("]", 1)
    return close > 1 and close + 1 < len(s) and s[close + 1] == ":"


def strip_inline_md_links(text: str) -> str:
    """Reduce every inline markdown link ``[label](url)`` to its `label` — byte-equivalent
    to ``re.sub(r'\\[([^\\]]+)\\]\\([^)]*\\)', r'\\1', text)`` (label ≥1 non-``]``; url is any
    run of non-``)``)."""
    out: list[str] = []
    n = len(text)
    i = 0
    while i < n:
        if text[i] == "[":
            close = text.find("]", i + 1)
            if close > i + 1 and close + 1 < n and text[close + 1] == "(":  # [^\]]+ then ](
                rclose = text.find(")", close + 2)
                if rclose != -1:  # [^)]* then )
                    out.append(text[i + 1:close])  # group 1 (the label)
                    i = rclose + 1
                    continue
        out.append(text[i])
        i += 1
    return "".join(out)
