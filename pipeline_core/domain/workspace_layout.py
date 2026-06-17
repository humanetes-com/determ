"""Workspace layout vocabulary — the well-known directory names the pipeline
writes under a project workspace. Pure constants (stdlib only); the single
source of truth so adapters and the composition root agree on the on-disk layout
instead of each repeating a string literal.

(``ORIGINAL_STRUCTURE_DIR`` currently lives in ``adapters/strip.py`` for
historical reasons; consolidate it here in a later slice.)"""
from __future__ import annotations

# Per-module locked source + the `locked.txt` ledger live here once a module's
# tests pass and it is frozen as a dependency for later modules.
LOCKED_MODULES_DIR = ".locked_modules"
