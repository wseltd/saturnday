"""Interactive guided workflow for saturnday start — persistent REPL."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from saturnday import capability_registry

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Context and backend detection
# ---------------------------------------------------------------------------

def detect_context(repo_path: Path) -> dict[str, Any]:
    """Detect repo context: git status, SKILL.md, branch.

    Args:
        repo_path: Path to the repository root.

    Returns:
        Dictionary with keys: repo_path, skill_md, git_initialized,
        git_branch, git_clean, saturnday_version.
    """
    ctx: dict[str, Any] = {
        "repo_path": str(repo_path.resolve()),
        "skill_md": (repo_path / "SKILL.md").is_file(),
        "git_initialized": (repo_path / ".git").is_dir(),
        "git_branch": "",
        "git_clean": True,
    }
    if ctx["git_initialized"]:
        try:
            r = subprocess.run(
                ["git", "-C", str(repo_path), "branch", "--show-current"],
                capture_output=True, text=True, timeout=5,
            )
            ctx["git_branch"] = r.stdout.strip()
        except (subprocess.SubprocessError, OSError):
            pass
        try:
            r = subprocess.run(
                ["git", "-C", str(repo_path), "status", "--porcelain"],
                capture_output=True, text=True, timeout=5,
            )
            ctx["git_clean"] = len(r.stdout.strip()) == 0
        except (subprocess.SubprocessError, OSError):
            pass
    try:
        from saturnday.version import __version__
        ctx["saturnday_version"] = __version__
    except ImportError:
        ctx["saturnday_version"] = "unknown"
    return ctx


_LOCAL_120B_MODEL = "openai/gpt-oss-120b"


def _probe_local_vllm(port: int = 8000) -> bool:
    """Return True if the GPT-OSS 120B vLLM server is running on localhost:{port}.

    Fetches /v1/models and checks that ``openai/gpt-oss-120b`` is in the list.
    A server running a *different* model (e.g. a stale llama session) returns
    False — prevents stale cached sessions from misfiring against the wrong model.

    Args:
        port: TCP port to probe (default 8000).

    Returns:
        ``True`` only if the server is up AND serving the correct model.
    """
    import json as _json
    import urllib.request
    import urllib.error
    try:
        with urllib.request.urlopen(
            f"http://localhost:{port}/v1/models", timeout=3
        ) as resp:
            data = _json.loads(resp.read().decode())
            model_ids = [m.get("id", "") for m in data.get("data", [])]
            return _LOCAL_120B_MODEL in model_ids
    except Exception:
        return False


def _probe_cli_binary(binary: str) -> str:
    """Run ``binary --version`` and return a human-readable reason string.

    Args:
        binary: Name of the CLI binary to probe (must already be on PATH).

    Returns:
        Reason string describing the probe outcome.
    """
    try:
        r = subprocess.run(
            [binary, "--version"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0:
            return "installed and responsive"
        return f"installed but returned error (run '{binary} login' if needed)"
    except (subprocess.SubprocessError, OSError):
        return "installed but not responding"


def detect_backends() -> list[dict[str, Any]]:
    """Detect available coder backends.

    For CLI backends, also probes ``--version`` to distinguish a binary
    that exists from one that actually responds.  API backends are detected
    purely by environment variable presence.

    Returns:
        List of dicts with keys: name, available, reason.
    """
    backends: list[dict[str, Any]] = []
    # CLI backends
    for name, binary in [("codex-cli", "codex"), ("claude-cli", "claude"), ("openclaude", "openclaude"), ("cursor-cli", "agent")]:
        found = shutil.which(binary) is not None
        if found:
            reason = _probe_cli_binary(binary)
        else:
            reason = f"'{binary}' not found on PATH"
        backends.append({
            "name": name,
            "available": found,
            "reason": reason,
        })
    # API backends
    for name, env_var in [("openai", "OPENAI_API_KEY"), ("anthropic", "ANTHROPIC_API_KEY")]:
        has_key = bool(os.environ.get(env_var))
        backends.append({
            "name": name,
            "available": has_key,
            "reason": "API key set" if has_key else f"no {env_var}",
        })
    return backends


# ---------------------------------------------------------------------------
# Intent classification
# ---------------------------------------------------------------------------

_GUARD_KEYWORDS = r'\b(scan|check|lint|governance|findings|audit|review|inspect|analyse|analyze|examine)\b'
_PLAN_RUN_KEYWORDS = r'\b(build|create|implement|add|make|generate|write|develop)\b'
_REPAIR_KEYWORDS = r'\b(fix|repair|patch|remediate|resolve)\b'
_STATUS_KEYWORDS = r'\b(status|evidence|what happened|last run|summary|progress)\b'
_PUBLISH_KEYWORDS = r'\b(publish|release|ship|deploy)\b'


def classify_intent(line: str) -> str:
    """Classify user input into an action intent.

    Args:
        line: Raw input line from the user.

    Returns:
        One of: ``'empty'``, ``'slash'``, ``'help'``, ``'guard'``,
        ``'plan_run'``, ``'repair'``, ``'status'``, ``'publish'``,
        ``'unknown'``.
    """
    stripped = line.strip()
    if not stripped:
        return "empty"
    if stripped.startswith("/"):
        return "slash"
    lower = stripped.lower()
    # Bare help shortcut checked before keyword regexes.
    if lower in ("help", "?", "h"):
        return "help"
    # Repair verbs checked first: "fix the findings" is repair, not guard.
    if re.search(_REPAIR_KEYWORDS, lower):
        return "repair"
    if re.search(_GUARD_KEYWORDS, lower):
        return "guard"
    if re.search(_PLAN_RUN_KEYWORDS, lower):
        return "plan_run"
    if re.search(_STATUS_KEYWORDS, lower):
        return "status"
    if re.search(_PUBLISH_KEYWORDS, lower):
        return "publish"
    return "unknown"


def parse_slash(line: str) -> tuple[str, str]:
    """Parse a slash command into (command, args).

    Args:
        line: Raw input line beginning with ``/``.

    Returns:
        Tuple of ``(command, remainder_args)``.  If the command is not
        recognised, returns ``('unknown', line)``.
    """
    parts = line.strip().lstrip("/").split(None, 1)
    cmd = parts[0].lower() if parts else ""
    arg = parts[1] if len(parts) > 1 else ""
    known = {
        "guard", "scan", "check", "plan", "run", "repair",
        "status", "resume", "quit", "exit", "help",
    }
    if cmd in known:
        return (cmd, arg)
    return ("unknown", line)


# ---------------------------------------------------------------------------
# Session persistence
# ---------------------------------------------------------------------------

@dataclass
class Session:
    """Persistent session state written to .saturnday/session.json."""

    repo_path: str = ""
    backend: str | None = None
    last_plan_path: str | None = None
    last_evidence_dir: str | None = None
    last_disposition: str | None = None
    started_at: str = ""
    agent_governance_enabled: bool = False
    agent_governance_scope: str = ""  # "repo" | "session" | ""
    governed_agents: list = field(default_factory=list)
    # I.4: propagated from `saturnday start --work-branch` into the
    # guided REPL's downstream run calls.  ``None`` when the flag
    # was not passed.
    work_branch: str | None = None


def save_session(session: Session, repo_path: Path) -> Path:
    """Persist session to .saturnday/session.json.

    Args:
        session: Session dataclass to serialise.
        repo_path: Repository root directory.

    Returns:
        Path to the written session file.
    """
    sat_dir = repo_path / ".saturnday"
    sat_dir.mkdir(parents=True, exist_ok=True)
    path = sat_dir / "session.json"
    path.write_text(json.dumps(asdict(session), indent=2), encoding="utf-8")
    return path


def load_session(repo_path: Path) -> Session | None:
    """Load session from .saturnday/session.json if present.

    Args:
        repo_path: Repository root directory.

    Returns:
        :class:`Session` if found and parseable, otherwise ``None``.
    """
    path = repo_path / ".saturnday" / "session.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return Session(**{k: v for k, v in data.items() if k in Session.__dataclass_fields__})
    except (json.JSONDecodeError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Header and help
# ---------------------------------------------------------------------------

def print_header(context: dict[str, Any]) -> None:
    """Print compact one-liner header for the REPL.

    Args:
        context: Output of :func:`detect_context`.
    """
    v = context.get("saturnday_version", "?")
    repo = context.get("repo_path", ".")
    if context.get("git_initialized"):
        branch = context.get("git_branch", "?")
        clean = "clean" if context.get("git_clean") else "dirty"
        git_str = f"Git: {clean}, {branch}"
    else:
        git_str = "Git: none"

    print(f"\n  Saturnday {v}")
    print(f"  {repo}  |  {git_str}")
    print()


def _print_help() -> None:
    """Print REPL help text."""
    print()
    print("  Natural language:")
    print("    'build a calculator skill'   Plan and run a governed project")
    print("    'scan this skill'            Run Guard checks")
    print("    'check the last 3 commits'   Governance check on a diff")
    print("    'fix the findings'           Repair mode")
    print("    'what happened'              Show last run status")
    print()
    print("  Slash commands:")
    print("    /guard      Run Guard (scan or check)")
    print("    /plan <goal> Generate a plan")
    print("    /run        Run the current plan")
    print("    /repair     Fix findings")
    print("    /resume     Resume last run")
    print("    /status     Show evidence and status")
    print("    /help       This help")
    print("    /quit       Exit")
    print()


# ---------------------------------------------------------------------------
# Conversational pass-through to the coder
# ---------------------------------------------------------------------------

# Conversation history shared across the REPL session
_conversation_history: list[dict[str, str]] = []

_SATURNDAY_SYSTEM_PROMPT = """\
You are an AI coding assistant inside Saturnday, a governed terminal-first \
execution system for AI-built software. You are chatting with the developer \
in the saturnday> REPL.

Context:
- Repo: {repo_path}
- You can help with questions about the repo, findings, errors, and next steps.
- If the user wants to scan, build, repair, or check, tell them what to type \
(e.g. "scan this repo", "fix the findings", "build a calculator skill").
- If the user shares an error (like a pip install failure), help them fix it.
- Be concise and direct. You are a terminal tool, not a chatbot.
- Do not make up information about the repo you haven't seen.
"""

_FREE_CHAT_SYSTEM_PROMPT = """\
You are a helpful, knowledgeable assistant. The user is working in the \
directory: {repo_path}. Answer their questions directly and naturally. \
You can discuss code, debug problems, explain concepts, or talk about \
anything else. Be genuine, thoughtful, and conversational.
"""


def _handle_conversation(
    user_msg: str, repo_path: Path, session: "Session | None"
) -> None:
    """Forward a message to the coder backend for a conversational response.

    Uses the session's backend if set, otherwise tries claude-cli or falls
    back to a simple help message.
    """
    backend = session.backend if session and session.backend else None

    # If no backend set yet, try to detect one
    if not backend:
        for name in ("claude-cli", "codex-cli"):
            ready, _ = _check_backend_ready(name)
            if ready:
                backend = name
                break

    if not backend:
        # No backend available — show help
        print()
        print("  No coder backend available for conversation.")
        print("  Set one up first, then I can chat with you.")
        print("  Type /help for available commands.")
        print()
        return

    _conversation_history.append({"role": "user", "content": user_msg})

    # local-120b: free-form chat — no Saturnday workflow restrictions
    if backend == "local-120b":
        system_prompt = _FREE_CHAT_SYSTEM_PROMPT.format(repo_path=repo_path)
    else:
        system_prompt = _SATURNDAY_SYSTEM_PROMPT.format(repo_path=repo_path)
    messages = [{"role": "system", "content": system_prompt}]
    # Keep last 10 turns to avoid token limits
    messages.extend(_conversation_history[-20:])

    try:
        from saturnday.coder_adapter import call_coder
        config = _build_coder_config(backend)
        response = call_coder(config, messages, repo_path)
        if response:
            _conversation_history.append({"role": "assistant", "content": response})
            print()
            # Indent response for clean terminal display
            for resp_line in response.strip().splitlines():
                print(f"  {resp_line}")
            print()
    except Exception as exc:
        print(f"\n  Could not reach {backend}: {exc}")
        print("  Type /help for available commands.\n")


# ---------------------------------------------------------------------------
# Governance system prompt for coder injection
# ---------------------------------------------------------------------------

# The new governance prompt — clean, structured, with output contract
_GOVERNANCE_PROMPT_NEW = """\
You are operating inside Saturnday, a governed terminal-first execution system \
for AI-built software. All work in this repository must follow Saturnday governance.

Core objectives:
1. Complete the user's task correctly.
2. Keep work governed, auditable, and reviewable.
3. Prefer facts over claims. Do not invent results or say checks passed if you did not run them.

Working rules:
1. When asked to scan or review a repository, run:
   saturnday governance --repo . --full

2. Before any commit, run:
   saturnday governance --repo . --staged

3. A pre-commit hook may block commits that fail governance. If blocked, fix the findings and retry.

4. When asked to BUILD a new project or feature from scratch, ALWAYS use the governed pipeline:
   a. BEFORE running `saturnday plan`, tell the user IN CHAT the clarification answers you are about to use.
      Print a short block like:

        I'll plan this with:
          operating_mode:      cli_tool
          dependency_profile:  self_contained
          testing_strategy:    unspecified
        Accept these or correct me.

      Wait for the user to confirm or correct.  Then run:

        saturnday plan --brief "description" --repo . --backend {backend} \\
          --clarify mode_ambiguous="<library | cli_tool | web_service | worker | pipeline | frontend | storage_only>" \\
          --clarify integration_scope_unclear="<self_contained | local_dependencies | external_dependencies>" \\
          --clarify testing_strategy_unspecified="<unspecified | live_credentials_gated | local_emulator | oss_substitute | generated_fake | seeded_demo | recorded_fixture>"

      IMPORTANT: pass every relevant --clarify flag on the FIRST call.  The operator should never see a
      "Clarifications needed" message — that is a signal you forgot a flag.
   b. saturnday run --plan .saturnday/plan.json --repo . --backend {backend}
   Do NOT code new projects directly. The pipeline generates design-decision-rich tickets, \
runs a simplifier pass, executes role passes, and produces a full run report.
   If the user wants to manually review and approve the Definition of Done before the run, \
