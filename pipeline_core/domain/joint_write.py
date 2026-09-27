"""The write set a single-body fill may widen, on the model's word.

A per-fn fill is told to write ONE body. Its reply may also carry
``<FN name=… block=…>`` blocks (the file and block routes' own contract,
parsed by `llm_output.extract_fn_bodies`) for other functions of the SAME
file that body cannot be right without; this module decides which of them
the unit may write. The pipeline does not decide which functions belong
together — the model is the language expert — it decides only who may
write: a name that resolves to exactly one function of the target's file
in the cluster, is not the target, is not compiled out, within a cap. The
set is then written, judged and kept or reverted as one.

Why the contract exists. Run `116341-24c76a97` (2026-09-25, cluster
`stable`): `src/error.rs::context` was a stub 45 of 62 failing tests stopped
in, dispatched per-fn in six fills and reverted every time on the same
E0277. The fill-5 reasoning (88 KB) derived the body the tree needed — a
payload type wrapping the crate's error, installed through a fn-pointer
table whose downcast and drop entries delegate to the inner error — noted
those entries were stubs or shaped for another payload, and, able to write
only `context`, answered with the call it had already shown cannot compile
("Given uncertainty, I'll assume Error implements StdError"). No route of
the run ever opened a write set holding `context` together with those
table functions.

Beyond the target's file. The file limit was the first answer to "who may
write together", and it cannot write a coupling that spans files: the
recursion `Boxed::new` (`src/kind.rs`) -> `from_boxed` ->
`construct_from_boxed` (`src/error.rs`) of runs `38934-2999cc94` through
`59479-cc3afffd` (2026-09-26/27) was opened per-fn on one member, whose
reply could name only functions of its own file. A unit's COUPLED bodies
(`domain.write_set.coupled_units`: a cycle of the tree's call graph, or of
an observed faulting stack) are admitted from any file, resolved against
that coupled set only; everything else keeps the file rule.

Pure: records in, admission out. The splice, the judge and the revert are
the route's (`fill_one_fn`)."""
from __future__ import annotations

import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from pipeline_core.domain.models import FnLocator, FnSite

#: How many functions besides the target one reply may add. Sized to the
#: widest joint write measured: a table-builder and the three table entries
#: it installs (the 2026-09-25 `context` case), with one to spare.
DEFAULT_JOINT_MAX = 4

#: The block header the file and block routes use for a free fn.
FREE_FNS = "free fns"


@dataclass(frozen=True, slots=True)
class JointUnit:
    """One admitted extra: where it is, and the body the reply gave it."""

    file: str
    fn_name: str
    container: str
    sig_text: str
    cfg_predicates: tuple[str, ...]
    body: str

    @property
    def locator(self) -> FnLocator:
        return FnLocator(self.file, self.fn_name)

    @property
    def label(self) -> str:
        return (f"{self.file}::{self.fn_name}"
                + (f" [{self.container}]" if self.container else ""))


@dataclass(frozen=True, slots=True)
class JointAdmission:
    admitted: tuple[JointUnit, ...] = ()
    #: (what the reply named, why it may not be written)
    refused: tuple[tuple[str, str], ...] = ()

    @property
    def asked(self) -> bool:
        return bool(self.admitted or self.refused)

    def refusal_note(self) -> str:
        """The sentence a retry turn carries so the next reply knows which
        of its extra blocks were not written, and why."""
        if not self.refused:
            return ""
        parts = "; ".join(f"`{name}`: {why}" for name, why in self.refused)
        return ("Not written (the extra <FN> blocks may only name other "
                "functions of this file, or a body listed under "
                f"COUPLED_BODIES): {parts}.")


def _norm(text: str) -> str:
    return " ".join(text.split())


_LABELLED = re.compile(r"^(?P<name>[^\s\[(]+)\s*[\[(](?P<container>.+?)[\])]$")


def _split_label(name: str) -> tuple[str, str]:
    """``fmt [impl Debug for MyError]`` -> (``fmt``, ``impl Debug for MyError``).

    The pipeline names a unit that way everywhere the model reads it (the
    session's editable list, the fill log, the route labels, ``JointUnit.label``),
    so a reply echoes the label into ``name=`` — measured on run
    `38934-2999cc94` (2026-09-26): `reply widened the write — admitted none,
    refused [('fmt [impl Debug for MyError]', 'no function of that name in
    src/nightly.rs')]`, a fn that exists in that file, refused because the
    whole label was read as its name. The input vocabulary is the output
    vocabulary; a name that is not a bare identifier is split, never refused
    for its spelling. A bare name comes back unchanged with no container."""
    m = _LABELLED.match(name.strip())
    if m is None:
        return name.strip(), ""
    return m.group("name"), m.group("container").strip()


def _block_matches(block: str, container: str) -> bool:
    b = _norm(block)
    if not b or b.lower() == FREE_FNS:
        return not container
    return b == _norm(container)


