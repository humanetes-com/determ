"""Typed identities (type-driven design).

Each NewType is minted at ONE parse/construction point and flows as a str
subtype everywhere else — consumers need no change, but mypy can now tell a
retro-store key from an arbitrary string at the places that construct them.

Application status: `RetroKey` is minted by `adapters.retros.retro_key`.
`ClusterName` / `ModuleName` / `RunId` are declared for the stages that
type their boundaries (cluster_plan / discovery / run registration) — mint
them there when those seams move into `build_app`.
"""

from __future__ import annotations

from typing import NewType

ClusterName = NewType("ClusterName", str)
ModuleName = NewType("ModuleName", str)
RunId = NewType("RunId", str)
RetroKey = NewType("RetroKey", str)

# A whitespace/where-clause-STABLE impl-block identity (F5). Minted ONCE by
# `impl_routing.impl_block_id` from the canonical container, so a block stored
# under one rendering (`impl<E> From<E> for Error`) and looked up under another
# (with a newline where-clause) resolve to the SAME key — the routing-table miss
# that silently dropped a whole impl's model reply (`response_unused`).
ContainerId = NewType("ContainerId", str)
