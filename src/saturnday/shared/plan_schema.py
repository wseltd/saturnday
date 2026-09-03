"""Single source of truth for plan JSON schema.

Used by both the planner prompt and the plan validator.
"""

from __future__ import annotations

from typing import Any

PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["version", "project_id", "tickets"],
    "properties": {
        "version": {"type": "integer", "const": 1},
        "project_id": {"type": "string", "minLength": 1},
        "notes": {"type": "string"},
        "mode": {"type": "string", "enum": ["generation", "remediation"]},
        "acceptance_cmd": {"type": "string"},
        "acceptance_setup": {"type": "array", "items": {"type": "string"}},
        "is_runnable_product": {"type": "boolean"},
        # Fix 76: persisted product mode / dependency profile / proof realism.
        "operating_mode": {
            "type": "string",
            "enum": [
                "legacy_unclassified",
                "library",
                "cli_tool",
                "web_service",
                "worker",
                "pipeline",
                "frontend",
                "storage_only",
            ],
        },
        "dependency_profile": {
            "type": "string",
            "enum": [
                "self_contained",
                "local_dependencies",
                "external_dependencies",
            ],
        },
        "proof_realism": {
            "type": "string",
            "enum": ["production_intent", "seeded_demo"],
        },
        "external_dependencies": {
            "type": "array",
            "items": {"type": "string"},
        },
        "local_proof_cmd": {"type": "string"},
        "live_proof_cmd": {"type": "string"},
        "demo_seed_source": {"type": "string"},
        "operator_disclaimer": {"type": "string"},
        "clarification_record": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["trigger_kind", "question"],
                "properties": {
                    "trigger_kind": {"type": "string"},
                    "question": {"type": "string"},
                    "answer": {"type": "string"},
                    "blocking": {"type": "boolean"},
                },
            },
        },
        # Phase 1: testing strategy negotiated for external-dependency products.
        "testing_strategy": {
            "type": "string",
            "enum": [
                "unspecified",
                "live_credentials_gated",
                "local_emulator",
                "oss_substitute",
                "generated_fake",
                "seeded_demo",
                "recorded_fixture",
            ],
        },
        # Phase 7: provenance of local_proof_cmd, set at plan time.
        "proof_resolution_source": {
            "type": "string",
            "enum": [
                "none",
                "planner_heuristic",
                "planner_gap",
                "coder_plan_time",
                "coder_post_execution",
                "operator_supplied",
                "operator_edited",
                "clarification_strategy",
                "operator_targeted_answer",
            ],
        },
        "definition_of_done": {
            "type": "array",
            "items": {
                "type": "string",
                "enum": ["tickets_applied", "all_tickets_passed"],
            },
        },
        "stop_conditions": {
            "type": "array",
            "items": {"type": "string"},
        },
        "max_project_tickets": {
            "type": "integer",
            "minimum": 1,
        },
        "default_timeout_seconds": {
            "type": "integer",
            "minimum": 1,
        },
        "default_retry_limit": {
            "type": "integer",
            "minimum": 0,
        },
        "phases": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["phase_id", "ticket_ids"],
                "properties": {
                    "phase_id": {"type": "string"},
                    "name": {"type": "string"},
                    "ticket_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
            },
        },
        "tickets": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["ticket_id", "goal"],
                "properties": {
                    "ticket_id": {"type": "string", "minLength": 1},
                    "goal": {"type": "string", "minLength": 1},
                    "verify_cmd": {"type": "string"},
                    "acceptance_criteria": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "dependencies": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "out_of_scope": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "evidence_required": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "failure_mode": {"type": "string"},
                    "if_blocked": {"type": "string"},
                    "atomic": {"type": "boolean"},
                    "scope": {
                        "type": "object",
                        "properties": {
                            "allowed_globs": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "forbidden_globs": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "budgets": {
                                "type": "object",
                                "properties": {
                                    "max_files_changed": {"type": "integer", "minimum": 1},
                                    "max_total_diff_lines": {"type": "integer", "minimum": 1},
                                    "max_per_file_diff_lines": {"type": "integer", "minimum": 1},
                                },
                            },
                        },
                    },
                },
            },
        },
    },
}