def admit_joint(
    blocks: Sequence[Mapping[str, Any]],
    *,
    target_file: str,
    target_name: str,
    target_container: str,
    records: Sequence[Mapping[str, Any]],
    cfg_dead: Collection[tuple[str, str]] = (),
    cap: int = DEFAULT_JOINT_MAX,
    coupled: Collection[FnSite] = (),
) -> JointAdmission:
    """Which of the reply's extra ``<FN>`` blocks the unit may write.

    ``blocks`` are `extract_fn_bodies` records (``fn_name``, ``block``,
    ``body``); ``records`` the cluster's fn records (``file``, ``fn_name``,
    ``container``, ``sig_text``, ``cfg_predicates``). A block is admitted
    when its name resolves to exactly one record of ``target_file``: by
    name alone when one record has it, else by the block header matched to
    the record's container. A name the target's file does not declare
    resolves against ``coupled`` (sites of any file), by name, then by the
    block header against the site's container; a ``file::name`` spelling
    picks the file. The target itself, a compiled-out function, a second
    block for an admitted function and anything past ``cap`` are refused,
    each with its reason; nothing is silently dropped."""
    here_all = [r for r in records if str(r.get("file", "")) == target_file]
    coupled_keys = {(c.file, c.fn_name, _norm(c.container)) for c in coupled}
    coupled_recs = [r for r in records
                    if (str(r.get("file", "")), str(r.get("fn_name", "")),
                        _norm(str(r.get("container", "") or "")))
                    in coupled_keys]
    dead = frozenset(cfg_dead)
    admitted: list[JointUnit] = []
    refused: list[tuple[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for b in blocks:
        name = str(b.get("fn_name", "")).strip()
        block = str(b.get("block", "") or "")
        body = str(b.get("body", "") or "")
        shown = name + (f" [{block}]" if block else "")
        if not any(str(r.get("fn_name", "")) == name for r in here_all):
            # Only when the literal name resolves to nothing: a fn whose own
            # name is the identifier keeps the reading it always had, and an
            # explicit `block=` outranks a container spelled into the name.
            bare, labelled = _split_label(name)
            if bare != name:
                name, block = bare, (block or labelled)
        # `src/other.rs::name` spells the file; only a coupled body may be
        # named in another file than the target's.
        spelled, _sep, leaf = name.rpartition("::")
        spelled = spelled if _sep and spelled.endswith(".rs") else ""
        if spelled:
            name = leaf
        if not name or not body.strip():
            refused.append((shown or "?", "no name or no body"))
            continue
        coupled_named = [r for r in coupled_recs
                         if str(r.get("fn_name", "")) == name
                         and (not spelled or str(r.get("file", "")) == spelled)]
        if spelled and spelled != target_file:
            named = coupled_named
        else:
            named = [r for r in here_all if str(r.get("fn_name", "")) == name]
            by_block = [r for r in coupled_named
                        if _block_matches(block, str(r.get("container", "")))]
            if (block and by_block and not spelled
                    and not any(_block_matches(block, str(r.get("container", "")))
                                for r in named)):
                # The header names a coupled body, not this file's
                # same-named function.
                named = by_block
            elif not named and not spelled:
                named = coupled_named
        if not named:
            refused.append((shown, f"no function of that name in {target_file}"
                                   + (" or among the coupled bodies"
                                      if coupled_recs else "")))
            continue
        if len(named) > 1:
            named = [r for r in named
                     if _block_matches(block, str(r.get("container", "")))]
            if len(named) != 1:
                refused.append((shown, "ambiguous — name the impl block it "
                                       "belongs to in `block`"))
                continue
        rec = named[0]
        container = str(rec.get("container", ""))
        rec_file = str(rec.get("file", "")) or target_file
        if (rec_file == target_file and name == target_name
                and _norm(container) == _norm(target_container)):
            refused.append((shown, "that is the target — its body goes in "
                                   "<BODY>"))
            continue
        if (rec_file, name) in dead:
            refused.append((shown, "compiled out in this cluster"))
            continue
        key = (rec_file, name, _norm(container))
        if key in seen:
            refused.append((shown, "given twice"))
            continue
        if len(admitted) >= cap:
            refused.append((shown, f"over the cap of {cap} extra function(s)"))
            continue
        seen.add(key)
        admitted.append(JointUnit(
            file=rec_file, fn_name=name, container=container,
            sig_text=str(rec.get("sig_text", "")),
            cfg_predicates=tuple(rec.get("cfg_predicates") or ()),
            body=body))
    return JointAdmission(tuple(admitted), tuple(refused))


#: The clause the per-fn output contract carries (initial, fix and retry
#: turns alike), so every reply of the conversation may use it.
JOINT_OUTPUT_CLAUSE = (
    "If this body cannot be correct unless OTHER functions of this same file "
    "change too (a helper it must call, a table entry it must install), give "
    "each of them AFTER the <BODY>, as\n"
    '  <FN name="fn_name" block="impl header, or free fns">\n'
    "  body without surrounding braces\n"
    "  </FN>\n"
    f"at most {DEFAULT_JOINT_MAX}. They are written and judged together with "
    "this body: all kept or all reverted. Only functions of this file, or a "
    "body listed under COUPLED_BODIES (those may sit in another file: name "
    'it `name="src/other.rs::fn_name"` with its impl header in `block`); '
    "leave them out when the target alone is enough.")