tell them to run this in their own terminal: ! saturnday plan-confirm --brief "..." --repo . --backend {backend}

5. Do NOT edit files directly. All changes must go through Saturnday:
   - New projects: saturnday plan + saturnday run
   - Bug fixes and small changes: saturnday repair
   - Governance findings: saturnday repair
   Every change must be governed, evidence-tracked, and auditable.

7. If governance findings exist, repair them with Saturnday repair for the repository or skill as appropriate. \
Do not use saturnday rerun-failed for repair work.

8. If a finding reflects the intended core behaviour of the project rather than a defect, \
exempt it in .saturnday-policy.yaml instead of forcing a misleading code change.

9. If saturnday repair finishes with failed tickets, inspect the repair report in the evidence directory. Then either \
exempt legitimate expected findings in .saturnday-policy.yaml, or run Saturnday repair again.

10. After repair passes are complete, verify final state with:
    saturnday governance --repo . --full

11. Evidence is recorded automatically by Saturnday commands.

12. In new repositories, install the governance hook:
    saturnday hook install

13. If required tools or packages are missing, stop and tell the user exactly what to install, \
using copy-paste shell commands.

Engineering standards:
1. Follow the engineering standards loaded into your system and repository context.
2. Avoid dead code, duplicated logic, shallow fixes, misleading comments, weak tests, and documentation drift.
3. Keep changes minimal, correct, and maintainable.
4. Do not trade real correctness for superficial governance compliance.
5. Do NOT browse other repositories or projects on this machine for reference. \
Build from the brief, your training, and the governance rules. \
Copying structure from existing repos produces template-shaped code.

Reports and evidence:
- Governance report: .saturnday/governance-report.md
- Repair report: .saturnday/repair-report.md
- Additional evidence: relevant Saturnday evidence directory

Output contract — when work completes, always report:
1. What you changed.
2. Which Saturnday commands you ran.
3. Whether governance passed.
4. Where the reports were written.
5. Any unresolved findings, exemptions, or blockers.

Style:
Talk naturally. Be direct and concise. Verify current branch, git state, backend, \
dependencies, and repository context in the current session rather than assuming them. \
Do not add Co-Authored-By or any co-author trailers to commit messages.
"""

# Command reference — appended to CLAUDE.md/AGENTS.md but kept out of the
# system prompt to reduce noise
_COMMAND_REFERENCE = """\

Saturnday commands:
  saturnday governance --repo . --full                  Full governance review of ALL tracked files
  saturnday governance --repo .                         Check last commit (diff-based)
  saturnday governance --repo . --diff <range>          Check a specific diff range
  saturnday governance --repo . --staged                Check staged changes before commit
  saturnday scan --skill . --output scan.json           Scan an OpenClaw skill
  saturnday check --repo . --diff <range>               Governance check on a diff
  saturnday repair --repo . --backend {backend}         Repair findings in any repo
  saturnday repair --skill . --backend {backend}        Repair findings in an OpenClaw skill
  saturnday repair --repo . --dry-run                   Preview repair plan without executing
  saturnday plan --brief "description" --repo . --backend {backend}    Generate a plan
  saturnday plan-confirm --brief "description" --repo . --backend {backend}    Generate plan + DoD approval gate (user must run in their own terminal)
  saturnday run --plan .saturnday/plan.json --repo . --backend {backend}          Execute a plan
  saturnday resume --output-dir <dir>                   Resume a stopped run
  saturnday rerun-failed --output-dir <dir> --plan .saturnday/plan.json --repo . --backend {backend}    Re-run failed tickets
  saturnday validate-plan --plan .saturnday/plan.json              Validate a plan file
  saturnday explain-failure --output-dir <dir>          Remediation report for a failed run
  saturnday baseline generate --repo .                  Create a ratchet baseline
  saturnday baseline compare --repo . --baseline .saturnday-baseline.json    Compare against baseline
  saturnday publish-preflight --skill .                 Check if a skill is ready to publish
  saturnday hook install                                Install pre-commit governance hook
"""

# Thin system prompt — just points to the governance file
_THIN_SYSTEM_PROMPT = """\
This repository is governed by Saturnday. Follow the governance rules in {gov_file}. \
Use saturnday CLI commands for all governance actions. Backend: {backend}.
"""

# Legacy fallback — kept for safety net
_GOVERNANCE_PROMPT = _GOVERNANCE_PROMPT_NEW


# ---------------------------------------------------------------------------
# REPL loop — launches coder directly or falls back to built-in REPL
# ---------------------------------------------------------------------------

_BRIEF_ENRICHMENT_PROMPT = (
    "You are a senior software architect. A user wants to build a project. "
    "Their brief may be vague or missing technical details.\n\n"
    "Before writing the enriched brief, reason through:\n"
    "1. What technical decisions does this brief leave open?\n"
    "2. What could go wrong if these aren't specified?\n"
    "3. What would a senior engineer ask before starting?\n\n"
    "Then produce a detailed technical spec that a project planner can turn "
    "into tickets. Include:\n"
    "- Programming language and framework\n"
    "- Data model (what entities, what relationships)\n"
    "- Key features and endpoints\n"
    "- What NOT to build (non-goals)\n"
    "- Testing expectations\n"
    "- Deployment approach (if applicable)\n"
    "- Constraints and limitations\n\n"
    "Keep the user's intent. Don't change what they want — fill in the "
    "technical details they didn't specify.\n"
    "Output ONLY the enriched brief. No explanations, no preamble.\n"
    "Max 500 words."
)


def _enrich_brief(goal: str, coder_config, repo_path: Path) -> str:
    """Enrich a vague brief into a detailed technical spec.

    Returns empty string if enrichment fails or brief is already detailed.
    """
    try:
        from saturnday.coder_adapter import call_coder
        messages = [
            {"role": "system", "content": _BRIEF_ENRICHMENT_PROMPT},
            {"role": "user", "content": f"User's brief:\n{goal}"},
        ]
        response = call_coder(coder_config, messages, repo_path)
        return response.strip()
    except Exception:
        return ""


_RELAXED_POLICY = """\
# Saturnday relaxed policy — skips publish-readiness checks.
# Delete this file when you're ready to enforce all checks.
expected_findings:
  - missing_license
  - missing_readme
  - readme_missing_section
  - missing_project_config
  - readme_language_mismatch
"""


def _offer_agent_governance(repo_path: Path, session: "Session") -> None:
    """Offer agent governance overlay for Claude agents (claude-cli only).

    Discovers project-local and user-level agents, then prompts the user
    whether to activate Saturnday's governance overlay.  The agent files
    themselves are never modified.

    Args:
        repo_path: Repository root directory.
        session: Active session that will receive the governance state.
    """
    # If repo-scope governance was already enabled in a prior session, respect it
    if session.agent_governance_enabled and session.agent_governance_scope == "repo":
        from saturnday.agent_discovery import discover_all_agents
        current_agents = discover_all_agents(repo_path)
        if current_agents:
            names = ", ".join(a["name"] for a in current_agents)
            print(f"\n  Agent governance: enabled (repo scope)")
            print(f"  Governed agents: {names}")
            # Update governed_agents list in case agents changed since last session
            session.governed_agents = [a["name"] for a in current_agents]
            return

    from saturnday.agent_discovery import discover_project_agents, discover_user_agents

    project_agents = discover_project_agents(repo_path)
    user_agents = discover_user_agents()

    if not project_agents and not user_agents:
        return

    print("\n  Claude agents found:")
    if project_agents:
        names = ", ".join(a["name"] for a in project_agents)
        print(f"    Project: {names} ({len(project_agents)} agents)")
    if user_agents:
        names = ", ".join(a["name"] for a in user_agents)
        print(f"    User: {names} ({len(user_agents)} agents)")

    print("\n  Would you like Saturnday to govern these agents?")
    print("    [1] Yes, for this repo (persists in .saturnday/session.json)")
    print("    [2] Yes, for this session only")
    print("    [3] No, skip agent governance")
    print()
    print("  Note: Your agent definitions will not be modified.")
    print("  Governance applies only within Saturnday's overlay.")

    try:
        choice = input("  Choice [1/2/3]: ").strip()
    except (EOFError, KeyboardInterrupt):
        choice = "3"

    all_names = [a["name"] for a in project_agents + user_agents]

    if choice == "1":
        session.agent_governance_enabled = True
        session.agent_governance_scope = "repo"
        session.governed_agents = all_names
        print("  Agent governance enabled for this repo.")
    elif choice == "2":
        session.agent_governance_enabled = True
        session.agent_governance_scope = "session"
        session.governed_agents = all_names
        print("  Agent governance enabled for this session.")
    else:
        print("  Agent governance skipped.")


def _offer_project_mode(repo_path: Path) -> None:
    """Ask the user whether to use strict or relaxed governance.

    Only prompts if no ``.saturnday-policy.yaml`` already exists.
    Relaxed mode writes a policy file that exempts publish-readiness
    checks (LICENSE, README sections, project config).  Useful for
    private repos and early-stage development.
    """
    policy_path = repo_path / ".saturnday-policy.yaml"
    if policy_path.exists():
        return

    print("\n  Project mode:")
    print("    [1] Strict  (all checks block commits)")
    print("    [2] Relaxed (skip README/LICENSE/project config checks)")
    try:
        choice = input("  Select [1-2]: ").strip()
    except (EOFError, KeyboardInterrupt):
        return
    if choice == "2":
        policy_path.write_text(_RELAXED_POLICY, encoding="utf-8")
        print(f"  Wrote {policy_path.name} — delete it when ready for strict mode.\n")


def repl_loop(repo_path: Path, work_branch: str | None = None) -> int:
    """Launch the governed coding workflow.

    For CLI backends (claude-cli, codex-cli), launches the coder directly
    with Saturnday governance injected via system prompt. The user talks
    to their coder naturally.

    For API backends or when no CLI is available, falls back to the
    built-in REPL with keyword-based routing.

    Args:
        repo_path: Repository root directory.
        work_branch: I.4 — opt-in work-branch name forwarded from
            ``saturnday start --work-branch``.  Stored on the session
            so every downstream ``guided_run`` / ``guided_repair`` call
            picks it up.

    Returns:
        Exit code (0 on normal exit, 1 on error).
    """
    context = detect_context(repo_path)
    print_header(context)

    # Version notification is now handled by cli.py main() via update_notifier

    session = load_session(repo_path) or Session(
        repo_path=str(repo_path),
        started_at=datetime.now(timezone.utc).isoformat(),
    )
    # I.4: record the work-branch preference for the session.  Only
    # overwrite when the operator explicitly passed the flag; otherwise
    # a persisted session value survives restart.
    if work_branch is not None:
        session.work_branch = work_branch

    # Session-scope governance is ephemeral — clear on restart
    if session.agent_governance_scope == "session":
        session.agent_governance_enabled = False
        session.agent_governance_scope = ""
        session.governed_agents = []

    # Try to launch the coder directly for CLI backends
    backend = select_backend(session)
    if backend and backend in ("claude-cli", "openclaude", "cursor-cli"):
        # FU-01 (launcher path): Ensure git is initialised before any launcher
        # side effects (policy-file write, coder launch).  Mirrors the identical
        # check in guided_run() which is only reachable via _fallback_repl().
        if not context["git_initialized"]:
            print("\n  This directory is not a git repository.")
            print("  Saturnday needs git to track changes and run governance checks.")
            try:
                _init = input("  Initialize git here? [Y/n] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                return 0
            if _init == "n":
                print("  Aborted. Run 'git init' manually if you want to proceed later.")
                return 0
            import subprocess as _sp_fu01
            _r = _sp_fu01.run(["git", "init", str(repo_path)], capture_output=True)
            if _r.returncode != 0:
                print("  Failed to initialize git. Is git installed?")
                print(f"  Error: {_r.stderr.decode(errors='replace').strip()}")
                return 1
            _sp_fu01.run(["git", "-C", str(repo_path), "config", "user.email", "saturnday@local"], capture_output=True)
            _sp_fu01.run(["git", "-C", str(repo_path), "config", "user.name", "Saturnday"], capture_output=True)
            _sp_fu01.run(["git", "-C", str(repo_path), "commit", "--allow-empty", "-m", "init"], capture_output=True)
            print("  Git initialized.\n")
            context["git_initialized"] = True
        # Offer relaxed mode if no policy file exists
        _offer_project_mode(repo_path)
        # Offer agent governance overlay (claude-cli only, not openclaude)
        if backend == "claude-cli":
            _offer_agent_governance(repo_path, session)
        save_session(session, repo_path)
        return _launch_coder(backend, repo_path, context)

    if backend == "codex-cli":
        # Codex-cli uses Saturnday's built-in REPL instead of an interactive
        # Codex session.  This avoids the recursive codex-in-codex nesting
        # that causes hangs and API quota competition.  Codex is used as a
        # silent coding engine — the user talks to Saturnday's prompt.
        _offer_project_mode(repo_path)
        session.backend = backend
        save_session(session, repo_path)
        print(
            "\n  Codex backend selected. You'll describe your project to Saturnday\n"
            "  and Codex will code each ticket silently in the background.\n"
            "  Ticket outcomes will stream here as they complete.\n"
        )
        return _fallback_repl(repo_path, context, session)

    # Fallback to built-in REPL for API backends or no backend
    return _fallback_repl(repo_path, context, session)


def _install_codex_wrappers(repo_path: Path) -> None:
    """Generate governed command wrappers in bin/ for Codex.

    Creates thin shell scripts that pre-configure the correct saturnday flags
    for common operations. Scripts are only written if they do not already exist,
    so user customisations are preserved on subsequent launches.

    Args:
        repo_path: Repository root directory where ``bin/`` will be created.
    """
    bin_dir = repo_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)

    wrappers = {
        "sat-scan": "#!/bin/sh\n# Saturnday: full governance scan\nsaturnday governance --repo . --full\n",
        "sat-scan-staged": "#!/bin/sh\n# Saturnday: check staged changes before commit\nsaturnday governance --repo . --staged\n",
        "sat-repair": "#!/bin/sh\n# Saturnday: governed repair\nsaturnday repair --repo . --backend codex-cli \"$@\"\n",
        "sat-repair-skill": "#!/bin/sh\n# Saturnday: governed skill repair\nsaturnday repair --skill . --backend codex-cli \"$@\"\n",
        "sat-plan": "#!/bin/sh\n# Saturnday: generate governed plan\nsaturnday plan --brief \"$1\" --repo . --backend codex-cli\n",
        "sat-run": '#!/bin/sh\n# Saturnday: execute governed plan\nsaturnday run --plan "$1" --repo . --backend codex-cli\n',
        "sat-commit": "#!/bin/sh\n# Saturnday: governed commit (checks staged changes first)\nsaturnday governance --repo . --staged && git commit \"$@\"\n",
    }

    for name, content in wrappers.items():
        path = bin_dir / name
        if not path.is_file():
            path.write_text(content, encoding="utf-8")
            path.chmod(0o755)


def _launch_coder(backend: str, repo_path: Path, context: dict[str, Any]) -> int:
    """Launch the coder CLI directly with Saturnday governance injected.

    Args:
        backend: ``"claude-cli"``, ``"codex-cli"``, or ``"cursor-cli"``.
        repo_path: Repository root directory.
        context: Output of :func:`detect_context`.

    Returns:
        Exit code from the coder process.
    """
    git_status = "clean" if context.get("git_clean") else "dirty"
    branch = context.get("git_branch", "?")
    if context.get("git_initialized"):
        git_str = f"{git_status}, {branch}"
    else:
        git_str = "not initialized"

    version = context.get("saturnday_version", "?")

    # Auto-install pre-commit governance hook if git is initialized
    if context.get("git_initialized"):
        hook_path = repo_path / ".git" / "hooks" / "pre-commit"
        if not hook_path.is_file() or "saturnday" not in hook_path.read_text(encoding="utf-8", errors="replace").lower():
            try:
                subprocess.run(
                    ["saturnday", "hook", "install", "--repo", str(repo_path)],
                    capture_output=True, timeout=5,
                )
                print("  Governance pre-commit hook installed.")
            except Exception:
                pass

    # Install Claude Code hooks for edit gating (claude-cli and openclaude both read CLAUDE.md)
    if backend in ("claude-cli", "openclaude"):
        try:
            from saturnday.hooks import install_claude_hooks
            hooks_path = install_claude_hooks(repo_path)
            if hooks_path:
                print(f"  Claude Code hooks installed: {hooks_path}")
        except Exception:
            pass

    # Install Codex command wrappers in bin/
    if backend == "codex-cli":
        try:
            _install_codex_wrappers(repo_path)
            print("  Command wrappers installed in bin/")
        except Exception:
            pass

    # Ensure Cursor rules directory exists for cursor-cli
    if backend == "cursor-cli":
        try:
            cursor_rules_dir = repo_path / ".cursor" / "rules"
            cursor_rules_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

    # Write governance rules to CLAUDE.md or AGENTS.md in the repo
    gov_content = _GOVERNANCE_PROMPT_NEW + _COMMAND_REFERENCE.format(backend=backend)
    if backend == "codex-cli":
        gov_content += """