def schema_for_prompt() -> str:
    """Return a human-readable schema description for inclusion in planner prompts.

    This is NOT the JSON schema — it's a natural-language description
    optimized for LLM consumption.
    """
    return """\
You must output a valid JSON plan with this structure:

{
  "version": 1,
  "project_id": "<short-kebab-case-id>",
  "notes": "<context for the coder — repo layout, conventions, constraints>",
  "mode": "generation" | "remediation",
  "operating_mode": "library" | "cli_tool" | "web_service" | "worker" | "pipeline" | "frontend" | "storage_only",
  "dependency_profile": "self_contained" | "local_dependencies" | "external_dependencies",
  "proof_realism": "production_intent" | "seeded_demo",
  "external_dependencies": ["<required iff dependency_profile=external_dependencies, e.g. 'openai_api', 'stripe', 'github_api'>"],
  "local_proof_cmd": "<REQUIRED for declared mode: command that proves the product works locally without third-party network or live credentials>",
  "live_proof_cmd": "<optional, only meaningful when dependency_profile=external_dependencies; runs against real services when LIVE_PROOF=1>",
  "demo_seed_source": "<required iff proof_realism=seeded_demo: name of the fixture/script that seeds the demo>",
  "operator_disclaimer": "<required when proof_realism=seeded_demo OR operating_mode=storage_only OR dependency_profile=external_dependencies; surfaced verbatim in the run summary>",
  "acceptance_setup": ["<optional: setup steps needed before realistic acceptance, e.g. 'download SDO imagery dataset', 'install rdkit-pypi', 'start local API server'>"],
  "definition_of_done": ["all_tickets_passed"],
  "stop_conditions": [],
  "max_project_tickets": null,
  "phases": [
    {"phase_id": "phase-1", "name": "Build", "ticket_ids": ["T001", "T002"]}
  ],
  "tickets": [
    {
      "ticket_id": "T001",
      "goal": "Clear, specific instruction for what to create or change.",
      "verify_cmd": "pytest tests/test_foo.py -q",
      "acceptance_criteria": ["function parse_event exists in src/parser.py", "class Event exists in src/models.py", "file src/models.py exists", "test test_parse_event exists in tests/test_parser.py"],
      "out_of_scope": ["Do not modify existing tests", "Do not change the API surface"],
      "evidence_required": ["New test file passes", "Function exists in module"],
      "failure_mode": "coder_error",
      "if_blocked": "Check dependency T001 output, verify function signature",
      "dependencies": [],
      "scope": {
        "allowed_globs": ["src/**", "tests/**"],
        "forbidden_globs": ["*.lock"],
        "budgets": {
          "max_files_changed": 2,
          "max_total_diff_lines": 80,
          "max_per_file_diff_lines": 60
        }
      }
    }
  ]
}

Rules:
- Each ticket must change at most 2 files and 80 lines total.
- Each ticket must have at most 3 dependencies.
- ticket_id must be unique across the plan.
- dependencies must reference ticket_ids defined in the plan.
- Goal must be specific and actionable — not vague directions.
- verify_cmd should be a concrete command that validates the ticket.
- operating_mode (plan-level, REQUIRED): the concrete runtime shape of the product.  Pick exactly one: library / cli_tool / web_service / worker / pipeline / frontend / storage_only.  Do NOT emit "legacy_unclassified" — that value exists only as a compatibility state for plans loaded from pre-Fix-76 files.
- dependency_profile (plan-level, REQUIRED): self_contained (no externals), local_dependencies (services you can stand up locally — Postgres, Redis, Docker Compose), or external_dependencies (third-party APIs, cloud services, payment processors, anything requiring non-trivial credentials).
- proof_realism (plan-level, REQUIRED): production_intent (proof exercises real-shaped data) or seeded_demo (proof exercises the operator path with explicitly seeded fixture data — operator_disclaimer required).
- local_proof_cmd (plan-level, REQUIRED for any declared operating_mode): canonical primary blocking proof.  Runs locally without third-party network, live credentials, production databases, or live payment/email/external API calls.  THIS replaces the legacy acceptance_cmd field for declared-mode plans.  Per-mode rules apply (Fix 73): web_service must start the real service process and issue real HTTP; frontend must build AND serve AND assert at least one operator-visible behaviour; trivial proofs like 'python -c "import x"' alone or 'pytest tests/ -q' alone are rejected.
- live_proof_cmd (plan-level, optional, non-blocking): runs against real external services when LIVE_PROOF=1 is set.  Only meaningful for external_dependencies profile.  Failure does NOT block plan completion or trigger repair.
- external_dependencies (plan-level, REQUIRED iff dependency_profile=external_dependencies): names of the third-party services the product calls.
- demo_seed_source (plan-level, REQUIRED iff proof_realism=seeded_demo): the fixture/script name that seeds the demo path.
- operator_disclaimer (plan-level, REQUIRED when proof_realism=seeded_demo OR operating_mode=storage_only OR dependency_profile=external_dependencies): honest one-line text surfaced verbatim in the run summary, e.g. "Demo mode — uses fixtures from seeds/demo.json. NOT production data." or "Storage-only backend — no behaviour beyond persistence."
- acceptance_cmd (DEPRECATED): only honoured for legacy_unclassified plans loaded from pre-Fix-76 files.  Newly generated plans must use local_proof_cmd; setting both is rejected.
- acceptance_setup (plan-level, optional): list of setup steps needed before realistic acceptance testing, such as downloading datasets, installing heavy optional dependencies, fetching model weights, or starting services.  Leave empty for lightweight products that need no special setup.
- Phases group tickets for progress tracking (optional).
- definition_of_done: "all_tickets_passed" requires all tickets to PASS.
- stop_conditions: normalized markers that abort the run if matched.
- out_of_scope: explicitly state what the ticket must NOT do.
- evidence_required: what artifacts must exist after the ticket completes.
- failure_mode: expected failure type (coder_error, spec_ambiguity, dependency_missing, governance_block).
- if_blocked: what the operator should do if the ticket fails all retries.
- The ticket rubric aligns with the engineering standards corpus: tickets should be written so code produced to satisfy them naturally meets the engineering constitution, testing rules, and senior judgment rules.
"""
