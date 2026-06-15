"""Workspace-structure + dependency-graph scanners (ADAPTERS: they do I/O —
fs walks, the `cargo metadata` subprocess, and the LLM scan). Moved verbatim
from pipeline.py (P4). Domain/log/retry are imported; the pipeline-bound
collaborators (`ask`, the task0 model, the skip-dir set, the shell-split helper)
are passed explicitly via the frozen `WorkspaceScanDeps` to each scanner/graph
constructor and the two factories (#14: constructor injection, no module state).

Backend selection: WORKSPACE_SCANNER / DEPENDENCY_GRAPH in .env (llm | static).
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pipeline_core.adapters.discovery import SKIP_DIR_NAMES as _DEFAULT_SKIP
from pipeline_core.domain.cargo_toml import cargo_is_proc_macro as _cargo_is_proc_macro
from pipeline_core.domain.cargo_toml import load_cargo_toml as _load_cargo_toml
from pipeline_core.log import log as _log
from pipeline_core.retry import with_retry


def _default_ask(*a: object, **k: object) -> str:
    return ""


@dataclass(frozen=True, slots=True)
class WorkspaceScanDeps:
    """The pipeline-bound collaborators the scanners need (#14: was injected into
    module globals via configure()). Defaults make the adapters self-contained for
    tests; the composition root passes the real `ask`/model/shell-split."""
    ask: Callable[..., str] = _default_ask
    task0_model: str = ""
    skip_dir_names: frozenset[str] = field(default_factory=lambda: _DEFAULT_SKIP)
    parse_cmd: Callable[[str], list[str]] = shlex.split


class WorkspaceScanner(ABC):
    """Produce a markdown description of workspace structure for TASK 2."""

    @abstractmethod
    def scan(self, project_dir: Path) -> str:
        """Return a markdown string, or empty string if nothing found."""


class StaticWorkspaceScanner(WorkspaceScanner):
    """
    Deterministic Python parsing of known build manifests (Rust/Cargo, Node/npm).
    Fast, no LLM call, but requires per-language code for each new ecosystem.
    """

    def __init__(self, deps: WorkspaceScanDeps | None = None) -> None:
        self._deps = deps or WorkspaceScanDeps()

    def scan(self, project_dir: Path) -> str:
        lines = ["## Workspace structure (read from existing build files — follow exactly)\n"]

        # ---- Rust / Cargo ---------------------------------------------------
        cargo_tomls = sorted(project_dir.rglob("Cargo.toml"))
        if cargo_tomls:
            lines.append("### Rust workspace (Cargo)\n")

            for ct in cargo_tomls:
                rel = ct.relative_to(project_dir)
                if rel.parts[0] in self._deps.skip_dir_names:
                    continue
                try:
                    text = ct.read_text()
                except Exception:
                    continue

                data = _load_cargo_toml(text)
                pkg = data.get("package", {})
                pkg_name = pkg.get("name", "") if isinstance(pkg, dict) else ""
                if not pkg_name:
                    continue
                crate_dir = ct.parent
                rel_dir = str(crate_dir.relative_to(project_dir)) if crate_dir != project_dir else "."

                targets = []
                lib = data.get("lib")
                if isinstance(lib, dict):
                    lib_path = lib.get("path", "src/lib.rs")
                    is_proc_macro = lib.get("proc-macro", False) is True
                    label = "proc-macro target" if is_proc_macro else "lib target"
                    targets.append(f"  - {label} → required file: `{rel_dir}/{lib_path}`")
                elif (crate_dir / "src" / "lib.rs").exists():
                    targets.append(f"  - lib target (implicit) → required file: `{rel_dir}/src/lib.rs`")

                for b in data.get("bin", []) if isinstance(data.get("bin"), list) else []:
                    bin_name = b.get("name", pkg_name) if isinstance(b, dict) else pkg_name
                    bin_path = b.get("path", "src/main.rs") if isinstance(b, dict) else "src/main.rs"
                    targets.append(f"  - bin target `{bin_name}` → required file: `{rel_dir}/{bin_path}`")

                if not targets and (crate_dir / "src" / "main.rs").exists():
                    targets.append(f"  - bin target (implicit) → required file: `{rel_dir}/src/main.rs`")

                if not targets and not (crate_dir / "src" / "main.rs").exists():
                    targets.append(f"  - lib target (implicit default) → required file: `{rel_dir}/src/lib.rs`")

                # Note if a separate tests.rs file exists — lib.rs must declare it.
                tests_note = []
                if (crate_dir / "src" / "tests.rs").exists():
                    tests_note.append(
                        f"  - ⚠️  `{rel_dir}/src/tests.rs` exists — lib.rs MUST declare "
                        f"`#[cfg(test)] mod tests;` at the end"
                    )

                # Expose [dependencies] versions (from the parsed TOML, rendered compactly).
                dep_lines = []
                deps = data.get("dependencies", {})
                if isinstance(deps, dict):
                    for name, spec in deps.items():
                        if isinstance(spec, str):
                            rendered = f'"{spec}"'
                        elif isinstance(spec, dict):
                            inner = ", ".join(f"{k} = {v!r}" for k, v in spec.items())
                            rendered = f"{{ {inner} }}"
                        else:
                            rendered = str(spec)
                        dep_lines.append(f"    {name} = {rendered}")

                if targets:
                    lines.append(f"**Crate `{pkg_name}`** (at `{rel_dir}/`):")
                    lines.extend(targets)
                    if tests_note:
                        lines.extend(tests_note)
                    if dep_lines:
                        lines.append("  - dependencies:")
                        lines.extend(dep_lines)
                    lines.append("")

            lines.append(
                "_Each crate MUST have the required entry-point file(s) listed above or the build will fail._\n"
            )

        # ---- Node / npm -----------------------------------------------------
        pkg_jsons = [p for p in sorted(project_dir.rglob("package.json"))
                     if "node_modules" not in p.parts]
        if pkg_jsons:
            lines.append("### Node.js workspace (npm/yarn)\n")
            for pj in pkg_jsons:
                try:
                    data = json.loads(pj.read_text())
                    name = data.get("name", str(pj.parent.relative_to(project_dir)))
                    main = data.get("main", "index.js")
                    rel_dir = str(pj.parent.relative_to(project_dir))
                    lines.append(f"**Package `{name}`** (at `{rel_dir}/`): main entry `{main}`")
                except Exception:
                    continue
            lines.append("")

        if len(lines) == 1:
            return ""
        return "\n".join(lines)


class LLMWorkspaceScanner(WorkspaceScanner):
    """
    Language-agnostic scanner: collects all build/manifest files found in the
    project and asks the LLM to describe the workspace structure.
    Works for any language without requiring per-language Python code.
    """

    # Manifest filenames recognised across ecosystems.
    _MANIFEST_NAMES = {
        "Cargo.toml", "package.json", "pom.xml",
        "build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts",
        "composer.json", "build.sbt", "Gemfile", "go.mod",
        "pyproject.toml", "setup.py", "setup.cfg",
        "CMakeLists.txt", "mix.exs", "pubspec.yaml",
    }

    def __init__(self, deps: WorkspaceScanDeps | None = None) -> None:
        self._deps = deps or WorkspaceScanDeps()

    def scan(self, project_dir: Path) -> str:
        manifests = self._collect_manifests(project_dir)
        if not manifests:
            return ""

        manifest_text = "\n\n".join(
            f"### {rel_path}\n```\n{content}\n```"
            for rel_path, content in manifests
        )

        prompt = (
            "You are analysing a software project's build manifests to determine its workspace structure.\n\n"
            f"{manifest_text}\n\n"
            "Based solely on the manifests above, produce a concise markdown description of the workspace:\n"
            "- List every package/crate/module.\n"
            "- For each, state the relative directory and the required source entry-point file(s) "
            "(e.g. src/lib.rs, src/main.rs, index.js, Main.java, lib/my_app.ex, etc.).\n"
            "- Use this exact format for each entry:\n"
            "  **Package `<name>`** (at `<rel_dir>/`): required file(s): `<path>` [, `<path>`]\n"
            "- End with the line: "
            "_Each package MUST have the required entry-point file(s) listed above or the build will fail._\n"
            "Do not include explanations — only the structured list."
        )

        response = self._deps.ask(prompt, model=self._deps.task0_model)
        if not response or not response.strip():
            return ""
        return "## Workspace structure (derived from build manifests — follow exactly)\n\n" + response.strip()

    def _collect_manifests(self, project_dir: Path) -> list[tuple[str, str]]:
        results = []
        for path in sorted(project_dir.rglob("*")):
            if path.name not in self._MANIFEST_NAMES:
                continue
            rel = path.relative_to(project_dir)
            if any(part in self._deps.skip_dir_names for part in rel.parts):
                continue
            try:
                content = path.read_text(errors="replace")
                results.append((str(rel), content))
            except Exception:
                continue
        return results


def _workspace_scanner_factory(deps: WorkspaceScanDeps | None = None) -> WorkspaceScanner:
    deps = deps or WorkspaceScanDeps()
    backend = os.getenv("WORKSPACE_SCANNER", "llm").lower()
    if backend == "static":
        return StaticWorkspaceScanner(deps)
    return LLMWorkspaceScanner(deps)


class DependencyGraph(ABC):
    """
    Analyses intra-workspace module dependencies and provides a topological
    processing order (leaves first, dependents last).

    Also owns the logic for scoping workspace-level commands (test, build check)
    to a single module — because that logic is ecosystem-specific.
    """

    @abstractmethod
    def modules(self) -> list[str]:
        """Return all module names in the workspace. Empty list → whole-workspace fallback."""

    @abstractmethod
    def crate_dir(self, module_name: str) -> Path:
        """Return the root directory of a module."""

    @abstractmethod
    def crate_type(self, module_name: str) -> str:
        """Return 'lib', 'bin', or 'proc-macro' (or a similar ecosystem label)."""

    @abstractmethod
    def workspace_deps(self, module_name: str) -> set[str]:
        """Return the set of workspace-internal dependencies of a module."""

    @abstractmethod
    def topological_order(self) -> list[str]:
        """Return module names ordered so every dependency precedes its dependents."""

    @abstractmethod
    def module_test_command(self, test_command: str, module_name: str) -> str:
        """Derive a module-scoped test command from the full workspace command."""

    @abstractmethod
    def module_build_check_command(self, build_cmd: str | None, module_name: str) -> str | None:
        """Derive a module-scoped compile-check command from the workspace command."""


class StaticDependencyGraph(DependencyGraph):
    """
    Deterministic Python parsing of Cargo.toml files.
    Fast, no LLM call, but Rust/Cargo workspaces only.
    Returns an empty module list for non-Cargo projects.
    """

    def __init__(self, project_dir: Path, wsdeps: WorkspaceScanDeps | None = None) -> None:
        self._project_dir = project_dir
        self._wsdeps = wsdeps or WorkspaceScanDeps()
        self._modules: dict[str, Path] = {}
        self._deps: dict[str, set[str]] = {}
        self._crate_types: dict[str, str] = {}
        self._parse()

    def _try_cargo_metadata(self) -> bool:
        """BL-006: Use `cargo metadata` to populate module/dep/type data authoritatively."""
        try:
            result = subprocess.run(
                ["cargo", "metadata", "--format-version", "1", "--no-deps"],
                cwd=str(self._project_dir),
                capture_output=True, text=True, timeout=60,
            )
            if result.returncode != 0:
                return False
            data = json.loads(result.stdout)
        except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError, Exception):
            return False

        workspace_members = set(data.get("workspace_members", []))
        packages = {pkg["id"]: pkg for pkg in data.get("packages", [])}

        for pkg_id in workspace_members:
            pkg = packages.get(pkg_id)
            if pkg is None:
                continue
            name = pkg["name"]
            manifest_path = Path(pkg["manifest_path"])
            try:
                rel = manifest_path.relative_to(self._project_dir)
            except ValueError:
                continue
            rel_parts_lower = [p.lower() for p in rel.parts]
            if any(p in ("tests", "test-helpers", "test_helpers", "testdata") for p in rel_parts_lower):
                continue

            self._modules[name] = manifest_path.parent

            crate_type = "lib"
            for target in pkg.get("targets", []):
                kinds = target.get("kind", [])
                if "proc-macro" in kinds:
                    crate_type = "proc-macro"
                    break
                if "bin" in kinds:
                    crate_type = "bin"
            self._crate_types[name] = crate_type

            dep_names: set[str] = set()
            for dep in pkg.get("dependencies", []):
                dep_name = dep["name"]
                dep_names.add(dep_name)
                dep_names.add(dep_name.replace("-", "_"))
            self._deps[name] = dep_names

        if not self._modules:
            return False

        workspace_names = set(self._modules.keys())
        for name in list(self._deps):
            resolved: set[str] = set()
            for d in self._deps[name]:
                if d in workspace_names:
                    resolved.add(d)
                elif d.replace("_", "-") in workspace_names:
                    resolved.add(d.replace("_", "-"))
            self._deps[name] = resolved

        _log(f"  [dep graph] cargo metadata: {len(self._modules)} module(s).")
        return True

    def _parse(self) -> None:
        # Try cargo metadata first (authoritative, handles workspace edge cases).
        # Falls back to regex parser for environments without cargo or network access.
        if self._try_cargo_metadata():
            return
        _log("  [dep graph] cargo metadata unavailable — using tomllib parser.")
        self._parse_manifests()

    def _parse_manifests(self) -> None:
        cargo_tomls = sorted(self._project_dir.rglob("Cargo.toml"))
        if not cargo_tomls:
            return

        for ct in cargo_tomls:
            rel = ct.relative_to(self._project_dir)
            if any(part in self._wsdeps.skip_dir_names for part in rel.parts):
                continue
            # Skip test-fixture crates: publish=false crates living under tests/ or
            # test-helpers/ directories are auxiliary and not pipeline modules.
            rel_parts_lower = [p.lower() for p in rel.parts]
            if any(p in ("tests", "test-helpers", "test_helpers", "testdata") for p in rel_parts_lower):
                continue
            try:
                text = ct.read_text()
            except Exception:
                continue

            data = _load_cargo_toml(text)
            pkg = data.get("package", {})
            pkg_name = pkg.get("name", "") if isinstance(pkg, dict) else ""
            if not pkg_name:
                continue
            self._modules[pkg_name] = ct.parent

            if _cargo_is_proc_macro(text):
                self._crate_types[pkg_name] = "proc-macro"
            elif isinstance(data.get("bin"), list) and data["bin"]:
                self._crate_types[pkg_name] = "bin"
            else:
                self._crate_types[pkg_name] = "lib"

            # tomllib (R8: never scrape a structured format with regex). The
            # dep NAMES are the table keys — handles inline-table, workspace-
            # inherited (`name.workspace = true`), and quoted forms uniformly.
            dep_names: set[str] = set()
            for section in ("dependencies", "dev-dependencies"):
                sec = data.get(section, {})
                if isinstance(sec, dict):
                    for raw in sec:
                        dep_names.add(raw)
                        dep_names.add(raw.replace("-", "_"))
            self._deps[pkg_name] = dep_names

        workspace_names = set(self._modules.keys())
        for name in list(self._deps):
            resolved: set[str] = set()
            for d in self._deps[name]:
                if d in workspace_names:
                    resolved.add(d)
                elif d.replace("_", "-") in workspace_names:
                    resolved.add(d.replace("_", "-"))
            self._deps[name] = resolved

    def modules(self) -> list[str]:
        return list(self._modules.keys())

    def crate_dir(self, module_name: str) -> Path:
        return self._modules[module_name]

    def crate_type(self, module_name: str) -> str:
        return self._crate_types.get(module_name, "lib")

    def workspace_deps(self, module_name: str) -> set[str]:
        return self._deps.get(module_name, set())

    def topological_order(self) -> list[str]:
        """Kahn's algorithm; falls back to dep-count order if a cycle is found."""
        if not self._modules:
            return []

        dependents: dict[str, list[str]] = {n: [] for n in self._modules}
        in_deg: dict[str, int] = dict.fromkeys(self._modules, 0)

        for name, deps in self._deps.items():
            for dep in deps:
                if dep in self._modules:
                    in_deg[name] += 1
                    dependents[dep].append(name)

        queue: list[str] = sorted(n for n in self._modules if in_deg[n] == 0)
        result: list[str] = []

        while queue:
            node = queue.pop(0)
            result.append(node)
            for dependent in sorted(dependents.get(node, [])):
                in_deg[dependent] -= 1
                if in_deg[dependent] == 0:
                    queue.append(dependent)

        if len(result) != len(self._modules):
            _log("  WARNING: dependency cycle detected — using dep-count order.")
            return sorted(self._modules.keys(), key=lambda n: len(self._deps.get(n, set())))

        return result

    def module_test_command(self, test_command: str, module_name: str) -> str:
        """
        cargo test                  →  cargo test -p <name>
        cargo nextest run           →  cargo nextest run -p <name>
        cargo test --all-features   →  cargo test -p <name> --all-features
        """
        cmd = test_command.strip()
        if not cmd.startswith("cargo"):
            return test_command
        parts = self._wsdeps.parse_cmd(cmd)
        insert_at = 1
        for i in range(1, len(parts)):
            if not parts[i].startswith("-"):
                insert_at = i + 1
            else:
                break
        return " ".join(parts[:insert_at] + ["-p", module_name] + parts[insert_at:])

    def module_build_check_command(self, build_cmd: str | None, module_name: str) -> str | None:
        """cargo check --workspace  →  cargo check -p <name>"""
        if not build_cmd:
            return None
        cmd = build_cmd.strip()
        if "--workspace" in cmd:
            return cmd.replace("--workspace", f"-p {module_name}")
        if cmd.startswith("cargo check"):
            return f"cargo check -p {module_name}"
        return build_cmd


