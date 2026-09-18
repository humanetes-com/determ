"""Pure Cargo.toml accessors — parse the structured format with tomllib,
never regex. No I/O — callers read the file text and pass it in."""

from __future__ import annotations

import tomllib
from typing import Any


def load_cargo_toml(text: str) -> dict[str, Any]:
    """Parse Cargo.toml content via tomllib. Returns {} on malformed input."""
    try:
        return tomllib.loads(text)
    except (tomllib.TOMLDecodeError, ValueError, TypeError):
        return {}


def cargo_package_name(text: str) -> str:
    """The [package] name, or "" if absent/unparseable."""
    pkg = load_cargo_toml(text).get("package", {})
    return str(pkg.get("name", "") or "") if isinstance(pkg, dict) else ""


def cargo_is_proc_macro(text: str) -> bool:
    """True iff `[lib] proc-macro = true`."""
    lib = load_cargo_toml(text).get("lib", {})
    return isinstance(lib, dict) and lib.get("proc-macro", False) is True


def cargo_entry_point_paths(text: str) -> list[str]:
    """Explicit crate entry-point source paths declared in Cargo.toml, in
    precedence order: `[lib] path`, then each `[[bin]] path`. Regex-free —
    replaces the `\\[lib\\].*?path` / `\\[\\[bin\\]\\].*?path` DOTALL regexes."""
    data = load_cargo_toml(text)
    out: list[str] = []
    lib = data.get("lib")
    if isinstance(lib, dict) and isinstance(lib.get("path"), str):
        out.append(lib["path"])
    bins = data.get("bin")
    if isinstance(bins, list):
        for b in bins:
            if isinstance(b, dict) and isinstance(b.get("path"), str):
                out.append(b["path"])
    return out


def cargo_default_features(text: str) -> list[str]:
    """The `[features] default = [...]` list (empty if absent)."""
    feats = load_cargo_toml(text).get("features", {})
    default = feats.get("default", []) if isinstance(feats, dict) else []
    return [str(x) for x in default] if isinstance(default, list) else []
