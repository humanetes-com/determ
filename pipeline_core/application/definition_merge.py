"""Preserve implemented bodies when a declaration is reinstalled.

Reinstalling an authored definition over a live one is a SIGNATURE operation:
the authored text is the layout of record, so its shape must win. What it must
never do is take a member's BODY back to a stub — a trait declaration carries
its members' default bodies inside it, so replacing the declaration wholesale
re-emits every default as `sig { unimplemented!() }` and silently discards work
the fill loop already landed and reported as filled.

Observed cost: `anyhow`'s `kind.rs` declares `anyhow_kind` as a default method
on three traits. Two of them were reinstalled at every single startup, so a
5-char body was re-filled and re-stubbed roughly a dozen times over nine days,
each round a full model call. The state the invariant forbids is exactly that
loop: a reinstall may replace a member's signature, never its body with a stub.
"""
from __future__ import annotations

from pipeline_core.application.reconstruct.fill_rules import is_unimplemented_body
from pipeline_core.domain.rust_fn_headers import fn_headers


def _member_bodies(text: str) -> dict[str, tuple[int, int]]:
    """Byte-free char spans of each named fn body in `text`, last wins.

    Only the members a declaration OWNS are visible here, because the caller
    passes one declaration's text — not a whole file — so a same-named fn in
    another impl cannot be reached."""
    out: dict[str, tuple[int, int]] = {}
    for h in fn_headers(text, line_anchored=False):
        start = h.brace_open + 1
        depth = 1
        i = start
        while i < len(text) and depth > 0:
            ch = text[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            i += 1
        if depth == 0:
            out[h.name] = (start, i - 1)
    return out


def merge_live_bodies(authored: str, live: str) -> str:
    """`authored` with every stubbed member body replaced by `live`'s, when
    live holds a real implementation of that member.

    Direction is deliberate: authored wins on structure (it is the layout being
    installed), live wins on bodies (it is the work already done). A member the
    authored text implements is left alone — an authored body is a decision,
    not an accident — and a member absent from either side is untouched."""
    live_bodies = _member_bodies(live)
    if not live_bodies:
        return authored
    spans = _member_bodies(authored)
    # Right-to-left so each splice leaves earlier spans valid.
    for name, (a_start, a_end) in sorted(spans.items(), key=lambda kv: -kv[1][0]):
        if not is_unimplemented_body(authored[a_start:a_end], name):
            continue
        live_span = live_bodies.get(name)
        if live_span is None:
            continue
        live_body = live[live_span[0]:live_span[1]]
        if is_unimplemented_body(live_body, name):
            continue
        authored = authored[:a_start] + live_body + authored[a_end:]
    return authored
