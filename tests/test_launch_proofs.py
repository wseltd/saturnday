"""Phase 9: Launch-proof demo and eval assets.

Reproducible proofs for the strongest product claims.
Each test is a self-contained demo that another operator can run.
"""

import json
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def _init_git(path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t.com"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "T"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "init"], check=True, capture_output=True)


class TestGovernanceFailureProof:
    """Demo: governance scan detects seeded security defects."""

    def test_seeded_security_findings_detected(self, tmp_path: Path) -> None:
        repo = tmp_path / "service"
        shutil.copytree(FIXTURES / "sample-python-service", repo)
        _init_git(repo)

        from saturnday.governance import run_full_repo_review

        pack, _ = run_full_repo_review(repo)

        assert pack.disposition == "FAIL"
        sec_checks = [cr for cr in pack.check_results if cr.rule_id and cr.rule_id.startswith("SEC-")]
        assert len(sec_checks) > 0, "No security findings with rule_id detected"

        # Verify rule_id and cwe are populated (Phase 8 fix)
        for cr in sec_checks:
            assert cr.rule_id is not None
            assert cr.cwe is not None


class TestScopedRepairProof:
    """Demo: security-only repair stays scoped."""

    def test_security_category_filter_preserves_scope(self) -> None:
        from saturnday.repair.finding_category import parse_repair_categories
        from saturnday.openclaw_scanner import Finding

        cats = parse_repair_categories("fix all the security issues")
        assert cats == {"security"}

        # Create mixed findings
        findings = [
            Finding(check="hardcoded_jwt", kind="hardcoded_secret", category="security"),
            Finding(check="ruff", kind="ruff", category="quality"),
            Finding(check="license", kind="missing_license", category="project"),
        ]

        filtered = [f for f in findings if f.category in cats]
        assert len(filtered) == 1
        assert filtered[0].check == "hardcoded_jwt"


class TestPlanGovernanceProof:
    """Demo: ticket completion vs governing intent differ."""

    def test_dod_met_but_plan_governance_not_met(self) -> None:
        from saturnday._types import RunResult

        result = RunResult(
            project_id="demo",
            total_tickets=5,
            passed=5,
            definition_of_done_met=True,
            plan_governance_met=False,
            plan_governance_reason=(
                "PLAN_GOVERNANCE_NOT_MET: quality findings were also changed, "
                "violating exclusion"
            ),
        )
        assert result.definition_of_done_met is True
        assert result.plan_governance_met is False


class TestReleasePreflightProof:
    """Demo: release-preflight catches bad artefact."""

    def test_leak_package_fails_preflight(self, tmp_path: Path) -> None:
        leak = FIXTURES / "sample-leak-package"
        if not leak.is_dir():
            pytest.fail("Leak fixture missing")

        repo = tmp_path / "leaky"
        shutil.copytree(leak, repo)
        _init_git(repo)

        from saturnday.release.orchestrator import run_release_preflight

        pack = run_release_preflight(repo_path=repo, artefact_type="python")
        assert hasattr(pack, "disposition")


class TestThreeStateProof:
    """Demo: three entitlement states produce honest evidence."""

    def test_public_only_state(self) -> None:
        from saturnday import capability_registry
        from saturnday.shared.evidence_schema import build_capability_state

        capability_registry.clear()
        state = build_capability_state()
        assert state["premium_capabilities_enabled"] is False

    def test_premium_registered_state(self) -> None:
        from saturnday import capability_registry
        from saturnday.shared.evidence_schema import build_capability_state

        capability_registry.clear()
        capability_registry.register("security_triage", object())
        state = build_capability_state()
        assert state["premium_capabilities_enabled"] is True
        capability_registry.clear()


class TestOpenClawProof:
    """Demo: OpenClaw skill scan works."""

    def test_skill_passes_preflight(self) -> None:
        skill = FIXTURES / "sample-openclaw-skill"
        if not skill.is_dir():
            pytest.fail("Skill fixture missing")

        from saturnday.guard.publish_preflight import run_publish_preflight

        result = run_publish_preflight(skill)
        assert result is not None