Governed command wrappers (use these instead of raw saturnday commands):
  ./bin/sat-scan              Full governance scan
  ./bin/sat-scan-staged       Check staged changes before commit
  ./bin/sat-repair            Repair findings (governed)
  ./bin/sat-repair-skill      Repair OpenClaw skill findings
  ./bin/sat-plan "brief"      Generate a governed plan
  ./bin/sat-run .saturnday/plan.json     Execute a governed plan
  ./bin/sat-commit -m "msg"   Governed commit (scans staged first)

Prefer these wrappers over raw saturnday commands — they have the correct flags pre-configured.
"""
    if backend in ("claude-cli", "openclaude"):
        # openclaude is a Claude Code fork and reads the same CLAUDE.md file
        gov_file = repo_path / "CLAUDE.md"
    elif backend == "cursor-cli":
        gov_file = repo_path / ".cursor" / "rules" / "saturnday.md"
    else:
        gov_file = repo_path / "AGENTS.md"

    # Preserve existing content — append governance section if not already there
    existing = ""
    if gov_file.is_file():
        existing = gov_file.read_text(encoding="utf-8", errors="replace")
    if "Saturnday governance" not in existing:
        separator = "\n\n---\n\n" if existing.strip() else ""
        gov_file.write_text(
            existing + separator + gov_content,
            encoding="utf-8",
        )
        print(f"  Governance rules written to: {gov_file.name}")

    # Thin system prompt as fallback — points to the file
    thin_prompt = _THIN_SYSTEM_PROMPT.format(
        gov_file=gov_file.name,
        backend=backend,
    )

    # Full prompt as safety net — injected via system prompt in case
    # the coder doesn't read the governance file
    full_fallback = _GOVERNANCE_PROMPT_NEW.format() if "{" not in _GOVERNANCE_PROMPT_NEW else _GOVERNANCE_PROMPT_NEW

    if backend == "claude-cli":
        cmd = [
            "claude",
            "--append-system-prompt", thin_prompt + "\n\n" + full_fallback,
        ]
    elif backend == "openclaude":
        cmd = [
            "openclaude",
            "--append-system-prompt", thin_prompt + "\n\n" + full_fallback,
        ]
    elif backend == "codex-cli":
        # Use bypass mode so nested codex exec calls (from saturnday plan/run/repair)
        # can access the network. --full-auto sandbox blocks nested subprocess API access.
        cmd = [
            "codex",
            "--dangerously-bypass-approvals-and-sandbox",
            thin_prompt,
        ]
    elif backend == "cursor-cli":
        cmd = [
            "agent",
            thin_prompt,
        ]
    else:
        return 1

    print(f"  Launching {backend} with Saturnday governance...\n")

    try:
        result = subprocess.run(cmd, cwd=str(repo_path))
        return result.returncode
    except FileNotFoundError:
        print(f"  Error: '{cmd[0]}' not found on PATH.")
        return 1
    except KeyboardInterrupt:
        print("\n  Session ended.")
        return 0


def _fallback_repl(repo_path: Path, context: dict[str, Any], session: Session) -> int:
    """Built-in REPL for API backends or when no CLI coder is available."""

    # Show prior state if exists
    if session.last_evidence_dir:
        evidence_path = Path(session.last_evidence_dir)
        if evidence_path.is_dir():
            print(f"  Prior run: {evidence_path.name} ({session.last_disposition or '?'})")
            print()

    print("  Type what you want to do, or /help for commands.\n")

    while True:
        try:
            line = input("saturnday> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not line:
            continue

        intent = classify_intent(line)

        if intent == "empty":
            continue

        elif intent == "slash":
            cmd, arg = parse_slash(line)
            if cmd in ("quit", "exit"):
                break
            elif cmd == "help":
                _print_help()
            elif cmd in ("guard", "scan", "check"):
                guided_guard(repo_path, context, hint=cmd)
            elif cmd == "plan":
                guided_run(
                    repo_path, goal=arg if arg else None, session=session,
                    work_branch=session.work_branch,
                )
            elif cmd == "run":
                if session.last_plan_path and Path(session.last_plan_path).is_file():
                    guided_run(
                        repo_path, goal=None, session=session,
                        work_branch=session.work_branch,
                    )
                else:
                    print("  No plan to run. Describe what you want to build.")
            elif cmd == "repair":
                guided_repair(repo_path, session=session, hint=arg or "")
            elif cmd == "resume":
                if session.last_evidence_dir and session.last_plan_path:
                    evidence_dir = Path(session.last_evidence_dir)
                    plan_path = Path(session.last_plan_path)
                    if evidence_dir.is_dir() and plan_path.is_file():
                        print(f"  Resuming from {evidence_dir.name}...")
                        be = select_backend(session)
                        if be:
                            from saturnday._types import CoderConfig
                            from saturnday.run.resume import rerun_failed
                            config = CoderConfig(backend=be)
                            standards_dir = _find_standards_dir(repo_path)
                            try:
                                resume_result = rerun_failed(
                                    evidence_dir=str(evidence_dir),
                                    plan_path=str(plan_path),
                                    repo_path=str(repo_path),
                                    coder_config=config,
                                    standards_dir=str(standards_dir),
                                    output_dir=str(evidence_dir),
                                )
                                show_run_summary(resume_result)
                            except Exception as exc:
                                print(f"  Resume failed: {exc}")
                    else:
                        print("  Prior run evidence not found on disk.")
                else:
                    print("  No prior run to resume. Start a build first.")
            elif cmd == "status":
                show_status(repo_path)
            else:
                print(f"  Unknown command: {line}")
                print("  Type /help for available commands.")

        elif intent == "help":
            _print_help()

        elif intent == "guard":
            guided_guard(repo_path, context, hint=line)

        elif intent == "plan_run":
            goal = line.strip()
            result_code = guided_run(  # noqa: F841
                repo_path, goal=goal, session=session,
                work_branch=session.work_branch,
            )

        elif intent == "repair":
            guided_repair(repo_path, session=session, hint=line)

        elif intent == "status":
            show_status(repo_path)

        elif intent == "publish":
            print("\n  Publish preflight checks:")
            print("  Running Guard scan before publish...")
            context = detect_context(repo_path)
            guided_guard(repo_path, context, hint="scan")

        elif intent == "unknown":
            _handle_conversation(line, repo_path, session)

    # Save session on exit
    save_session(session, repo_path)
    print("  Session saved. Goodbye.")
    return 0


# ---------------------------------------------------------------------------
# Backend selection
# ---------------------------------------------------------------------------

# All supported backends with setup instructions.
_SUPPORTED_BACKENDS: list[dict[str, str]] = [
    {
        "name": "codex-cli",
        "label": "Codex CLI (OpenAI subscription)",
        "check": "codex login status",
        "setup": "npm install -g @openai/codex && codex login",
        "login": "codex login",
    },
    {
        "name": "claude-cli",
        "label": "Claude Code CLI (Anthropic subscription)",
        "check": "claude auth status",
        "setup": "Install from claude.ai/download, then run: claude login",
        "login": "claude login",
    },
    {
        "name": "openclaude",
        "label": "OpenClaude (Claude Code fork + OpenAI-compatible shim)",
        "check": "openclaude --version",
        "setup": "npm install -g @gitlawb/openclaude",
    },
    {
        "name": "cursor-cli",
        "label": "Cursor CLI (multi-model)",
        "check": "agent --version",
        "setup": "curl https://cursor.com/install -fsS | bash",
        "login": "Set CURSOR_API_KEY or sign in at cursor.com",
    },
    {
        "name": "openai",
        "label": "OpenAI API (API key)",
        "check": "OPENAI_API_KEY environment variable",
        "setup": "export OPENAI_API_KEY=sk-...",
    },
    {
        "name": "anthropic",
        "label": "Anthropic API (API key)",
        "check": "ANTHROPIC_API_KEY environment variable",
        "setup": "export ANTHROPIC_API_KEY=sk-ant-...",
    },
    {
        "name": "local-120b",
        "label": "Local GPT-OSS 120B  (vLLM on localhost:8000)",
        "check": "vLLM server at http://localhost:8000/v1",
        "setup": (
            "# Terminal 1 — start the vLLM server:\n"
            "  source $HOME/gpt-oss-vllm/.venv/bin/activate\n"
            "  VLLM_USE_FLASHINFER_SAMPLER=0 vllm serve openai/gpt-oss-120b --port 8000 --tensor-parallel-size 1"
        ),
    },
]


def _check_cli_auth(binary: str, auth_cmd: list[str]) -> tuple[str, str]:
    """Probe CLI binary installation and auth status.

    Args:
        binary: CLI binary name (``"codex"`` or ``"claude"``).
        auth_cmd: Command to check auth status, e.g. ``["claude", "auth", "status"]``.

    Returns:
        Tuple of ``(status, reason)`` where status is one of:
        ``"not_installed"``, ``"installed_auth_unverified"``, ``"auth_verified"``.
    """
    if shutil.which(binary) is None:
        return "not_installed", f"'{binary}' not found on PATH"
    # Attempt real auth verification
    try:
        r = subprocess.run(
            auth_cmd,
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0:
            return "auth_verified", "installed, logged in"
        return "installed_auth_unverified", "installed, not logged in"
    except (subprocess.SubprocessError, OSError):
        # Auth command failed or doesn't exist — fall back to binary probe
        return "installed_auth_unverified", "installed, login status unknown"


def _check_backend_ready(name: str) -> tuple[bool, str]:
    """Check if a backend is ready to use.

    Returns honest readiness status:
    - CLI backends: checks both installation AND auth (not just binary existence)
    - API backends: checks environment variable presence

    Args:
        name: Backend name from :data:`_SUPPORTED_BACKENDS`.

    Returns:
        Tuple of ``(usable, reason)``.  ``usable`` is ``True`` only when
        auth is verified (CLI) or an API key is set.
    """
    if name == "codex-cli":
        status, reason = _check_cli_auth("codex", ["codex", "login", "status"])
        return status == "auth_verified", reason
    if name == "claude-cli":
        status, reason = _check_cli_auth("claude", ["claude", "auth", "status"])
        return status == "auth_verified", reason
    if name == "openclaude":
        if shutil.which("openclaude") is None:
            return False, "'openclaude' not found on PATH"
        return True, "openclaude binary found"
    if name == "cursor-cli":
        if shutil.which("agent") is None:
            return False, "'agent' not found on PATH"
        # CURSOR_API_KEY set → ready without a further subprocess probe
        if os.environ.get("CURSOR_API_KEY"):
            return True, "API key set"
        # Fall back to version probe as a basic liveness check
        status, reason = _check_cli_auth("agent", ["agent", "--version"])
        return status == "auth_verified", reason
    if name == "openai":
        if os.environ.get("OPENAI_API_KEY"):
            return True, "API key set"
        return False, "OPENAI_API_KEY not set"
    if name == "anthropic":
        if os.environ.get("ANTHROPIC_API_KEY"):
            return True, "API key set"
        return False, "ANTHROPIC_API_KEY not set"
    if name == "local-120b":
        if _probe_local_vllm():
            return True, "vLLM responding on localhost:8000"
        return False, "vLLM not running — start server in Terminal 1"
    return False, f"unknown backend '{name}'"


def select_backend(session: "Session | None" = None) -> str | None:
    """Ask the user which backend to use.

    If the session already has a backend, offers to reuse it.
    Otherwise shows all supported backends and lets the user pick.
    After selection, checks readiness and shows setup instructions
    if the backend isn't ready.

    Args:
        session: Active session.  If it has a ``backend`` set, the
            user is offered to reuse it.

    Returns:
        Backend name string, or ``None`` if user cancelled.
    """
    # Reuse session backend if set
    if session and session.backend:
        ready, reason = _check_backend_ready(session.backend)
        status = f"\u2713 {reason}" if ready else f"\u2717 {reason}"
        print(f"  Backend: {session.backend} ({status})")
        try:
            reuse = input("  Use this backend? [Y/n] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return None
        if reuse != "n":
            if ready:
                return session.backend
            # Not ready — show setup and wait
            return _wait_for_backend(session.backend)

    # Show all supported backends
    print("\n  Which backend do you want to use?")
    print()
    for i, b in enumerate(_SUPPORTED_BACKENDS, 1):
        ready, reason = _check_backend_ready(b["name"])
        status = f"\u2713 {reason}" if ready else f"\u2717 {reason}"
        print(f"    [{i}] {b['label']}  ({status})")
    print()
    try:
        choice = input(f"  Select [1-{len(_SUPPORTED_BACKENDS)}]: ").strip()
    except (EOFError, KeyboardInterrupt):
        return None

    selected = None
    if choice.isdigit() and 1 <= int(choice) <= len(_SUPPORTED_BACKENDS):
        selected = _SUPPORTED_BACKENDS[int(choice) - 1]["name"]
    else:
        # Try matching by name
        for b in _SUPPORTED_BACKENDS:
            if choice.lower() in (b["name"].lower(), b["label"].lower()):
                selected = b["name"]
                break
    if not selected:
        print("  Invalid selection.")
        return None

    # Remember in session
    if session:
        session.backend = selected

    ready, reason = _check_backend_ready(selected)
    if ready:
        print(f"  Using backend: {selected}")
        return selected

    return _wait_for_backend(selected)


def _wait_for_backend(name: str) -> str | None:
    """Show setup instructions and wait for the user to set up a backend.

    For CLI backends that are installed but not logged in, shows a login
    prompt and lets the user proceed after logging in.  For backends
    that are not installed at all, shows install instructions.

    Args:
        name: Backend name.

    Returns:
        Backend name if it becomes ready or user chooses to proceed
        anyway, ``None`` if user aborts.
    """
    info = next((b for b in _SUPPORTED_BACKENDS if b["name"] == name), None)
    if not info:
        return None

    ready, reason = _check_backend_ready(name)
    if ready:
        print(f"  Using backend: {name}")
        return name

    # Local 120B: guided two-terminal startup with readiness probe
    if name == "local-120b":
        print("\n  Local GPT-OSS 120B requires a vLLM server in a separate terminal.")
        print()
        print("  Terminal 1 — start the vLLM server:")
        print("    source $HOME/gpt-oss-vllm/.venv/bin/activate")
        print("    VLLM_USE_FLASHINFER_SAMPLER=0 vllm serve openai/gpt-oss-120b --port 8000 --tensor-parallel-size 1")
        print()
        print("  The model loads in 2–5 minutes.  Watch for 'Application startup complete'.")
        print("  Press Enter here to check readiness, or Ctrl+C to cancel.")
        while True:
            try:
                input("  > ")
            except (EOFError, KeyboardInterrupt):
                print()
                return None
            if _probe_local_vllm():
                print("  vLLM is responding. Using local-120b.")
                return name
            print("  Not ready yet — vLLM still loading. Press Enter to check again.")
        # unreachable

    # Distinguish "installed but not logged in" from "not installed"
    is_cli = name in ("codex-cli", "claude-cli", "openclaude", "cursor-cli")
    _cli_binaries = {"codex-cli": "codex", "claude-cli": "claude", "openclaude": "openclaude", "cursor-cli": "agent"}
    binary = _cli_binaries.get(name)
    installed = is_cli and binary and shutil.which(binary) is not None

    if installed:
        # Installed but auth not verified
        login_cmd = info.get("login", f"{binary} login")
        print(f"\n  {info['label']} is installed, but login could not be verified.")
        print(f"  If you are not already logged in, run:")
        print(f"    {login_cmd}")
        print(f"  in another terminal, then press Enter to continue.")
    else:
        # Not installed or API key not set
        print(f"\n  Backend '{name}' is not ready: {reason}")
        print(f"\n  To set up:")
        print(f"    {info['setup']}")

    print()
    try:
        input("  Press Enter when ready, or Ctrl+C to pick a different backend...")
    except (EOFError, KeyboardInterrupt):
        return None

    ready, reason = _check_backend_ready(name)
    if ready:
        print(f"  Using backend: {name}")
        return name

    # For installed CLI backends, let user proceed anyway — the auth
    # check may not work in all environments
    if installed:
        print(f"  Login still could not be verified automatically.")
        try:
            proceed = input("  Try anyway? [Y/n] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return None
        if proceed != "n":
            print(f"  Using backend: {name}")
            return name

    print(f"  Backend not ready: {reason}")
    return None


# ---------------------------------------------------------------------------
# Plan preview and progress helpers
# ---------------------------------------------------------------------------

def _write_accepted_dod_artifact(plan_data: dict[str, Any], evidence_dir: Path) -> None:
    """Write the accepted Definition of Done contract as a per-run artifact.

    Written into the run-specific evidence directory so each run has its
    own auditable DoD record that cannot be overwritten by later runs.

    Args:
        plan_data: Parsed plan JSON containing DoD fields.
        evidence_dir: The per-run evidence directory (e.g.
            ``.saturnday/run/run_20260405T.../``).
    """
    import json as _json
    from datetime import datetime, timezone

    evidence_dir.mkdir(parents=True, exist_ok=True)
    artifact = {
        "created_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "project_id": plan_data.get("project_id", ""),
        "governing_goal": plan_data.get("governing_goal", ""),
        "required_outcomes": plan_data.get("required_outcomes", []),
        "proof_expectations": plan_data.get("proof_expectations", []),
        "exclusions": plan_data.get("exclusions", []),
        "constraints": plan_data.get("constraints", []),
        "definition_of_done_markers": plan_data.get("definition_of_done", []),
        "user_edited": plan_data.get("_dod_user_edited", False),
    }
    dod_path = evidence_dir / "accepted-dod.json"
    dod_path.write_text(_json.dumps(artifact, indent=2), encoding="utf-8")


def show_plan_preview(plan_data: dict[str, Any]) -> None:
    """Print plan summary including the real Definition of Done contract.

    Args:
        plan_data: Parsed plan JSON as a dictionary.
    """
    tickets = plan_data.get("tickets", [])
    phases = plan_data.get("phases", [])
    dod = plan_data.get("definition_of_done", ["all_tickets_passed"])
    project_id = plan_data.get("project_id", "?")
    print(f"\n  Plan: {project_id}")
    print(f"  {'─' * 48}")
    print(f"  Tickets: {len(tickets)}    Phases: {len(phases)}")
    print()

    # Phase 8: surface the persisted product framing so the operator sees
    # WHAT will be built and HOW it will be proved before the run fires.
    _op_mode = plan_data.get("operating_mode", "")
    _dep_prof = plan_data.get("dependency_profile", "")
    _realism = plan_data.get("proof_realism", "")
    _test_strat = plan_data.get("testing_strategy", "")
    _disclaimer = plan_data.get("operator_disclaimer", "")
    _ext_deps = plan_data.get("external_dependencies", []) or []
    _local_proof = plan_data.get("local_proof_cmd", "") or ""
    _live_proof = plan_data.get("live_proof_cmd", "") or ""
    _resolution_src = plan_data.get("proof_resolution_source", "") or ""

    _has_framing = (
        _op_mode and _op_mode != "legacy_unclassified"
    ) or _realism or _dep_prof or _test_strat

    if _has_framing:
        print("  Product framing:")
        if _op_mode:
            print(f"    operating_mode      : {_op_mode}")
        if _dep_prof:
            print(f"    dependency_profile  : {_dep_prof}")
        if _realism:
            print(f"    proof_realism       : {_realism}")
        if _test_strat and _test_strat != "unspecified":
            print(f"    testing_strategy    : {_test_strat}")
        if _ext_deps:
            print(f"    external_deps       : {', '.join(_ext_deps)}")
        print()

    if _disclaimer:
        print("  Operator disclaimer:")
        # Wrap long disclaimers to 72 cols for readability.
        import textwrap
        for line in textwrap.wrap(_disclaimer, width=70):
            print(f"    {line}")
        print()

    if _local_proof or _live_proof:
        print("  Proof:")
        if _local_proof:
            _is_gap = "FIX73_PROOF_GAP" in _local_proof
            _label = "local_proof (GAP)" if _is_gap else "local_proof"
            _preview = _local_proof.splitlines()[0][:160]
            print(f"    {_label:20s}: {_preview}")
            if _is_gap:
                print(
                    "      ⚠ proof not derived at plan time — Phase 5 "
                    "(post-execution) or Phase 6 (operator) will try."
                )
        if _live_proof:
            _preview = _live_proof.splitlines()[0][:160]
            print(f"    {'live_proof':20s}: {_preview}")
            print("      (LIVE_PROOF=1 required — non-blocking)")
        if _resolution_src and _resolution_src != "none":
            print(f"    {'resolution_source':20s}: {_resolution_src}")
        print()

    # Phase 8: show the clarification record so the operator sees which
    # assumptions were made (and by whom) before planning proceeded.
    _clarif = plan_data.get("clarification_record", []) or []
    if _clarif:
        print("  Clarification record:")
        for entry in _clarif:
            _kind = entry.get("trigger_kind", "?")
            _ans = entry.get("answer", "") or "(no answer)"
            print(f"    • [{_kind}] {_ans[:60]}")
        print()

    # Show the real DoD contract — not just the marker token
    required_outcomes = plan_data.get("required_outcomes", [])
    proof_expectations = plan_data.get("proof_expectations", [])
    exclusions = plan_data.get("exclusions", [])
    constraints = plan_data.get("constraints", [])

    if required_outcomes or proof_expectations:
        print("  Definition of Done:")
        if required_outcomes:
            print("    Required outcomes:")
            for outcome in required_outcomes:
                print(f"      • {outcome}")
        if proof_expectations:
            print("    Proof expectations:")
            for proof in proof_expectations:
                print(f"      • {proof}")
        if exclusions:
            print("    Exclusions:")
            for excl in exclusions:
                print(f"      • {excl}")
        if constraints:
            print("    Constraints:")
            for con in constraints:
                print(f"      • {con}")
        print()
    else:
        print(f"  Definition of Done: {', '.join(dod)}")
        print()

    for t in tickets[:10]:
        tid = t.get("ticket_id", "?")
        goal = t.get("goal", "")[:55]
        print(f"    {tid:8s} {goal}")
    if len(tickets) > 10:
        print(f"    ... and {len(tickets) - 10} more")
    print()


def _edit_outcomes_in_editor(outcomes: list[str]) -> list[str]:
    """Open ``$EDITOR`` with the existing outcomes pre-filled, return edits.

    Uses ``$EDITOR`` / ``$VISUAL`` if set, falls back to ``nano``, ``vim``,
    ``vi`` in that order.  The file is seeded with the current outcomes
    (one per line) plus a short ``#``-prefixed header explaining the format.
    Comment lines and blank lines are stripped from the result.

    If the editor fails to launch or the saved file is empty, the original
    outcomes are returned unchanged (no destructive replacement).
    """
    import os
    import shutil
    import subprocess
    import tempfile

    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or ""
    if not editor or not shutil.which(editor.split()[0]):
        for fallback in ("nano", "vim", "vi"):
            if shutil.which(fallback):
                editor = fallback
                break
    if not editor:
        return outcomes  # no editor available — preserve original

    header = (
        "# Edit the Definition of Done — one required outcome per line.\n"
        "# Lines starting with '#' are ignored.\n"
        "# Save and exit when done.\n"
        "\n"
    )
    body = "\n".join(outcomes) + "\n"

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8",
    ) as f:
        f.write(header + body)
        tmp_path = f.name

    try:
        subprocess.run([editor, tmp_path], check=False)
        content = Path(tmp_path).read_text(encoding="utf-8")
        edited = [
            line.strip()
            for line in content.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        return edited if edited else outcomes
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass


def _amend_outcomes_inline(outcomes: list[str]) -> list[str] | None:
    """β: inline amendment of the proposed DoD outcomes.

    Iterates each current outcome and prompts the operator for a
    replacement.  When ``readline`` is available and stdin is a TTY, the
    current value is prefilled via ``readline.insert_text`` so the
    operator can edit it in place.  When ``readline`` is not available,
    the current value is printed above the prompt and ``input()`` is used
    with the convention "press enter to keep current, or type replacement".

    After all existing outcomes are processed, the operator is prompted
    for additional new outcomes; a blank line terminates the sequence.

    Returns the amended list on success, or ``None`` if the operator
    cancelled (EOF / Ctrl-C).  A ``None`` return signals "preserve the
    original" to the caller.
    """
    try:
        import readline  # type: ignore[import-not-found]
        _has_readline = True
    except ImportError:  # pragma: no cover — readline is stdlib on Linux/macOS
        readline = None  # type: ignore[assignment]
        _has_readline = False

    amended: list[str] = []
    print()
    print("  Amend inline — for each existing outcome, press enter to keep it")
    print("  or type a replacement.  After the last existing outcome, you can")
    print("  add new outcomes; a blank line finishes.")
    print()

    try:
        for i, current in enumerate(outcomes, 1):
            if _has_readline:
                readline.set_startup_hook(lambda value=current: readline.insert_text(value))
                try:
                    typed = input(f"  [{i}] ")
                finally:
                    readline.set_startup_hook()
            else:
                print(f"  Current [{i}]: {current}")
                typed = input(f"  [{i}] (enter to keep, or replacement): ")
            typed = typed.strip()
            if typed == "":
                # readline prefill submitted with no edits OR fallback "keep".
                amended.append(current)
            else:
                amended.append(typed)

        # Additional outcomes — blank line ends.
        next_idx = len(outcomes) + 1
        while True:
            added = input(f"  [{next_idx}] (new outcome, blank to finish): ").strip()
            if not added:
                break
            amended.append(added)
            next_idx += 1
    except (EOFError, KeyboardInterrupt):
        print("\n  Amend cancelled — original Definition of Done preserved.")
        return None

    return amended


def _run_dod_gate(plan_data: dict[str, Any], plan_path: Path) -> bool:
    """Present the DoD approval gate.

    Shared by both :func:`guided_run` and :func:`plan_confirm` so the
    approval surface is identical on all start paths.

    Menu (β extension):
      [1] Accept                    — proceed with Saturnday's proposed DoD
      [2] Amend inline              — edit outcomes one-by-one in the terminal
      [3] Edit in $EDITOR           — open the whole list in $EDITOR (prefilled)
      [4] Abort                     — do not proceed with the run

    Args:
        plan_data: Parsed plan dict — mutated in-place if the user edits outcomes.
        plan_path: Path to the on-disk plan.json — rewritten if user edits.

    Returns:
        ``True`` when the plan is ready to run.  Returns ``False`` on
        explicit ``[4] Abort`` or on a hard input interrupt (EOF / Ctrl-C)
        before a choice is made.
    """
    required_outcomes = plan_data.get("required_outcomes", [])
    if required_outcomes:
        try:
            print("  [1] Accept this Definition of Done and proceed")
            print("  [2] Amend inline (edit outcomes one by one)")
            print("  [3] Edit in $EDITOR (whole list in your editor)")
            print("  [4] Abort — do not run this plan")
            dod_choice = input("  Select [1/2/3/4]: ").strip()
        except (EOFError, KeyboardInterrupt):
            return False

        if dod_choice == "4":
            return False

        if dod_choice in ("2", "3"):
            if dod_choice == "2":
                amended = _amend_outcomes_inline(list(required_outcomes))
                edited = amended if amended is not None else list(required_outcomes)
            else:
                edited = _edit_outcomes_in_editor(list(required_outcomes))
            changed = edited != list(required_outcomes)
            if changed:
                plan_data["required_outcomes"] = edited
                plan_data["_dod_user_edited"] = True
                plan_path.write_text(
                    json.dumps(plan_data, indent=2),
                    encoding="utf-8",
                )
                print()
                print("  Updated Definition of Done:")
                for outcome in edited:
                    print(f"    • {outcome}")
            else:
                plan_data.setdefault("_dod_user_edited", False)
                print("  No changes — proceeding with Saturnday's proposed DoD.")
            return True

        # Default for [1] (Accept) AND any other unrecognised input: proceed.
        plan_data.setdefault("_dod_user_edited", False)
        return True

    # No proposed outcomes — single proceed confirmation.
    try:
        proceed = input("  Proceed? [Y/n] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    if proceed == "n":
        return False
    return True


def plan_confirm(
    brief: str,
    repo_path: Path,
    backend: str,
    output_path: Path | None = None,
    clarification_record: "tuple | None" = None,
) -> int:
    """Generate a plan and enforce the DoD approval gate interactively.

    Intended for launcher-backed start flows (``claude-cli``, ``openclaude``,
    ``cursor-cli``) where the user talks to the coder and the coder calls this
    command instead of plain ``saturnday plan``.  The gate is mechanical —
    the plan is only written to disk after the user explicitly accepts or edits
    the Definition of Done.

    Args:
        brief: Plain-language project description.
        repo_path: Path to the target repository.
        backend: Coder backend name.
        output_path: Where to write the confirmed plan.  Defaults to
            ``<repo>/.saturnday/plan.json``.
        clarification_record: Fix 75 — operator answers collected by the
            clarification gate before this function was called.  Persisted
            into the plan's ``clarification_record`` field as evidence.

    Returns:
        0 on accept/edit-and-confirm, 1 on abort or generation failure.
    """
    from saturnday._types import CoderConfig
    from saturnday.run.planner import generate_plan

    config = CoderConfig(backend=backend)  # type: ignore[arg-type]
    out_path = output_path or (repo_path / ".saturnday" / "plan.json")

    print("\n  Generating plan...")
    try:
        plan_path = generate_plan(
            brief=brief,
            repo_path=str(repo_path),
            coder_config=config,
            output_path=str(out_path),
            clarification_record=clarification_record,
        )
        plan_data = json.loads(plan_path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"  Plan generation failed: {exc}")
        return 1

    show_plan_preview(plan_data)

    if not _run_dod_gate(plan_data, plan_path):
        print("  Aborted.")
        return 1

    print(f"  Plan confirmed: {plan_path}")
    return 0


def print_ticket_progress(ticket_id: str, phase_name: str, attempt: int, status: str) -> None:
    """Print one-line progress update during run.

    Args:
        ticket_id: The ticket identifier.
        phase_name: Name of the phase this ticket belongs to.
        attempt: Attempt number (0 = not yet attempted / RUNNING/SKIP).
        status: One of ``PASS``, ``FAIL``, ``SKIP``, ``RUNNING``, ``RETRY``.
    """
    icons = {"PASS": "\u2713", "FAIL": "\u2717", "SKIP": "\u2298", "RUNNING": "\u2192", "RETRY": "\u21bb"}
    icon = icons.get(status, "?")
    phase_str = f"  {phase_name:15s}" if phase_name else ""
    attempt_str = f"  (attempt {attempt})" if attempt > 0 else ""
    print(f"  {ticket_id:8s}{phase_str}  {icon} {status}{attempt_str}")


def show_run_summary(result: Any) -> None:
    """Print run summary after completion.

    Args:
        result: A :class:`~saturnday._types.RunResult` instance.
    """
    from saturnday._types import RunResult
    if not isinstance(result, RunResult):
        return
    print(f"\n  {'─' * 48}")
    parts = [f"Passed: {result.passed}"]
    if result.coded_ungoverned > 0:
        parts.append(f"Coded (review needed): {result.coded_ungoverned}")
    parts.append(f"Failed: {result.failed}")
    parts.append(f"Skipped: {result.skipped}")
    print(f"  {'   '.join(parts)}")
    dod_str = "MET" if result.definition_of_done_met else "NOT MET"
    print(f"  DoD: {dod_str}")
    if result.stop_reason:
        print(f"  Stop: {result.stop_reason}")
    # Fix 39: verify_cmd failures — executable proof of incomplete work.
    _vc_failures = [tr for tr in result.ticket_results if tr.verify_cmd_passed is False]
    if _vc_failures:
        print(f"  Executable verification FAILED: {len(_vc_failures)} ticket(s)")
        for _vcf in _vc_failures:
            _summary = _vcf.verify_cmd_failure[:120] if _vcf.verify_cmd_failure else "see evidence"
            print(f"    [{_vcf.ticket_id}] verify_cmd failed — {_summary}")
    # Fix 41: final acceptance gate result.
    if result.acceptance_cmd_passed is True:
        print("  Final acceptance: PASSED")
    elif result.acceptance_cmd_passed is False:
        print("  Final acceptance: FAILED")
        if result.acceptance_cmd_failure:
            print(f"    {result.acceptance_cmd_failure[:200]}")
    # Plan-governance verdict — only shown when the DoD role pass emitted one.
    if result.plan_governance_reason:
        pg_label = "MET" if result.plan_governance_met else "NOT MET"
        print(f"  Plan governance: {pg_label}")
        if not result.plan_governance_met:
            print(f"  Reason: {result.plan_governance_reason}")
    print()


# ---------------------------------------------------------------------------
# Status display
# ---------------------------------------------------------------------------

def show_status(repo_path: Path) -> int:
    """Show last run evidence.

    Args:
        repo_path: Path to the repository root.

    Returns:
        Exit code (always 0).
    """
    candidates = sorted(
        repo_path.glob(".saturnday-*"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        print("  No previous runs found.")
        return 0
    evidence_dir = candidates[0]
    summary_path = evidence_dir / "run-summary.json"
    if not summary_path.is_file():
        print(f"  Evidence dir: {evidence_dir}")
        print("  No run-summary.json found.")
        return 0
    data = json.loads(summary_path.read_text(encoding="utf-8"))
    print(f"\n  Last run: {evidence_dir.name}")
    print(f"  {'─' * 48}")
    results = data.get("ticket_results", [])
    passed = sum(1 for r in results if r.get("disposition") == "PASS")
    failed = sum(1 for r in results if r.get("disposition") == "FAIL")
    skipped = sum(1 for r in results if r.get("disposition") == "SKIP")
    print(f"  Passed: {passed}   Failed: {failed}   Skipped: {skipped}")
    dod = data.get("definition_of_done_met", False)
    print(f"  DoD: {'MET' if dod else 'NOT MET'}")
    print(f"  Evidence: {evidence_dir}")
    print()
    return 0


# ---------------------------------------------------------------------------
# Finding kind humanisation
# ---------------------------------------------------------------------------

_FINDING_KIND_LABELS: dict[str, str] = {
    "shell_danger": "Dangerous shell execution",
    "credential_leak": "Exposed credentials",
    "command_interpolation": "Command injection risk",
    "broad_filesystem": "Risky filesystem operations",
    "missing_approval_gate": "Missing confirmation for destructive actions",
}


def _humanize_finding_kind(kind: str) -> str:
    """Return a human-readable label for a finding kind.

    Falls back to the raw ``kind`` string (with underscores replaced by spaces
    and title-cased) when no explicit mapping exists.

    Args:
        kind: Internal finding kind identifier, e.g. ``"shell_danger"``.

    Returns:
        Human-readable description string.
    """
    return _FINDING_KIND_LABELS.get(kind, kind.replace("_", " ").title())


# ---------------------------------------------------------------------------
# Guided flows
# ---------------------------------------------------------------------------

def _show_role_pass_results(evidence_dir: Path) -> None:
    """Display role pass summaries if available.

    Args:
        evidence_dir: Evidence output directory that may contain
            ``role-pass-dod.json`` and ``role-pass-evidence-gate.json``.
    """
    import json
    try:
        from saturnday.role_modes import extract_classification
    except ImportError:
        def extract_classification(text: str, labels: list) -> str:  # type: ignore[misc]
            return ""

    _DOD_LABELS: dict[str, str] = {
        "DOD_MET": "Completion: all acceptance criteria met",
        "DOD_PARTIAL": "Completion: some criteria not met",
        "DOD_NOT_MET": "Completion: criteria not met",
        "DOD_BLOCKED_BY_MISSING_EVIDENCE": "Completion: blocked — missing evidence",
    }
    _EVIDENCE_LABELS: dict[str, str] = {
        "SUFFICIENT": "Evidence: complete",
        "INSUFFICIENT": "Evidence: incomplete",
        "PARTIAL": "Evidence: partial",
    }

    dod_path = evidence_dir / "role-pass-dod.json"
    if dod_path.is_file():
        try:
            data = json.loads(dod_path.read_text(encoding="utf-8"))
            if data.get("success"):
                classification = extract_classification(
                    data.get("output", ""),
                    ["DOD_MET", "DOD_PARTIAL", "DOD_NOT_MET", "DOD_BLOCKED_BY_MISSING_EVIDENCE"],
                )
                print("  --- completion check ---")
                print(f"  {_DOD_LABELS.get(classification, classification)}")
                print()
        except (json.JSONDecodeError, OSError):
            pass

    eg_path = evidence_dir / "role-pass-evidence-gate.json"
    if eg_path.is_file():
        try:
            data = json.loads(eg_path.read_text(encoding="utf-8"))
            if data.get("success"):
                classification = extract_classification(
                    data.get("output", ""),
                    ["SUFFICIENT", "INSUFFICIENT", "PARTIAL"],
                )
                print("  --- evidence review ---")
                print(f"  {_EVIDENCE_LABELS.get(classification, classification)}")
                print()
        except (json.JSONDecodeError, OSError):
            pass


def print_role_stage(stage_name: str) -> None:
    """Print a visual stage separator.

    Args:
        stage_name: Human-readable name of the stage being entered.
    """
    print(f"\n  --- {stage_name} ---")


def _find_standards_dir(repo_path: Path) -> Path:
    """Find the senior_engineering_standards directory.

    Checks the repo root first, then the package source tree, and falls
    back to the current working directory if neither is found.

    Args:
        repo_path: Repository root directory.

    Returns:
        Path to the standards directory (may not exist when falling back
        to ``Path(".")``).
    """
    local = repo_path / "senior_engineering_standards"
    if local.is_dir():
        return local
    pkg = Path(__file__).resolve().parent.parent.parent / "senior_engineering_standards"
    if pkg.is_dir():
        return pkg
    # Standards shipped inside the saturnday package itself
    bundled = Path(__file__).resolve().parent / "standards"
    if bundled.is_dir():
        return bundled
    # Return a non-existent path so load_standards_context returns ""
    # instead of loading the entire repo root as "standards files"
    # (which would include AGENTS.md, README.md, etc.)
    return repo_path / "senior_engineering_standards"


def _build_coder_config(backend: str):
    """Build a CoderConfig from a backend name.

    Handles the ``"local-120b"`` alias, which maps to the ``"openai"`` backend
    pre-configured for a local vLLM server on ``localhost:8000``.

    Args:
        backend: Backend name from :data:`_SUPPORTED_BACKENDS` or ``"local-120b"``.

    Returns:
        A :class:`~saturnday._types.CoderConfig` instance ready for use.
    """
    from saturnday._types import CoderConfig
    if backend == "local-120b":
        return CoderConfig(
            backend="openai",
            base_url="http://localhost:8000/v1",
            model="openai/gpt-oss-120b",
            api_key="EMPTY",
        )
    return CoderConfig(backend=backend)  # type: ignore[arg-type]


def guided_guard(repo_path: Path, context: dict[str, Any], hint: str = "", coder_config=None) -> int:
    """Interactive Guard flow: full repo review, skill scan, or governance check.

    Args:
        repo_path: Path to the repository root.
        context: Output of :func:`detect_context`.
        hint: If the user's original input contained a clear intent
            (e.g. ``"review"``, ``"scan"``), skip the menu.
        coder_config: Optional coder config for LLM progress messages.

    Returns:
        Exit code (0 = pass, 1 = fail/blocked).
    """
    # Auto-detect intent from hint to skip the menu
    hint_lower = hint.lower()
    has_skill_md = (repo_path / "SKILL.md").is_file()
    if any(w in hint_lower for w in ("review", "inspect", "examine", "analyse", "analyze")):
        choice = "1"
    elif "scan" in hint_lower:
        # "scan" goes to skill scan if SKILL.md exists, otherwise full review
        choice = "2" if has_skill_md else "1"
    elif any(w in hint_lower for w in ("check", "diff", "commit")):
        choice = "3"
    else:
        print("\n  Guard -- review and check")
        print("  [1] Review -- full repo security and quality review")
        print("  [2] Scan   -- OpenClaw skill scan")
        print("  [3] Check  -- governance on recent changes")
        try:
            choice = input("  > ").strip()
        except (EOFError, KeyboardInterrupt):
            return 0

    if choice == "1" or choice.lower() == "review":
        if not context.get("git_initialized"):
            print("  Error: not a git repo. Cannot run full repo review.")
            return 1
        from saturnday.governance import run_full_repo_review
        print(f"\n  Reviewing all tracked files in {repo_path}...")
        try:
            pack, evidence_path = run_full_repo_review(repo_path)
        except RuntimeError as exc:
            print(f"  Error: {exc}")
            return 1
        print(f"  Disposition: {pack.disposition}")
        print(f"  Checks run: {len(pack.check_results)}")
        # Show findings by category
        by_check: dict[str, int] = {}
        for cr in pack.check_results:
            if cr.findings:
                by_check[cr.name] = len(cr.findings)
        if by_check:
            print()
            for name, count in sorted(by_check.items(), key=lambda x: -x[1])[:8]:
                print(f"    {name}: {count} finding(s)")
        total_findings = sum(len(cr.findings) for cr in pack.check_results)
        print(f"\n  Total findings: {total_findings}")
        print(f"  Evidence: {evidence_path}")
        # Progress message on FAIL
        if pack.disposition == "FAIL" and coder_config and by_check:
            try:
                from saturnday.ticket_runner import _generate_progress_message
                _finding_summary = ", ".join(f"{n}: {c}" for n, c in sorted(by_check.items(), key=lambda x: -x[1])[:5])
                _guard_msg = _generate_progress_message(
                    coder_config, repo_path,
                    f"Governance scan found {total_findings} findings: {_finding_summary}. "
                    f"Write one sentence: what would an AI coder without Saturnday have shipped if this code went unscanned?",
                )
                if _guard_msg:
                    print(f"\n  >> {_guard_msg}")
            except Exception:
                pass
        return 0 if pack.disposition == "PASS" else 1

    if choice == "2" or choice.lower() == "scan":
        has_skill_md = (repo_path / "SKILL.md").is_file()
        if has_skill_md:
            from saturnday.guard.cloud_scanner import scan_skill
            print(f"\n  Scanning {repo_path}...")
            result = scan_skill(repo_path)
            print(f"  Disposition: {result.disposition}")
            print(f"  Findings: {result.findings_count}")
            if result.findings:
                print()
                for f in result.findings[:5]:
                    print(f"    {_humanize_finding_kind(f.kind)}: {f.message or f.file}")
                if len(result.findings) > 5:
                    print(f"    ... and {len(result.findings) - 5} more")
            # Progress message on FAIL
            if result.disposition == "FAIL" and coder_config and result.findings:
                try:
                    from saturnday.ticket_runner import _generate_progress_message
                    _kinds = sorted({f.kind for f in result.findings if f.kind})[:5]
                    _scan_msg = _generate_progress_message(
                        coder_config, repo_path,
                        f"Skill scan found {result.findings_count} findings: {', '.join(_kinds)}. "
                        f"Write one sentence: what would happen if this skill was published without these findings being caught?",
                    )
                    if _scan_msg:
                        print(f"\n  >> {_scan_msg}")
                except Exception:
                    pass
            return 0 if result.disposition == "PASS" else 1
        # No SKILL.md — fall through to full repo review
        if not context.get("git_initialized"):
            print("  Error: not a git repo and no SKILL.md. Cannot scan.")
            return 1
        print("  No SKILL.md found — running full repo review instead.")
        from saturnday.governance import run_full_repo_review
        print(f"\n  Reviewing all tracked files in {repo_path}...")
        try:
            pack, evidence_path = run_full_repo_review(repo_path)
        except RuntimeError as exc:
            print(f"  Error: {exc}")
            return 1
        print(f"  Disposition: {pack.disposition}")
        print(f"  Checks run: {len(pack.check_results)}")
        by_check_s: dict[str, int] = {}
        for cr in pack.check_results:
            if cr.findings:
                by_check_s[cr.name] = len(cr.findings)
        if by_check_s:
            print()
            for name, count in sorted(by_check_s.items(), key=lambda x: -x[1])[:8]:
                print(f"    {name}: {count} finding(s)")
        total_findings_s = sum(len(cr.findings) for cr in pack.check_results)
        print(f"\n  Total findings: {total_findings_s}")
        print(f"  Evidence: {evidence_path}")
        return 0 if pack.disposition == "PASS" else 1

    if choice == "3" or choice.lower() == "check":
        if not context.get("git_initialized"):
            print("  Error: not a git repo. Cannot run governance check.")
            return 1
        try:
            diff_range = input("  Diff range [HEAD~1..HEAD]: ").strip() or "HEAD~1..HEAD"
        except (EOFError, KeyboardInterrupt):
            return 0
        from saturnday.governance import run_governance_check
        print(f"\n  Checking {repo_path} ({diff_range})...")
        try:
            pack, evidence_path = run_governance_check(repo_path, diff_range)
        except RuntimeError as exc:
            print(f"  Error: {exc}")
            print("  Hint: use a git diff range like HEAD~3..HEAD or a commit SHA range.")
            return 1
        print(f"  Disposition: {pack.disposition}")
        print(f"  Checks run: {len(pack.check_results)}")
        findings_count = sum(len(cr.findings) for cr in pack.check_results)
        print(f"  Findings: {findings_count}")
        print(f"  Evidence: {evidence_path}")
        return 0 if pack.disposition == "PASS" else 1

    return 0


def guided_run(
    repo_path: Path,
    goal: str | None = None,
    session: "Session | None" = None,
    work_branch: str | None = None,
) -> int:
    """Interactive Run flow: goal -> plan -> confirm -> execute -> summary.

    Args:
        repo_path: Path to the repository root.
        goal: Pre-supplied goal string.  If provided the goal-input prompt
            is skipped.
        session: Active :class:`Session` instance.  When provided, the
            plan path, evidence directory, and disposition are written back
            to the session and persisted after the run completes.
        work_branch: I.4 — opt-in work-branch name.  ``None`` disables
            the feature (unchanged behaviour); the
            :data:`AUTO_NAME_SENTINEL` string triggers auto-naming; any
            other string is used verbatim.  Forwarded straight into
            ``run_plan`` — all the refusal / creation logic lives there.

    Returns:
        Exit code (0 = all tickets passed, 1 = failures).
    """
    # FU-01: Ensure the repo is git-initialised before proceeding.
    if not (repo_path / ".git").is_dir():
        print("\n  This directory is not a git repository.")
        print("  Saturnday needs git to track changes and run governance checks.")
        try:
            init = input("  Initialize git here? [Y/n] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return 0
        if init == "n":
            print("  Aborted. Run 'git init' manually if you want to proceed later.")
            return 0
        import subprocess as _sp
        r = _sp.run(["git", "init", str(repo_path)], capture_output=True)
        if r.returncode != 0:
            print("  Failed to initialize git. Is git installed?")
            print(f"  Error: {r.stderr.decode(errors='replace').strip()}")
            return 1
        _sp.run(["git", "-C", str(repo_path), "config", "user.email", "saturnday@local"], capture_output=True)
        _sp.run(["git", "-C", str(repo_path), "config", "user.name", "Saturnday"], capture_output=True)
        _sp.run(["git", "-C", str(repo_path), "commit", "--allow-empty", "-m", "init"], capture_output=True)
        print("  Git initialized.\n")

    backend = select_backend(session)
    if not backend:
        return 1

    goal_param_was_none = goal is None
    if goal is None:
        try:
            goal = input("\n  Describe your goal:\n  > ").strip()
        except (EOFError, KeyboardInterrupt):
            return 0

    if not goal:
        print("  No goal provided.")
        return 1

    # Build config
    config = _build_coder_config(backend)

    # Brief enrichment — only in real interactive sessions (not tests/scripts)
    import os as _os_enrich
    _is_tty = _os_enrich.isatty(0)  # stdin is a terminal
    if goal_param_was_none and _is_tty and len(goal.split()) < 200:
        enriched = _enrich_brief(goal, config, repo_path)
        if enriched:
            print(f"\n  Enriched brief:\n")
            for line in enriched[:600].split("\n"):
                print(f"    {line}")
            if len(enriched) > 600:
                print(f"    ... ({len(enriched)} chars total)")
            try:
                use_enriched = input("\n  Use this enriched brief? [Y/n] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                use_enriched = "y"
            if use_enriched != "n":
                goal = enriched

    # Generate plan — returns a Path to the written plan.json.
    #
    # The plan is written to the repo's own .saturnday/plan.json so it
    # lands at the same canonical location as 'saturnday plan'.  Each
    # project lives in its own repo with its own .saturnday/ directory,
    # so there is no cross-project collision — different projects never
    # share a plan.json.  Within a single repo, re-running 'saturnday
    # start' DOES overwrite the working plan.json, but every prior run
    # still has its own archived copy at .saturnday/run/<timestamp>/plan.json
    # (written by run_plan in ticket_runner.py), so historical plans
    # are preserved regardless.
    #
    # Pre-fix behaviour was tempfile.mkdtemp("saturnday-plan-") — which
    # wrote to /tmp, got cleaned on reboot, and made the plan invisible
    # to the standard 'find .saturnday' discovery flow.
    print("\n  Generating plan...")
    from saturnday.run.planner import generate_plan

    _sat_dir = Path(repo_path) / ".saturnday"
    _sat_dir.mkdir(parents=True, exist_ok=True)
    plan_output_path = _sat_dir / "plan.json"
    try:
        plan_path = generate_plan(
            brief=goal,
            repo_path=str(repo_path),
            coder_config=config,
            output_path=str(plan_output_path),
        )
        plan_data = json.loads(plan_path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"  Plan generation failed: {exc}")
        return 1

    print(f"  Plan written to: {plan_path}")

    show_plan_preview(plan_data)

    # DoD approval gate — shared with plan_confirm for identical behaviour on all paths.
    if not _run_dod_gate(plan_data, plan_path):
        print("  Aborted.")
        return 0

    # Find standards dir using shared helper.
    standards_dir = _find_standards_dir(repo_path)

    # Run — evidence dir is generated per-run by run_plan (unique run ID).
    # Pass output_dir=None to let run_plan create it.
    print(f"\n  {'─' * 48}")
    print("  Running...")
    print()
    print_role_stage("analysing repo")
    from saturnday.ticket_runner import run_plan
    # Wire the two end-user features into the guided (saturnday start) path:
    # - auto_repair=True gives retry-exhausted tickets one final targeted
    #   repair pass AND fires Phase 9 proof-gap auto-repair post-execution.
    # - lessons_db points at the shared per-user ~/.saturnday/lessons.db so
    #   failure patterns from past projects inform future ticket runs.
    # Directory is created lazily; if it doesn't exist yet, run_plan's
    #   lessons loader treats the empty DB as "no prior lessons" and records
    #   new ones from this run.
    # Resolve via the canonical state-dir resolver (respects
    # SATURNDAY_STATE_DIR and XDG_STATE_HOME, falls back to ~/.saturnday).
    from saturnday.paths import state_dir as _state_dir
    _lessons_db_path = _state_dir() / "lessons.db"
    try:
        _lessons_db_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as _lessons_mkdir_exc:
        logger.debug(
            "Could not create lessons DB parent dir %s: %s — continuing "
            "without lessons",
            _lessons_db_path.parent, _lessons_mkdir_exc,
        )
        _lessons_db_path = None
    result = run_plan(
        plan_path=str(plan_path),
        repo_path=str(repo_path),
        coder_config=config,
        standards_dir=str(standards_dir),
        output_dir=None,
        progress_callback=print_ticket_progress,
        role_passes=True,
        auto_repair=True,
        lessons_db=str(_lessons_db_path) if _lessons_db_path else None,
        work_branch=work_branch,
    )

    show_run_summary(result)

    # run_plan returns the actual evidence dir directly on the result
    output_dir = result.evidence_dir

    # Show role pass results if available
    if output_dir:
        _show_role_pass_results(Path(output_dir))

    # FU-05: Persist run state into the session.
    if session is not None:
        session.last_plan_path = str(plan_path)
        session.last_evidence_dir = output_dir
        session.last_disposition = "MET" if result.definition_of_done_met else "NOT MET"
        save_session(session, repo_path)

    # FU-02: Post-run next steps guidance.
    print("  What to do next:")
    if result.failed > 0:
        print("    'fix the findings'       -- repair failed items")
        print("    'scan this skill'        -- run Guard checks")
    elif result.definition_of_done_met:
        print("    'scan this skill'        -- run Guard checks")
        print("    'check the last commit'  -- governance on your changes")
        print("    '/status'                -- review evidence")
    else:
        print("    'scan this skill'        -- run Guard checks")
        print("    'fix the findings'       -- repair any issues")
    print()

    # Resume prompt
    if result.failed > 0:
        try:
            resume = input("  Resume failed tickets? [Y/n] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return 1
        if resume != "n":
            from saturnday.run.resume import rerun_failed
            result = rerun_failed(
                evidence_dir=output_dir,
                plan_path=str(plan_path),
                repo_path=str(repo_path),
                coder_config=config,
                standards_dir=str(standards_dir),
                output_dir=output_dir,
            )
            show_run_summary(result)

    return 0 if result.failed == 0 else 1


def _scan_for_repair(
    repo_path: Path,
    coder_config=None,
    *,
    target_files: list[str] | None = None,
) -> tuple[list["Finding"], str]:
    """Scan a repo for repairable findings using the full governance review.

    For any git repo (with or without SKILL.md), uses run_full_repo_review
    which runs 40+ checks — the same scanner as ``saturnday governance --full``.
    This ensures repair tickets match governance findings exactly.

    Only falls back to the OpenClaw skill scanner for non-git repos.

    Args:
        repo_path: Repository root directory.
        coder_config: Optional ``CoderConfig`` for LLM-based security triage.
            When provided, ``triage_security_findings`` is called on the raw
            governance finding dicts before they are converted to ``Finding``
            objects, filtering false positives.  Failures are non-fatal.
        target_files: Optional list of repo-relative file paths to restrict
            the governance scan to.  When set, only those files are reviewed —
            use only for file-local finding kinds.  ``None`` (default) runs
            the full-repo scan.

    Returns:
        Tuple of ``(findings_list, scan_mode)`` where scan_mode is
        ``"skill"`` or ``"repo"``.
    """
    from saturnday.openclaw_scanner import Finding

    is_git = (repo_path / ".git").is_dir()

    if is_git:
        # Regular repo — use full governance review
        from saturnday.governance import run_full_repo_review
        try:
            pack, _ = run_full_repo_review(repo_path, target_files=target_files)
        except RuntimeError as exc:
            print(f"  Review error: {exc}")
            return [], "repo"

        # Security triage: filter false positives before generating repair
        # tickets.  Operates on raw governance finding dicts so that the LLM
        # sees the same shape as the CLI --triage path.  Optional and non-fatal.
        if coder_config is not None:
            try:
                _triage = capability_registry.get("security_triage")
                if _triage is not None:
                    _all_gov_findings: list[dict] = []
                    for cr in pack.check_results:
                        _all_gov_findings.extend(cr.findings)
                    _triaged = _triage.filter_findings(
                        _all_gov_findings, repo_path, coder_config
                    )
                    _triaged_ids = set(id(f) for f in _triaged)
                    for cr in pack.check_results:
                        cr.findings = [
                            f for f in cr.findings if id(f) in _triaged_ids
                        ]
            except Exception as exc:
                import logging as _triage_log
                _triage_log.getLogger(__name__).debug(
                    "Security triage in repair skipped: %s", exc
                )

        # Convert governance CheckResult findings to Finding objects
        from saturnday.repair.finding_category import check_name_to_category
        findings: list[Finding] = []
        for cr in pack.check_results:
            for fd in cr.findings:
                findings.append(Finding(
                    check=cr.name,
                    severity=cr.severity,
                    file=fd.get("file", ""),
                    line=fd.get("line"),
                    message=fd.get("pattern", fd.get("message", cr.name)),
                    kind=fd.get("kind", cr.name),
                    remediation=fd.get("remediation"),
                    category=check_name_to_category(cr.name),
                ))
        # Filter out findings in denied paths from policy
        _policy_path = repo_path / ".saturnday-policy.yaml"
        if _policy_path.is_file():
            try:
                import yaml as _yaml
                _raw_policy = _yaml.safe_load(_policy_path.read_text(encoding="utf-8")) or {}
                _denied = _raw_policy.get("scope", {}).get("denied_paths", [])
                if _denied:
                    from saturnday.scope_rules import matches_path_patterns
                    _pre_denied = len(findings)
                    findings = [f for f in findings if not matches_path_patterns(f.file, _denied)]
                    _denied_count = _pre_denied - len(findings)
                    if _denied_count:
                        logger.info("Excluded %d finding(s) in denied paths", _denied_count)
            except Exception as _dp_exc:
                logger.debug("Denied-path filtering failed: %s", _dp_exc)

        return findings, "repo"

    # No SKILL.md and no git — try skill scan anyway
    from saturnday.guard.cloud_scanner import scan_skill
    result = scan_skill(repo_path)
    return result.findings, "skill"


def _write_repair_plan_md(
    repo_path: Path,
    tickets: list,
    total_findings: int,
) -> Path:
    """Write a detailed repair plan to .saturnday/repair-plan.md.

    Args:
        repo_path: Path to the repository root.
        tickets: List of ``RepairTicket`` objects.
        total_findings: Total pre-repair finding count.

    Returns:
        Path to the written plan file.
    """
    from saturnday.remediation_guidance import get_guidance

    sat_dir = repo_path / ".saturnday"
    sat_dir.mkdir(parents=True, exist_ok=True)
    plan_path = sat_dir / "repair-plan.md"

    _ENV_KINDS = {
        "dependency_declaration", "import_check",
        "package_not_importable", "declared_not_installed",
    }

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "# Repair Plan\n",
        f"**Repo:** {repo_path}  ",
        f"**Date:** {now}  ",
        f"**Findings:** {total_findings}  ",
        f"**Tickets:** {len(tickets)}\n",
        "## Summary\n",
        "| Ticket | Finding | File | Severity |",
        "|--------|---------|------|----------|",
    ]
    for t in tickets:
        tid = getattr(t, "ticket_id", "?")
        kind = _humanize_finding_kind(getattr(t, "finding_kind", "?"))
        fp = getattr(t, "file_path", "")
        sev = getattr(t, "severity", "medium")
        lines.append(f"| {tid} | {kind} | {Path(fp).name if fp else '—'} | {sev} |")

    lines.append("\n## Ticket Details\n")

    for t in tickets:
        tid = getattr(t, "ticket_id", "?")
        kind = getattr(t, "finding_kind", "?")
        fp = getattr(t, "file_path", "")
        sev = getattr(t, "severity", "medium")
        label = _humanize_finding_kind(kind)

        lines.append(f"### {tid}: {label} in {Path(fp).name if fp else 'repo'}\n")
        lines.append(f"**Finding:** {label}  ")
        lines.append(f"**File:** `{fp}`  ")
        lines.append(f"**Severity:** {sev}\n")

        evidence = getattr(t, "evidence", [])
        if evidence:
            lines.append("**Evidence:**")
            for e in evidence:
                lines.append(f"- `{e}`")
            lines.append("")

        # Get remediation — from ticket or fallback to registry
        rem = getattr(t, "remediation", None)
        guidance = get_guidance(kind)
        if rem and isinstance(rem, dict):
            lines.append("**How to fix:**")
            if rem.get("why"):
                lines.append(f"- **Why:** {rem['why']}")
            if rem.get("fix"):
                lines.append(f"- **Fix:** {rem['fix']}")
            if rem.get("patch"):
                lines.append(f"- **Patch:** `{rem['patch']}`")
            lines.append("")
        elif rem and isinstance(rem, str):
            lines.append(f"**How to fix:** {rem}\n")
        elif guidance:
            lines.append("**How to fix:**")
            lines.append(f"- **Why:** {guidance.why_it_matters}")
            lines.append(f"- **Fix:** {guidance.how_to_fix}")
            if guidance.patch_template:
                lines.append(f"- **Patch:** `{guidance.patch_template}`")
            lines.append("")

        if kind in _ENV_KINDS:
            lines.append("**Dependencies:** Requires package installation in the current environment.\n")

        lines.append("---\n")

    plan_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"  Repair plan saved to: {plan_path}")
    return plan_path


def guided_repair(
    repo_path: Path,
    session: "Session | None" = None,
    coder_config=None,
    hint: str = "",
) -> int:
    """Interactive Repair flow: scan -> plan -> confirm -> fix -> re-scan -> next steps.

    Automatically chooses the right scanner:
    - Repos with SKILL.md: OpenClaw skill scan (8 security checks)
    - Git repos without SKILL.md: full governance review (30+ checks)

    Args:
        repo_path: Path to the repository root.
        session: Active session for backend memory.
        coder_config: Optional coder config for LLM progress messages.
        hint: Original user input text.  Used to parse a category qualifier
            (e.g. "fix all security issues") so that only matching findings
            are repaired.  Empty string means repair all categories.

    Returns:
        Exit code (0 = all fixed, 1 = failures).
    """
    _t_total_start = time.monotonic()
    print(f"\n  Scanning {repo_path} for findings...")
    _t0 = time.monotonic()
    pre_findings, scan_mode = _scan_for_repair(repo_path, coder_config=coder_config)
    logger.info("TIMING %-30s %.2fs", "repair_initial_scan", time.monotonic() - _t0)
    pre_count = len(pre_findings)

    if scan_mode == "skill":
        print(f"  Scan mode: OpenClaw skill (SKILL.md found)")
    else:
        print(f"  Scan mode: full repo review")

    if not pre_findings:
        print("  No findings. Nothing to repair.")
        return 0

    # Generate tickets before showing the plan
    from saturnday._types import CoderConfig
    from saturnday.repair.repair_tickets import generate_repair_tickets
    from saturnday.repair.repair_runner import run_repair_batch
    from saturnday.coder_adapter import call_coder

    # Check for environment findings that need package installation first
    _ENV_INSTALL_KINDS = {"declared_not_installed", "package_not_importable"}
    env_findings = [f for f in pre_findings if f.kind in _ENV_INSTALL_KINDS]
    if env_findings:
        # Extract package names from findings
        import re as _re
        _imp_re = _re.compile(r'(?:from\s+|import\s+)([A-Za-z_][A-Za-z0-9_]*)')
        pkgs: set[str] = set()
        for f in env_findings:
            m = _imp_re.search(f.message or "")
            if m:
                pkgs.add(m.group(1))
        if pkgs:
            # Read optional-dependency group names from pyproject.toml
            extras: list[str] = []
            pyproject = repo_path / "pyproject.toml"
            if pyproject.is_file():
                try:
                    import re as _re2
                    for m in _re2.finditer(
                        r'\[project\.optional-dependencies\]\s*\n([^\[]+)',
                        pyproject.read_text(),
                    ):
                        for line in m.group(1).splitlines():
                            if "=" in line:
                                extras.append(line.split("=")[0].strip())
                except Exception:
                    pass

            print(f"\n  {len(env_findings)} finding(s) need packages installed in your environment.")
            print("  These are declared in your project but not installed.\n")
            for pkg in sorted(pkgs):
                print(f"    - {pkg}")
            if extras:
                extras_str = ",".join(extras)
                print(f"\n  Install with:")
                print(f"    pip install -e '.[{extras_str}]'")
            print(f"\n  Or install individually:")
            print(f"    pip install {' '.join(sorted(pkgs))}")
            print()
            print("  Options:")
            print("    - Press Enter after installing to re-scan")
            print("    - Type 'skip' to continue with code fixes only")
            print("    - Type anything else to share an error or ask a question")
            print()

            installed = False
            while True:
                try:
                    response = input("  > ").strip()
                except (EOFError, KeyboardInterrupt):
                    print("\n  Skipping install — continuing with code fixes only.")
                    break
                if not response:
                    # User pressed Enter — re-scan
                    print("\n  Re-scanning after install...")
                    pre_findings, scan_mode = _scan_for_repair(repo_path, coder_config=coder_config)
                    pre_count = len(pre_findings)
                    env_remaining = sum(1 for f in pre_findings if f.kind in _ENV_INSTALL_KINDS)
                    print(f"  Findings: {pre_count} ({env_remaining} still need installation)")
                    if not pre_findings:
                        print("  All findings resolved by installation.")
                        return 0
                    if env_remaining == 0:
                        print("  All packages installed. Continuing with code fixes.")
                        installed = True
                        break
                    print("  Some packages still not installed. Try again or type 'skip'.")
                    continue
                if response.lower() == "skip":
                    print("  Skipping install — continuing with code fixes only.")
                    break
                # User typed something — they might be sharing an error
                # Try to help based on common patterns
                if "could not find" in response.lower() or "error" in response.lower():
                    print("\n  That package might not be on PyPI or might need a different name.")
                    print("  Some packages need special installation:")
                    print("    - pythonocc-core: conda install -c conda-forge pythonocc-core")
                    print("    - OCC: same as pythonocc-core (conda only)")
                    print("  Try the conda command, then press Enter to re-scan.")
                    print()
                else:
                    print(f"\n  Got it. Try installing and press Enter, or type 'skip'.")
                    print()

    # Filter out environment findings — they can't be fixed by code edits
    code_findings = [f for f in pre_findings if f.kind not in _ENV_INSTALL_KINDS]
    # Initialise intent-filter tracking variables (may be overwritten below)
    excluded_by_intent: list = []
    _excl_parts: list[str] = []
    exempted: int = 0
    if not code_findings:
        if pre_findings:
            print(f"\n  All {len(pre_findings)} remaining findings need package installation, not code fixes.")
            print("  Install the missing packages and re-run.")
        else:
            print("  No findings. Nothing to repair.")
        return 0

    # Load policy exemptions BEFORE generating tickets
    policy_path = repo_path / ".saturnday-policy.yaml"
    exempt_kinds: set[str] = set()
    if policy_path.is_file():
        try:
            # Parse YAML without requiring pyyaml — simple key: value format
            raw = policy_path.read_text(encoding="utf-8")
            try:
                import yaml
                policy_data = yaml.safe_load(raw)
            except ImportError:
                # Fallback: parse simple YAML manually
                policy_data = {}
                in_list = False
                current_key = ""
                items: list[str] = []
                for pline in raw.splitlines():
                    stripped = pline.strip()
                    if stripped.endswith(":") and not stripped.startswith("-"):
                        if current_key and items:
                            policy_data[current_key] = items
                        current_key = stripped[:-1].strip()
                        items = []
                        in_list = True
                    elif in_list and stripped.startswith("- "):
                        items.append(stripped[2:].strip())
                if current_key and items:
                    policy_data[current_key] = items

            # Fix 53.e: use shared loader (policy_data from manual parser above
            # may be incomplete — prefer the full YAML loader if available)
            from saturnday.policy_loader import load_policy as _lp3, validated_expected_findings as _vef3
            _full_policy = _lp3(repo_path)
            if _full_policy:
                exempt_kinds = _vef3(_full_policy)
            elif policy_data:
                # Fallback to manual parser output
                exempt_kinds = {e for e in policy_data.get("expected_findings", []) if isinstance(e, str)}
            else:
                exempt_kinds = set()
            if exempt_kinds:
                print(f"  Policy exemptions: {', '.join(sorted(exempt_kinds))}")
                before_count = len(code_findings)
                code_findings = [f for f in code_findings if f.kind not in exempt_kinds]
                exempted = before_count - len(code_findings)
                if exempted:
                    print(f"  Exempted {exempted} finding(s) by policy")
                pre_count = len(code_findings)
        except Exception as exc:
            print(f"  Warning: could not load .saturnday-policy.yaml: {exc}")

    if not code_findings:
        print("  All findings exempted by policy. Nothing to repair.")
        return 0

    # Intent-based category filtering — honour qualifiers like "fix security issues"
    from saturnday.repair.finding_category import parse_repair_categories
    requested_categories = parse_repair_categories(hint)
    if requested_categories is not None:
        _pre_filter_count = len(code_findings)
        excluded_by_intent = [f for f in code_findings if f.category not in requested_categories]
        code_findings = [f for f in code_findings if f.category in requested_categories]
        if excluded_by_intent:
            # Group excluded by category for display
            _excl_cats: dict[str, int] = {}
            for f in excluded_by_intent:
                _excl_cats[f.category] = _excl_cats.get(f.category, 0) + 1
            _excl_parts = [f"{count} {cat}" for cat, count in sorted(_excl_cats.items())]
            print(f"  Filtering to {', '.join(requested_categories)} findings: {len(code_findings)} of {_pre_filter_count}")
            print(f"  Excluded by intent: {', '.join(_excl_parts)}")

    if not code_findings:
        print("  No findings in the requested categories. Nothing to repair.")
        return 0

    tickets = generate_repair_tickets(code_findings, repo_path=repo_path)
    pre_count = len(code_findings)

    # Show ALL tickets in terminal
    print(f"\n  Found {pre_count} finding(s) across {len(tickets)} repair ticket(s):")
    print()
    for ticket in tickets:
        tid = str(getattr(ticket, "ticket_id", "?"))
        kind = str(getattr(ticket, "finding_kind", "unknown"))
        fp = str(getattr(ticket, "file_path", ""))
        label = _humanize_finding_kind(kind)
        file_display = Path(fp).name if fp else ""
        print(f"    {tid:12s} {label:<38s} {file_display}")
    print()

    # Write full repair plan to MD
    _write_repair_plan_md(repo_path, tickets, pre_count)

    backend = select_backend(session)
    if not backend:
        return 1

    try:
        proceed = input("  Proceed with this repair plan? [Y/n] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return 0
    if proceed == "n":
        print("  Aborted.")
        return 0

    config = _build_coder_config(backend)

    from saturnday.coder_adapter import is_cli_backend

    try:
        from saturnday.role_modes import load_role_prompt
        repair_role_prompt = load_role_prompt("repair")
    except (FileNotFoundError, ImportError):
        repair_role_prompt = (
            "You are a code repair agent. Fix the issue described. "
            "Edit the file directly. Do not ask questions. "
            "SCOPE CONSTRAINT: Fix ONLY the specific finding. Do NOT refactor, add features, or change unrelated code."
        )

    # Load senior engineering corpus for quality standards
    try:
        from saturnday.context_assembler import build_system_prompt
        standards_dir = _find_standards_dir(repo_path)
        corpus = build_system_prompt(str(standards_dir))
        repair_system_prompt = f"{corpus}\n\n{repair_role_prompt}"
    except Exception:
        repair_system_prompt = repair_role_prompt

    # Pre-repair repo analysis — understand the codebase before fixing
    print_role_stage("analysing repo")
    try:
        from saturnday.role_modes import invoke_role
        analyst_result = invoke_role(
            "repo_analyst",
            f"Analyse the codebase at {repo_path}. Summarise: architecture, module boundaries, local code style, test patterns, and dependencies. This will guide the repair coder.",
            coder_config=config,
            repo_path=repo_path,
        )
        if analyst_result.success and analyst_result.output:
            # Include repo context in the repair system prompt
            repo_context = analyst_result.output[:2000]  # Cap to avoid token bloat
            repair_system_prompt = f"{repair_system_prompt}\n\nREPO CONTEXT FROM ANALYST:\n{repo_context}"
    except Exception:
        pass

    cli_mode = is_cli_backend(config)

    if cli_mode:
        # CLI backends read CLAUDE.md/AGENTS.md — no need for 25K corpus in prompt
        def coder_fn(prompt: str, file_path: str, repo_path_: Path) -> str:
            """Run CLI coder as a file-editing agent."""
            full_prompt = (
                f"Fix this issue in {file_path}:\n{prompt}\n\n"
                f"Follow the governance rules in CLAUDE.md. "
                f"Edit the file directly. Ensure consistency with the rest of the codebase — "
                f"same language, same patterns, same style.\n\n"
                f"SCOPE CONSTRAINT: Fix ONLY this specific finding. Do NOT refactor surrounding code, "
                f"add new features, change unrelated logic, or make improvements beyond the exact fix. "
                f"One finding, one surgical fix, nothing else."
            )
            return call_coder(config, [
                {"role": "user", "content": full_prompt}
            ], repo_path_, agent_mode=True)
    else:
        # API backends return text that must be written to disk.
        def coder_fn(prompt: str, file_path: str, repo_path_: Path) -> str:
            """Call API coder and return repaired file content."""
            current = ""
            fp = Path(file_path)
            if fp.exists():
                current = fp.read_text(encoding="utf-8", errors="replace")
            messages = [
                {"role": "system", "content": repair_system_prompt},
                {"role": "user", "content": (
                    f"Current file ({file_path}):\n```\n{current}\n```\n\n"
                    f"Repair:\n{prompt}\n\n"
                    f"Return ONLY the complete repaired file content, "
                    f"no explanations, no markdown fences:"
                )},
            ]
            return call_coder(config, messages, repo_path_)

    # Build a scan function matching the scanner used for initial scan.
    # coder_config is captured from the enclosing guided_repair scope so that
    # triage runs consistently during both initial scan and post-repair checks.
    def repair_scan_fn(path: Path) -> list:
        findings, _ = _scan_for_repair(path, coder_config=coder_config)
        return findings

    def _repair_progress(ticket_id: str, kind: str, status: str) -> None:
        short = status.split("(")[0].strip()
        icons = {"RUNNING": "\u2192", "FIXED": "\u2713", "PARTIAL": "~", "FAILED": "\u2717", "SKIP": "\u2298"}
        icon = icons.get(short, "?")
        label = _humanize_finding_kind(kind)
        if "(" in status:
            reason = status[status.index("("):]
            print(f"    {ticket_id:12s} {icon} {short:8s} {label}  {reason}")
        else:
            print(f"    {ticket_id:12s} {icon} {short:8s} {label}")

    # Enrich repair tickets with design decisions (same pattern as planner)
    print("  Enriching repair tickets...")
    _t0 = time.monotonic()
    try:
        from saturnday.repair.repair_runner import enrich_repair_tickets
        tickets = enrich_repair_tickets(tickets, config, repo_path)
        print(f"  Enriched {len(tickets)} ticket(s)")
    except Exception as exc:
        logger.warning("Repair enrichment failed: %s", exc)
    logger.info("TIMING %-30s %.2fs", "repair_enrichment", time.monotonic() - _t0)

    repair_ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_dir = repo_path / ".saturnday" / "repairs" / repair_ts
    print(f"\n  Repairing {len(tickets)} ticket(s)...\n")
    _t0 = time.monotonic()
    run_result = run_repair_batch(
        tickets, repo_path, coder_fn,
        scan_fn=repair_scan_fn,
        cli_mode=cli_mode,
        progress_fn=_repair_progress,
    )
    logger.info("TIMING %-30s %.2fs", "repair_batch", time.monotonic() - _t0)

    print(f"\n  {'─' * 48}")
    print(f"  Repair summary:")
    if requested_categories is not None:
        print(f"    Excluded by intent:  {len(excluded_by_intent)} ({', '.join(_excl_parts)})")
    if exempted > 0:
        print(f"    Excluded by policy:  {exempted}")
    print(f"    Repaired:            {run_result.fixed}")
    print(f"    Not auto-fixable:    {run_result.failed}")
    if run_result.partial > 0:
        print(f"    Partially fixed:     {run_result.partial}")
    if run_result.stopped_early:
        print(f"  Stopped: {run_result.stop_reason}")
    print()

    # Role passes (advisory — never block on failure)
    _t0 = time.monotonic()
    try:
        from saturnday.repair.repair_runner import run_repair_role_passes
        from saturnday._types import CoderConfig as _CoderConfig
        repair_config = _CoderConfig(backend=backend)  # type: ignore[arg-type]
        run_repair_role_passes(run_result, output_dir, repo_path, coder_config=repair_config)
        _show_role_pass_results(output_dir)
    except Exception:
        pass
    logger.info("TIMING %-30s %.2fs", "repair_role_passes", time.monotonic() - _t0)

    # Simplifier pass — ask coder to remove unnecessary complexity
    if cli_mode and run_result.fixed > 0:
        print("\n  Running simplifier pass...")
        try:
            simplify_prompt = (
                "You are a senior code reviewer. Review the recent changes in this repository.\n\n"
                "SIMPLIFICATION — for each changed file:\n"
                "1. Delete any helper function that is defined but only used once — inline it.\n"
                "2. Delete any comment that describes WHAT the code does (noise). "
                "Keep only comments that explain WHY a decision was made.\n"
                "3. Delete any abstraction that does not reduce complexity.\n"
                "4. If a file was rewritten from scratch when a small edit would have worked, "
                "revert to the small edit.\n\n"
                "CONSTRAINT COMPLIANCE — check the diff against senior standards:\n"
                "5. Are tests distributed UNEVENLY? (many for risky logic, few for trivial code) "
                "If tests are evenly spread, delete the trivial ones.\n"
                "6. Does every comment explain WHY, not WHAT? Remove any that restate the code.\n"
                "7. Is the README proportional to the code complexity? "
                "A 200-line script should not have a feature matrix.\n"
                "8. Were any unnecessary dependencies, classes, or abstraction layers introduced? Remove them.\n"
                "9. Is there any dead code — functions defined but never called? Delete them.\n\n"
                "Make the code simpler and more senior. Only remove, inline, or tighten. Do not add anything."
            )
            call_coder(config, [
                {"role": "user", "content": simplify_prompt}
            ], repo_path, agent_mode=True)
            print("  Simplifier pass complete.")
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning("Simplifier pass failed: %s", exc)

    # Auto-commit repair results
    if run_result.fixed > 0 or run_result.partial > 0:
        try:
            import subprocess as _sp
            # Check if there are changes to commit
            status = _sp.run(["git", "-C", str(repo_path), "status", "--porcelain"],
                           capture_output=True, text=True, timeout=10)
            if status.stdout.strip():
                _sp.run(["git", "-C", str(repo_path), "add", "-A"], capture_output=True, timeout=10)
                _sp.run(["git", "-C", str(repo_path), "commit", "-m",
                        f"saturnday repair: {run_result.fixed} fixed, {run_result.partial} partial"],
                       capture_output=True, timeout=10)
                print("  Repair changes committed.")
        except Exception:
            pass

    # Re-scan to verify repairs
    print("\n  Re-scanning to verify repairs...")
    _t0 = time.monotonic()
    post_findings, _ = _scan_for_repair(repo_path, coder_config=coder_config)
    logger.info("TIMING %-30s %.2fs", "repair_verify_scan", time.monotonic() - _t0)
    post_count = len(post_findings)
    print(f"  Before: {pre_count} findings")
    print(f"  After:  {post_count} findings")
    if post_count < pre_count:
        print(f"  Resolved: {pre_count - post_count}")
    if post_count == 0:
        print("  All findings resolved.")

    # Progress message — what repair achieved
    if coder_config and pre_count > post_count:
        try:
            from saturnday.ticket_runner import _generate_progress_message
            _fixed_kinds = sorted({t.finding_kind for t in tickets if any(r.status == "fixed" for r in run_result.results if r.ticket_id == t.ticket_id)})
            _repair_msg = _generate_progress_message(
                coder_config, repo_path,
                f"Repair auto-fixed {pre_count - post_count} findings (before: {pre_count}, after: {post_count}). "
                f"Fixed kinds: {', '.join(_fixed_kinds[:5]) if _fixed_kinds else 'various'}. "
                f"Write one sentence: what would have happened if an AI coder shipped this code without these fixes?",
            )
            if _repair_msg:
                print(f"\n  >> {_repair_msg}")
        except Exception:
            pass

    # Evidence path
    if output_dir.is_dir():
        print(f"\n  Evidence: {output_dir}")

    # Full governance re-scan for the end report
    post_pack = None
    _t0 = time.monotonic()
    try:
        from saturnday.governance import run_full_repo_review
        print("\n  Running final governance scan...")
        post_pack, _ = run_full_repo_review(repo_path)
        total_post = sum(len(cr.findings) for cr in post_pack.check_results)
        print(f"  Final governance: {post_pack.disposition} ({total_post} findings)")
    except Exception:
        pass
    logger.info("TIMING %-30s %.2fs", "repair_final_governance", time.monotonic() - _t0)

    # Generate repair report
    try:
        from saturnday.reporting import generate_repair_report
        report_path = generate_repair_report(
            pre_findings=pre_findings,
            post_pack=post_pack,
            repair_result=run_result,
            tickets=tickets,
            evidence_dir=output_dir,
            repo_path=repo_path,
        )
        print(f"\n  Full report: {report_path}")
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning("Failed to generate repair report: %s", exc)

    # Next-steps guidance
    print("\n  What to do next:")
    if post_count > 0:
        print("    'fix the findings'    -- repair remaining issues")
        if scan_mode == "skill":
            print("    'scan this skill'     -- full scan")
        else:
            print("    'review this repo'    -- full scan")
    else:
        if scan_mode == "skill":
            print("    'scan this skill'     -- verify clean state")
        else:
            print("    'review this repo'    -- verify clean state")
        print("    'check the last commit' -- governance on changes")
    print()

    logger.info("TIMING %-30s %.2fs", "guided_repair_total", time.monotonic() - _t_total_start)
    return 0 if run_result.failed == 0 else 1
