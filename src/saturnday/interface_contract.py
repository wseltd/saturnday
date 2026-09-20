"""Interface-contract checks for generated plans.

Large plans describe each ticket in prose.  Nothing records *what a ticket
produces*, so a later ticket that consumes an earlier ticket's output can only
describe it in words — and nothing checks that the description matches what the
earlier ticket was actually asked to build.  On large plans this produces
tickets written against an imagined version of their own dependency.

This module adds two plan-time checks:

* **IC-001 — duplicate provides.**  Two tickets must not both claim ownership of
  the same file or the same exported symbol.  Opt-in: fires only for tickets
  that declare a ``provides`` block.
* **IC-002 — unresolvable verify_cmd tool.**  The executable named in a
  ``verify_cmd`` should resolve against the project's declared dependencies.  A
  ticket naming a binary that was never declared cannot pass however well it is
  implemented.

Both checks are deliberately kept out of :func:`saturnday.plan_parser.validate_plan`.
That function is called by :func:`saturnday.plan_parser.load_plan`, which raises
on any error — adding checks there would make existing plans on disk unloadable.
These run from ``saturnday validate-plan`` instead, where reporting is advisory.

Severity contract:

* IC-001 is an ``error``.  ``provides`` is a new optional key, so no plan
  written before this module existed can trigger it.
* IC-002 is a ``warning``.  Every existing plan has ``verify_cmd`` values, and
  tool resolution depends on the machine running the check, so this must never
  turn a previously-valid plan into a failure.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "InterfaceFinding",
    "check_duplicate_provides",
    "check_verify_cmd_tools",
    "check_interface_contract",
    "extract_executables",
    "collect_declared_tools",
]


# Tools assumed present in any POSIX dev environment.  Naming one of these in a
# verify_cmd is never reported — they are not project dependencies.
_BASELINE_TOOLS: frozenset[str] = frozenset({
    "sh", "bash", "zsh", "env", "cd", "echo", "printf", "test", "true", "false",
    "cat", "ls", "mkdir", "rm", "cp", "mv", "touch", "chmod", "find", "grep",
    "sed", "awk", "cut", "sort", "uniq", "head", "tail", "tr", "wc", "xargs",
    "tee", "diff", "git", "make", "curl", "wget", "tar", "zip", "unzip",
    "python", "python3", "pip", "pip3", "node", "npm", "npx", "timeout",
})

# Package runners: the tool actually invoked is a later token, not the runner.
_RUNNERS: frozenset[str] = frozenset({
    "npx", "pnpm", "yarn", "bun", "bunx", "uv", "uvx", "poetry", "pipx", "hatch",
})

# Subcommands that separate a runner from the tool it runs.
_RUNNER_SUBCOMMANDS: frozenset[str] = frozenset({
    "exec", "run", "dlx", "x", "tool",
})

# Shell operators that separate one command from the next.
_SPLIT_RE = re.compile(r"&&|\|\||;|\||\n")

# A leading NAME=value environment assignment.
_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


@dataclass
class InterfaceFinding:
    """One interface-contract finding.

    Attributes:
        rule_id: Stable rule identifier (``IC-001`` / ``IC-002``).
        severity: ``"error"`` or ``"warning"``.
        ticket_ids: Tickets involved in the finding.
        detail: Human-readable explanation.
    """

    rule_id: str
    severity: str
    detail: str
    ticket_ids: list[str] = field(default_factory=list)

    def format_line(self) -> str:
        """Render the finding as a single report line."""
        where = ", ".join(self.ticket_ids) if self.ticket_ids else "plan"
        return f"[{self.rule_id}] {where}: {self.detail}"


def _symbol_name(export_entry: str) -> str:
    """Reduce an export signature to the bare symbol name.

    ``"defineFixture(f: Shape): Shape"`` and
    ``"defineFixture(fixture: Shape): Shape"`` both reduce to
    ``"defineFixture"``.  Comparing full signature strings would reject valid
    plans over whitespace or parameter-name differences, so ownership is
    compared on the symbol alone.

    Args:
        export_entry: Raw export string from a ``provides.exports`` list.

    Returns:
        The leading identifier, or the stripped input when no identifier is
        found.
    """
    text = (export_entry or "").strip()
    match = re.match(r"[A-Za-z_$][A-Za-z0-9_$]*", text)
    return match.group(0) if match else text


def _iter_tickets(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the raw ticket dicts from a plan, tolerating malformed input."""
    tickets = raw.get("tickets")
    if not isinstance(tickets, list):
        return []
    return [t for t in tickets if isinstance(t, dict)]


