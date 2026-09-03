"""Tests for saturnday.run.failure_classifier."""

from saturnday.run.failure_classifier import classify_failure


class TestClassifyFailure:
    def test_timeout(self) -> None:
        result = classify_failure(error="Request timed out after 300s")
        assert result.governance_outcome == "TIMEOUT_RETRYABLE"
        assert result.failure_category == "timeout_or_complexity_defect"

    def test_no_files_changed(self) -> None:
        result = classify_failure(error="No changes", changed_files=())
        assert result.governance_outcome == "PLAN_DEFECT_REQUIRES_EDIT"
        assert result.failure_category == "plan_defect"

    def test_governance_fail(self) -> None:
        result = classify_failure(
            error="Governance blocked",
            governance_disposition="FAIL",
            changed_files=("src/foo.py",),
        )
        assert result.governance_outcome == "HARD_FAIL_BLOCKING"
        assert result.failure_category == "policy_defect"

    def test_post_check_findings(self) -> None:
        result = classify_failure(
            error="Post-checks failed",
            changed_files=("src/foo.py",),
            post_check_findings=[{"message": "silent exception"}],
        )
        assert result.governance_outcome == "SOFT_FAIL_RETRYABLE"
        assert result.failure_category == "coder_non_compliance_defect"

    def test_unsupported_framework(self) -> None:
        result = classify_failure(
            error="Module not found: some_framework",
            changed_files=("src/foo.py",),
        )
        assert result.governance_outcome == "UNSUPPORTED_FRAMEWORK_FAIL_CLOSED"
        assert result.failure_category == "unsupported_environment_defect"

    def test_default_classification(self) -> None:
        result = classify_failure(
            error="Something weird happened",
            changed_files=("src/foo.py",),
        )
        assert result.governance_outcome == "PLAN_DEFECT_REQUIRES_EDIT"
        assert result.failure_category == "plan_defect"