class LLMDependencyGraph(DependencyGraph):
    """
    Language-agnostic dependency graph: collects all build/manifest files and
    asks the model to extract the module list, dependency edges, topological
    order, and module-scoped command templates.

    Works for any build system without requiring per-ecosystem Python code.
    Falls back to an empty module list if the model returns unusable output.
    """

    # Same manifest filenames recognised by LLMWorkspaceScanner.
    _MANIFEST_NAMES = {
        "Cargo.toml", "package.json", "pom.xml",
        "build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts",
        "composer.json", "build.sbt", "Gemfile", "go.mod",
        "pyproject.toml", "setup.py", "setup.cfg",
        "CMakeLists.txt", "mix.exs", "pubspec.yaml",
    }

    def __init__(self, project_dir: Path, wsdeps: WorkspaceScanDeps | None = None) -> None:
        self._project_dir = project_dir
        self._wsdeps = wsdeps or WorkspaceScanDeps()
        self._modules: dict[str, Path] = {}
        self._deps: dict[str, set[str]] = {}
        self._crate_types: dict[str, str] = {}
        self._topo: list[str] = []
        self._test_cmd_tpl: str = ""        # e.g. "cargo test -p {module}"
        self._build_check_tpl: str = ""     # e.g. "cargo check -p {module}"
        self._parse()

    def _collect_manifests(self) -> list[tuple[str, str]]:
        results = []
        for path in sorted(self._project_dir.rglob("*")):
            if path.name not in self._MANIFEST_NAMES:
                continue
            rel = path.relative_to(self._project_dir)
            if any(part in self._wsdeps.skip_dir_names for part in rel.parts):
                continue
            try:
                content = path.read_text(errors="replace")
                results.append((str(rel), content))
            except Exception:
                continue
        return results

    def _parse(self) -> None:
        manifests = self._collect_manifests()
        if not manifests:
            return

        manifest_text = "\n\n".join(
            f"### {rel_path}\n```\n{content}\n```"
            for rel_path, content in manifests
        )

        prompt = f"""You are analysing a software project's build manifests to extract its
module dependency graph.

{manifest_text}

Based solely on the manifests above, output the dependency graph in this exact format.
Use one line per module, then the topological order, then the module-scoped command templates.
Do not add any explanation or extra text.

For each module:
MODULE: name=<name> dir=<relative_dir> type=<lib|bin|proc-macro> deps=<comma-separated names or empty>

Then:
TOPO_ORDER: <module names in topological order, space-separated, leaves first>
MODULE_TEST_CMD: <full test command with the literal placeholder {{module}}, e.g. cargo test -p {{module}}>
MODULE_BUILD_CHECK: <compile-only check command with {{module}}, or NONE if not applicable>

Rules:
- deps must only include other modules listed above (intra-workspace only, no external libraries)
- If the project has only one module, output that single module with no deps
- MODULE_TEST_CMD and MODULE_BUILD_CHECK use {{module}} as a placeholder for the module name
"""

        def attempt() -> Any:
            response = self._wsdeps.ask(prompt, model=self._wsdeps.task0_model)
            parsed = self._parse_response(response)
            return parsed if parsed else False

        result = with_retry(attempt)
        if result is False:
            _log("  WARNING: LLMDependencyGraph could not parse model response — "
                 "falling back to whole-workspace mode.")
            return

        modules, deps, types, topo, test_tpl, check_tpl = result
        self._modules = modules
        self._deps = deps
        self._crate_types = types
        self._topo = topo
        self._test_cmd_tpl = test_tpl
        self._build_check_tpl = check_tpl

    def _parse_response(self, text: str) -> tuple[Any, ...] | None:
        """
        Parse the structured model response into (modules, deps, types, topo, test_tpl, check_tpl).
        Returns None if required fields are missing or malformed.
        """
        modules: dict[str, Path] = {}
        deps: dict[str, set[str]] = {}
        types: dict[str, str] = {}

        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("MODULE:"):
                continue
            # MODULE: name=argh dir=argh type=lib deps=argh_shared,argh_derive
            name_m = re.search(r'name=([^\s]+)', line)
            dir_m  = re.search(r'dir=([^\s]+)', line)
            type_m = re.search(r'type=([^\s]+)', line)
            deps_m = re.search(r'deps=([^\s]*)', line)
            if not name_m or not dir_m:
                continue
            name = name_m.group(1)
            crate_dir = self._project_dir / dir_m.group(1)
            crate_type = type_m.group(1) if type_m else "lib"
            raw_deps = deps_m.group(1) if deps_m else ""
            dep_set = {d.strip() for d in raw_deps.split(",") if d.strip()} if raw_deps else set()
            modules[name] = crate_dir
            types[name] = crate_type
            deps[name] = dep_set

        if not modules:
            return None

        # Prune deps to workspace-only.
        for name in deps:
            deps[name] = {d for d in deps[name] if d in modules}

        # Topological order.
        topo: list[str] = []
        topo_m = re.search(r'^TOPO_ORDER:\s*(.+)$', text, re.MULTILINE)
        if topo_m:
            topo = [t.strip() for t in topo_m.group(1).split() if t.strip() in modules]
        if len(topo) != len(modules):
            # Fall back to alphabetical if model output is incomplete.
            topo = sorted(modules.keys())

        # Command templates.
        test_tpl = ""
        test_m = re.search(r'^MODULE_TEST_CMD:\s*(.+)$', text, re.MULTILINE)
        if test_m:
            test_tpl = test_m.group(1).strip()

        check_tpl = ""
        check_m = re.search(r'^MODULE_BUILD_CHECK:\s*(.+)$', text, re.MULTILINE)
        if check_m and check_m.group(1).strip().upper() != "NONE":
            check_tpl = check_m.group(1).strip()

        return modules, deps, types, topo, test_tpl, check_tpl

    def modules(self) -> list[str]:
        return list(self._modules.keys())

    def crate_dir(self, module_name: str) -> Path:
        return self._modules[module_name]

    def crate_type(self, module_name: str) -> str:
        return self._crate_types.get(module_name, "lib")

    def workspace_deps(self, module_name: str) -> set[str]:
        return self._deps.get(module_name, set())

    def topological_order(self) -> list[str]:
        return list(self._topo)

    def module_test_command(self, test_command: str, module_name: str) -> str:
        if self._test_cmd_tpl:
            return self._test_cmd_tpl.replace("{module}", module_name)
        return test_command  # no template: whole-workspace command unchanged

    def module_build_check_command(self, build_cmd: str | None, module_name: str) -> str | None:
        if self._build_check_tpl:
            return self._build_check_tpl.replace("{module}", module_name)
        return build_cmd


def _dependency_graph_factory(project_dir: Path,
                              deps: WorkspaceScanDeps | None = None) -> DependencyGraph:
    deps = deps or WorkspaceScanDeps()
    backend = os.getenv("DEPENDENCY_GRAPH", "static").lower()
    if backend == "llm":
        return LLMDependencyGraph(project_dir, deps)
    return StaticDependencyGraph(project_dir, deps)