def check_duplicate_provides(raw: dict[str, Any]) -> list[InterfaceFinding]:
    """IC-001 — reject two tickets claiming the same file or exported symbol.

    Only tickets carrying a ``provides`` block participate.  A plan with no
    ``provides`` anywhere produces no findings.

    Args:
        raw: Parsed plan JSON.

    Returns:
        One finding per collision, ordered by the colliding name.
    """
    file_owners: dict[str, list[str]] = {}
    export_owners: dict[str, list[str]] = {}

    for ticket in _iter_tickets(raw):
        tid = str(ticket.get("ticket_id", "?"))
        provides = ticket.get("provides")
        if not isinstance(provides, dict):
            continue

        for path in provides.get("files") or []:
            if not isinstance(path, str) or not path.strip():
                continue
            key = path.strip().lstrip("./")
            file_owners.setdefault(key, []).append(tid)

        for export in provides.get("exports") or []:
            if not isinstance(export, str) or not export.strip():
                continue
            name = _symbol_name(export)
            if name:
                export_owners.setdefault(name, []).append(tid)

    findings: list[InterfaceFinding] = []

    for path, owners in sorted(file_owners.items()):
        if len(owners) > 1:
            findings.append(InterfaceFinding(
                rule_id="IC-001",
                severity="error",
                ticket_ids=sorted(set(owners)),
                detail=(
                    f"{len(set(owners))} tickets each declare they provide the "
                    f"file {path!r}. Exactly one ticket must own it."
                ),
            ))

    for name, owners in sorted(export_owners.items()):
        if len(owners) > 1:
            findings.append(InterfaceFinding(
                rule_id="IC-001",
                severity="error",
                ticket_ids=sorted(set(owners)),
                detail=(
                    f"{len(set(owners))} tickets each declare they export "
                    f"{name!r}. Exactly one ticket must own it."
                ),
            ))

    return findings


def extract_executables(cmd: str) -> list[str]:
    """Extract the executables a shell command would invoke.

    Handles ``&&``/``||``/``;``/pipe separation, leading ``NAME=value``
    environment assignments, and package runners (``npx``, ``pnpm exec``,
    ``uv run`` …) where the interesting tool is a later token.

    Args:
        cmd: A ``verify_cmd`` string.

    Returns:
        Executable names in order of appearance, without duplicates.
    """
    if not cmd or not cmd.strip():
        return []

    found: list[str] = []
    for segment in _SPLIT_RE.split(cmd):
        tokens = [t for t in segment.strip().split() if t]
        idx = 0
        while idx < len(tokens) and _ENV_ASSIGN_RE.match(tokens[idx]):
            idx += 1
        if idx >= len(tokens):
            continue

        exe = tokens[idx].strip("\"'")
        exe = Path(exe).name if "/" in exe else exe
        if not exe:
            continue

        if exe in _RUNNERS:
            probe = idx + 1
            while probe < len(tokens):
                nxt = tokens[probe].strip("\"'")
                if nxt in _RUNNER_SUBCOMMANDS or nxt.startswith("-"):
                    probe += 1
                    continue
                exe = Path(nxt).name if "/" in nxt else nxt
                break

        if exe and exe not in found:
            found.append(exe)

    return found


