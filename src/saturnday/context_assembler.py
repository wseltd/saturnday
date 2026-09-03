"""Read engineering docs and build system/user prompts per ticket.

This is the key module that ensures the AI coder "reads the standards"
before coding each ticket.  The system prompt is built once (cached),
and the user prompt is built per ticket with goal + scope + project state.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

from saturnday._types import TicketSpec

logger = logging.getLogger(__name__)

# File extensions that contain standards/rules (not tooling scripts)
_STANDARDS_EXTENSIONS = frozenset({".txt", ".md", ".yaml", ".yml"})

# System prompt file (loaded separately, not as a standards doc)
_SYSTEM_PROMPT_FILE = "prompts/coder_system_senior_v1.txt"
_JUDGMENT_RULES_FILE = "docs/style/senior_judgment_rules.md"

# Files that should not be included in the coder's standards corpus
_SKIP_FILES = {
    _SYSTEM_PROMPT_FILE,
    _JUDGMENT_RULES_FILE,
    "prompts/reviewer_system_senior_v1.txt",  # Reviewer persona, not coder instructions
}
# Directories whose contents are data files, not coding standards
_SKIP_DIRS = {"corpus", "evals"}

# Python-only standards — skipped for non-Python projects
_PYTHON_ONLY_FILES = {"senior_python_standards.md"}


@lru_cache(maxsize=4)
def load_standards_context(standards_dir: str, *, project_languages: frozenset[str] | None = None) -> str:
    """Read and concatenate all engineering standards documents.

    Walks the entire standards directory and loads every standards file
    (txt, md, yaml). Skips .py files (those are tooling, not standards
    the coder needs to follow). Skips Python-only standards for
    non-Python projects.

    Args:
        standards_dir: Path to the directory containing standards files.
        project_languages: Detected languages in the project (e.g.
            ``frozenset({"python", "typescript"})``).  When provided,
            Python-only standards are skipped if ``"python"`` is not
            in the set.

    Returns:
        Concatenated standards text with section headers.
    """
    standards_path = Path(standards_dir)
    if not standards_path.is_dir():
        logger.warning("Standards directory not found: %s", standards_dir)
        return ""

    skip_python_only = (
        project_languages is not None
        and "python" not in project_languages
    )

    sections: list[str] = []

    for full_path in sorted(standards_path.rglob("*")):
        if not full_path.is_file():
            continue
        if full_path.suffix not in _STANDARDS_EXTENSIONS:
            continue
        rel_path = full_path.relative_to(standards_path)
        # Skip non-standards directories (data files, evaluation rubrics, evidence)
        if any(part in _SKIP_DIRS or part.startswith(".saturnday") for part in rel_path.parts):
            continue
        # Skip specific non-coder files (reviewer persona, system prompt loaded separately)
        if str(rel_path) in _SKIP_FILES:
            continue
        # Skip Python-only standards for non-Python projects
        if skip_python_only and rel_path.name in _PYTHON_ONLY_FILES:
            logger.debug("Skipping Python-only standards %s for non-Python project", rel_path)
            continue
        try:
            content = full_path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError as exc:
            logger.warning("Failed to read standards file %s: %s", full_path, exc)
            continue
        if content:
            sections.append(f"=== {rel_path} ===\n{content}")
            logger.debug("Loaded standards file: %s (%d chars)", rel_path, len(content))

    if not sections:
        logger.warning("No standards files found in %s", standards_dir)
        return ""

    logger.info("Loaded %d standards files from %s", len(sections), standards_dir)
    return "\n\n".join(sections)


@lru_cache(maxsize=4)
def build_system_prompt(standards_dir: str, project_languages: frozenset[str] | None = None) -> str:
    """Build the full system prompt: coder personality + engineering standards.

    Args:
        standards_dir: Path to the directory containing standards and prompts.
        project_languages: Detected languages (e.g. ``frozenset({"python"})``).
            Python-only standards are skipped for non-Python projects.

    Returns:
        Complete system prompt string.
    """
    standards_path = Path(standards_dir)
    prompt_path = standards_path / _SYSTEM_PROMPT_FILE

    # Load coder system prompt
    coder_prompt = ""
    if prompt_path.exists():
        try:
            coder_prompt = prompt_path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError as exc:
            logger.warning("Failed to read system prompt %s: %s", prompt_path, exc)

    # Load standards context (language-filtered)
    standards = load_standards_context(standards_dir, project_languages=project_languages)

    parts: list[str] = []
    if coder_prompt:
        parts.append(coder_prompt)
    if standards:
        parts.append(
            "ENGINEERING STANDARDS (follow these exactly):\n\n" + standards
        )

    # Load senior judgment rules separately if available
    judgment_rules_path = standards_path / "docs" / "style" / "senior_judgment_rules.md"
    if judgment_rules_path.is_file():
        try:
            judgment_text = judgment_rules_path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError as exc:
            logger.warning("Failed to read senior judgment rules: %s", exc)
        else:
            if judgment_text:
                parts.append(
                    "CRITICAL SENIOR JUDGMENT RULES:\n\n" + judgment_text
                )

    # Hard rules that override LLM hallucinations
    parts.append(
        "MANDATORY BUILD RULES:\n"
        '- pyproject.toml build-backend MUST be "setuptools.build_meta" — '
        "NEVER use setuptools.backends._legacy:_Backend (it does not exist).\n"
        '- [build-system] requires = ["setuptools>=68.0"]\n'
        '- build-backend = "setuptools.build_meta"'
    )

    parts.append(
        "COMMON MISTAKES TO AVOID:\n"
        "1. Centralize domain constants — never duplicate across modules\n"
        "2. README needs Trade-offs, Limitations, Non-goals sections\n"
        "3. Doc/config units must match code units\n"
        "4. Substring blacklist = keyword filter, not safety layer\n"
        "5. Expensive resources created once, not per-request\n"
        "6. except Exception: pass banned — log at WARNING and continue\n"
        "7. Tests must cover risk surface, not just shape\n"
        "8. Include .gitignore, never commit agent artifacts"
    )

    return "\n\n".join(parts)


def write_standards_file(
    standards_dir: str,
    output_path: Path,
    project_languages: frozenset[str] | None = None,
) -> str:
    """Write full engineering standards to a file and return a short system prompt.

    This is the CLI-backend entry point for standards delivery.  The full
    standards corpus (built by :func:`build_system_prompt`) is written to
    ``output_path`` so that a CLI agent can read it on disk.  A compact
    system prompt (~1500 chars) is returned for the API call; it references
    the written file and embeds the two sections that are too critical to
    defer to a file read.

    Args:
        standards_dir: Path to the directory containing standards and prompts.
        output_path: Destination path for the standards file (e.g.
            ``repo/.saturnday/standards.md``).
        project_languages: Detected languages in the project.  Forwarded
            to :func:`build_system_prompt` for language-filtered loading.

    Returns:
        Short system prompt string that references the written file and
        contains ``MANDATORY BUILD RULES`` and ``COMMON MISTAKES TO AVOID``
        inline.
    """
    full_content = build_system_prompt(standards_dir, project_languages)

    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(full_content, encoding="utf-8")
        logger.info("Wrote standards file (%d chars) to %s", len(full_content), output_path)
    except OSError as exc:
        logger.warning("Failed to write standards file %s: %s", output_path, exc)

    short_prompt = (
        "You are a senior software engineer in a governed Saturnday run.\n\n"
        "CRITICAL — DO NOT SKIP THIS STEP:\n"
        "You MUST read the file .saturnday/standards.md BEFORE writing any code.\n"
        "It contains all engineering standards and coding rules you must follow.\n"
        "If you do NOT read this file, your code WILL fail governance checks and\n"
        "be rejected. Every rule in that file is mechanically enforced. Skipping it\n"
        "wastes your attempt and forces a retry. Read it first.\n\n"
        "MANDATORY BUILD RULES:\n"
        '- pyproject.toml build-backend MUST be "setuptools.build_meta" — '
        "NEVER use setuptools.backends._legacy:_Backend (it does not exist).\n"
        '- [build-system] requires = ["setuptools>=68.0"]\n'
        '- build-backend = "setuptools.build_meta"\n\n'
        "COMMON MISTAKES TO AVOID:\n"
        "1. Centralize domain constants — never duplicate across modules\n"
        "2. README needs Trade-offs, Limitations, Non-goals sections\n"
        "3. Doc/config units must match code units\n"
        "4. Substring blacklist = keyword filter, not safety layer\n"
        "5. Expensive resources created once, not per-request\n"
        "6. except Exception: pass banned — log at WARNING and continue\n"
        "7. Tests must cover risk surface, not just shape\n"
        "8. Include .gitignore, never commit agent artifacts"
    )
    return short_prompt


def build_ticket_prompt(
    ticket: TicketSpec,
    project_state_summary: str,
    plan_notes: str,
    *,
    cli_mode: bool = False,
) -> str:
    """Build the user prompt for a specific ticket.

    Args:
        ticket: The ticket to code.
        project_state_summary: Text summary of what already exists.
        plan_notes: Free-form notes from the project plan.
        cli_mode: If True, prompt for direct file writing (CLI agent backends).
            If False, prompt for FILE block output (API backends).

    Returns:
        Complete user prompt string.
    """
    parts: list[str] = []

    # Plan notes (project-level constraints)
    if plan_notes:
        parts.append(f"PROJECT NOTES:\n{plan_notes}")

    # Project state context
    if project_state_summary:
        parts.append(project_state_summary)

    # Ticket goal
    parts.append(f"TICKET: {ticket.ticket_id}\n\nGOAL:\n{ticket.goal}")

    # Scope constraints
    scope_lines: list[str] = []
    if ticket.scope.allowed_globs != ("**",):
        scope_lines.append(f"Allowed files: {', '.join(ticket.scope.allowed_globs)}")
    if ticket.scope.forbidden_globs:
        scope_lines.append(f"Forbidden files: {', '.join(ticket.scope.forbidden_globs)}")
    scope_lines.append(f"Max files changed: {ticket.scope.max_files_changed}")
    if scope_lines:
        parts.append("SCOPE:\n" + "\n".join(scope_lines))

    # Out of scope — explicit non-goals and forbidden patterns
    if ticket.out_of_scope:
        nopes = "\n".join(f"- {n}" for n in ticket.out_of_scope)
        parts.append(f"OUT OF SCOPE — do NOT build these:\n{nopes}")

    # Acceptance criteria
    if ticket.acceptance_criteria:
        criteria = "\n".join(f"- {c}" for c in ticket.acceptance_criteria)
        parts.append(f"ACCEPTANCE CRITERIA:\n{criteria}")

    # Constraint restatement — force the coder to acknowledge constraints
    parts.append(
        "BEFORE YOU CODE — restate in your first line of output:\n"
        "1. What you are building (one sentence)\n"
        "2. What you are deliberately NOT building\n"
        "3. Where you will spend the most effort and why\n"
        "4. What you will keep simple\n"
        "Then proceed with implementation."
    )

    # Verify command
    if ticket.verify_cmd:
        parts.append(f"VERIFY COMMAND:\n{ticket.verify_cmd}")

    # Output instructions depend on backend type
    if cli_mode:
        parts.append(
            "INSTRUCTIONS:\n"
            "You are working directly in the repository. "
            "Create and write all required files now. "
            "Do not explain — just write the code."
        )
    else:
        parts.append(
            "MANDATORY OUTPUT FORMAT — you MUST follow this exactly:\n"
            "Output ONLY file blocks. No explanations, no markdown, no commentary.\n"
            "Each file must use this exact format:\n\n"
            "FILE: path/to/file.py\n"
            "<full file contents>\n"
            "END FILE\n\n"
            "Every file mentioned in the ticket must appear as a FILE block.\n"
            "Do NOT wrap output in ```code fences```.\n\n"
            "REMINDER: Output ONLY raw FILE blocks. No markdown. No explanations."
        )

    return "\n\n".join(parts)


def assemble_messages(
    system: str,
    user: str,
    repair_context: str | None = None,
) -> list[dict[str, str]]:
    """Assemble OpenAI-format messages for the coder API.

    Args:
        system: System prompt (standards + personality).
        user: User prompt (ticket + context).
        repair_context: Optional governance failure context for repair attempts.

    Returns:
        List of message dicts with ``role`` and ``content`` keys.
    """
    messages: list[dict[str, str]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]

    if repair_context:
        messages.append({"role": "assistant", "content": "I'll fix the issues."})
        messages.append({
            "role": "user",
            "content": (
                "The previous attempt failed governance. Fix ALL issues below "
                "and produce corrected FILE blocks.\n\n"
                f"GOVERNANCE FINDINGS:\n{repair_context}"
            ),
        })

    return messages
