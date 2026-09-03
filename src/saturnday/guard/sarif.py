"""Convert scanner findings to SARIF format for GitHub code scanning.

Produces SARIF v2.1.0 compatible output that can be uploaded to
GitHub's code scanning API via ``github/codeql-action/upload-sarif``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from saturnday.guard.cloud_scanner import Finding, SkillScanResult

SARIF_VERSION = "2.1.0"
SARIF_SCHEMA = "https://json.schemastore.org/sarif-2.1.0.json"
TOOL_NAME = "saturnday-guard"
TOOL_VERSION = "0.1.0"

# Map our severity levels to SARIF levels
_SEVERITY_TO_SARIF: dict[str, str] = {
    "high": "error",
    "medium": "warning",
    "low": "note",
    "info": "note",
}


def findings_to_sarif(
    findings: list[Finding],
    base_path: str = "",
) -> dict[str, Any]:
    """Convert a list of findings to a SARIF document.

    Args:
        findings: Scanner findings to convert.
        base_path: Base path prefix for file URIs (e.g., the skill directory).

    Returns:
        A SARIF v2.1.0 document as a dict.
    """
    results: list[dict[str, Any]] = []
    rule_ids: set[str] = set()

    for finding in findings:
        rule_id = finding.kind or finding.check
        rule_ids.add(rule_id)

        sarif_result: dict[str, Any] = {
            "ruleId": rule_id,
            "level": _SEVERITY_TO_SARIF.get(finding.severity, "warning"),
            "message": {"text": finding.message},
        }

        # Add location if file is specified
        if finding.file:
            uri = f"{base_path}/{finding.file}" if base_path else finding.file
            location: dict[str, Any] = {
                "physicalLocation": {
                    "artifactLocation": {"uri": uri},
                },
            }
            if finding.line is not None:
                location["physicalLocation"]["region"] = {
                    "startLine": finding.line,
                }
            sarif_result["locations"] = [location]

        results.append(sarif_result)

    # Build rule definitions
    rules = [
        {
            "id": rid,
            "shortDescription": {"text": rid.replace("_", " ").title()},
        }
        for rid in sorted(rule_ids)
    ]

    return {
        "$schema": SARIF_SCHEMA,
        "version": SARIF_VERSION,
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": TOOL_NAME,
                        "version": TOOL_VERSION,
                        "rules": rules,
                    },
                },
                "results": results,
            },
        ],
    }


def skill_result_to_sarif(result: SkillScanResult) -> dict[str, Any]:
    """Convert a SkillScanResult to a SARIF document.

    Args:
        result: The scan result to convert.

    Returns:
        A SARIF v2.1.0 document as a dict.
    """
    return findings_to_sarif(result.findings, base_path=result.relative_path)


def write_sarif(
    sarif_doc: dict[str, Any],
    output_path: str | Path,
) -> Path:
    """Write a SARIF document to a file.

    Args:
        sarif_doc: The SARIF document to write.
        output_path: Path for the output file.

    Returns:
        Path to the written file.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(sarif_doc, indent=2) + "\n",
        encoding="utf-8",
    )
    return output_path