def _tools_from_package_json(repo_path: Path) -> set[str]:
    """Collect dependency and script names from ``package.json``."""
    pkg = repo_path / "package.json"
    if not pkg.is_file():
        return set()
    try:
        data = json.loads(pkg.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.debug("interface_contract: cannot read package.json: %s", exc)
        return set()
    if not isinstance(data, dict):
        return set()

    names: set[str] = set()
    for key in ("dependencies", "devDependencies", "optionalDependencies", "scripts"):
        section = data.get(key)
        if isinstance(section, dict):
            for entry in section:
                names.add(entry)
                if "/" in entry:
                    names.add(entry.rsplit("/", 1)[-1])
    return names


def _requirement_name(spec: str) -> str:
    """Return the distribution name from a PEP 508 requirement string."""
    match = re.match(r"[A-Za-z0-9._-]+", (spec or "").strip())
    return match.group(0).lower() if match else ""


def _tools_from_pyproject(repo_path: Path) -> set[str]:
    """Collect distribution and console-script names from ``pyproject.toml``.

    Parses the file as TOML when available (Python 3.11+), falling back to a
    regex scan so Python 3.10 — the project's declared minimum — still works.
    """
    pyproject = repo_path / "pyproject.toml"
    if not pyproject.is_file():
        return set()

    names: set[str] = set()

    try:
        import tomllib

        with pyproject.open("rb") as handle:
            data = tomllib.load(handle)
    except Exception as exc:  # noqa: BLE001 - missing module or malformed TOML
        logger.debug("interface_contract: TOML parse unavailable/failed: %s", exc)
        data = None

    if isinstance(data, dict):
        project = data.get("project")
        if isinstance(project, dict):
            specs: list[Any] = list(project.get("dependencies") or [])
            optional = project.get("optional-dependencies")
            if isinstance(optional, dict):
                for group in optional.values():
                    specs.extend(group or [])
            for spec in specs:
                if isinstance(spec, str):
                    name = _requirement_name(spec)
                    if name:
                        names.add(name)
            scripts = project.get("scripts")
            if isinstance(scripts, dict):
                names.update(str(key).lower() for key in scripts)
        return names

    try:
        raw_text = pyproject.read_text(encoding="utf-8")
    except OSError as exc:
        logger.debug("interface_contract: cannot read pyproject.toml: %s", exc)
        return names

    for quoted in re.findall(r"[\"']([A-Za-z0-9._-]+[^\"']*)[\"']", raw_text):
        if re.match(r"^[A-Za-z0-9._-]+\s*(?:[><=~!]|\[|$)", quoted):
            name = _requirement_name(quoted)
            if name:
                names.add(name)
    for match in re.finditer(r"^\s*([A-Za-z0-9._-]+)\s*=\s*[\"'][^\"']*:[^\"']*[\"']", raw_text, re.M):
        names.add(match.group(1).lower())
    return names


def collect_declared_tools(repo_path: Path | None) -> set[str]:
    """Collect every tool name the project declares.

    Args:
        repo_path: Repository root, or ``None`` to skip manifest inspection.

    Returns:
        Lower-cased names from ``package.json`` and ``pyproject.toml``.
    """
    if repo_path is None:
        return set()
    repo_path = Path(repo_path)
    if not repo_path.is_dir():
        return set()
    declared = _tools_from_package_json(repo_path) | _tools_from_pyproject(repo_path)
    return {name.lower() for name in declared}


def check_verify_cmd_tools(
    raw: dict[str, Any],
    repo_path: Path | None = None,
) -> list[InterfaceFinding]:
    """IC-002 — warn when a ``verify_cmd`` names an undeclared executable.

    A tool resolves when it is a baseline POSIX/dev tool, appears in the
    project's declared dependencies, or is present on ``PATH``.

    Args:
        raw: Parsed plan JSON.
        repo_path: Repository root used to read dependency manifests.  When
            ``None``, only baseline tools and ``PATH`` are consulted.

    Returns:
        One warning per (ticket, unresolved tool) pair.
    """
    declared = collect_declared_tools(repo_path)
    findings: list[InterfaceFinding] = []

    for ticket in _iter_tickets(raw):
        tid = str(ticket.get("ticket_id", "?"))
        cmd = ticket.get("verify_cmd")
        if not isinstance(cmd, str) or not cmd.strip():
            continue

        for exe in extract_executables(cmd):
            lowered = exe.lower()
            if lowered in _BASELINE_TOOLS or lowered in declared:
                continue
            if shutil.which(exe):
                continue
            findings.append(InterfaceFinding(
                rule_id="IC-002",
                severity="warning",
                ticket_ids=[tid],
                detail=(
                    f"verify_cmd invokes {exe!r}, which is not a baseline tool, "
                    f"not in the project's declared dependencies, and not on "
                    f"PATH. This ticket cannot pass until {exe!r} is declared."
                ),
            ))

    return findings


def check_interface_contract(
    raw: dict[str, Any],
    repo_path: Path | None = None,
) -> list[InterfaceFinding]:
    """Run every interface-contract check over a plan.

    Args:
        raw: Parsed plan JSON.
        repo_path: Repository root for dependency resolution, or ``None``.

    Returns:
        IC-001 findings followed by IC-002 findings.
    """
    return check_duplicate_provides(raw) + check_verify_cmd_tools(raw, repo_path)
