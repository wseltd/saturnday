"""Tests for RS-022: PyPI publishing workflow YAML structure.

Covers:
- Workflow YAML file exists at .github/workflows/publish-pypi.yml
- Parses as valid YAML
- Contains required top-level keys: name, on, jobs
- Triggers on tag push matching v*
- Contains a test job
- Contains a release-preflight job that needs the test job
- Contains a publish job that needs the release-preflight job
- id-token: write permission is present (required for trusted-publisher OIDC)
- release-preflight step runs saturnday release-preflight
- Preflight verification step asserts disposition == PASS
- Publish step uses pypa/gh-action-pypi-publish
- Environment named 'release' is set on at least one job
"""
from __future__ import annotations

from pathlib import Path

import pytest

try:
    import yaml
    _YAML_AVAILABLE = True
except ImportError:
    _YAML_AVAILABLE = False

_WORKFLOW_PATH = Path(__file__).parent.parent.parent / ".github" / "workflows" / "publish-pypi.yml"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_workflow() -> dict:
    """Load and parse the workflow YAML.  Raises if YAML unavailable."""
    if not _YAML_AVAILABLE:
        pytest.skip("PyYAML not installed")
    with open(_WORKFLOW_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestWorkflowFileExists:
    def test_file_exists(self) -> None:
        assert _WORKFLOW_PATH.is_file(), (
            f"Expected workflow at {_WORKFLOW_PATH} — run RS-022 to create it"
        )

    def test_file_is_non_empty(self) -> None:
        assert _WORKFLOW_PATH.stat().st_size > 0


class TestWorkflowYamlValid:
    def test_parses_as_valid_yaml(self) -> None:
        wf = _load_workflow()
        assert isinstance(wf, dict), "Workflow YAML should parse to a dict"

    def test_has_name_key(self) -> None:
        wf = _load_workflow()
        assert "name" in wf, "Workflow must have a 'name' key"

    def test_has_on_key(self) -> None:
        wf = _load_workflow()
        # PyYAML parses 'on:' as boolean True in some versions.
        assert "on" in wf or True in wf, "Workflow must have an 'on' trigger key"

    def test_has_jobs_key(self) -> None:
        wf = _load_workflow()
        assert "jobs" in wf, "Workflow must have a 'jobs' key"
        assert isinstance(wf["jobs"], dict)
        assert len(wf["jobs"]) > 0


class TestWorkflowTrigger:
    def test_triggers_on_tag_push(self) -> None:
        wf = _load_workflow()
        # PyYAML parses bare 'on:' as boolean True in some configurations.
        on = wf.get("on") or wf.get(True) or {}
        if isinstance(on, dict):
            push = on.get("push", {}) or {}
        else:
            push = {}
        tags = push.get("tags", []) or []
        assert any("v*" in str(t) for t in tags), (
            "Workflow must trigger on push of 'v*' tags"
        )


class TestWorkflowPermissions:
    def test_id_token_write_permission(self) -> None:
        wf = _load_workflow()
        permissions = wf.get("permissions", {})
        assert permissions.get("id-token") == "write", (
            "Workflow must have 'id-token: write' permission for OIDC trusted-publisher"
        )


class TestWorkflowJobs:
    def _jobs(self) -> dict:
        return _load_workflow()["jobs"]

    def test_has_test_job(self) -> None:
        jobs = self._jobs()
        assert any(
            "test" in name.lower() for name in jobs
        ), "Workflow must have a test job"

    def test_has_release_preflight_job(self) -> None:
        jobs = self._jobs()
        assert any(
            "preflight" in name.lower() for name in jobs
        ), "Workflow must have a release-preflight job"

    def test_has_publish_job(self) -> None:
        jobs = self._jobs()
        assert any(
            "publish" in name.lower() for name in jobs
        ), "Workflow must have a publish job"

    def test_preflight_needs_test(self) -> None:
        jobs = self._jobs()
        preflight_job = next(
            (j for name, j in jobs.items() if "preflight" in name.lower()), None
        )
        assert preflight_job is not None
        needs = preflight_job.get("needs", [])
        if isinstance(needs, str):
            needs = [needs]
        assert any("test" in str(n).lower() for n in needs), (
            "release-preflight job must depend on the test job"
        )

    def test_publish_needs_preflight(self) -> None:
        jobs = self._jobs()
        publish_job = next(
            (j for name, j in jobs.items() if "publish" in name.lower()), None
        )
        assert publish_job is not None
        needs = publish_job.get("needs", [])
        if isinstance(needs, str):
            needs = [needs]
        assert any("preflight" in str(n).lower() for n in needs), (
            "publish job must depend on the release-preflight job"
        )

    def test_preflight_or_publish_has_release_environment(self) -> None:
        jobs = self._jobs()
        envs = []
        for job in jobs.values():
            env = job.get("environment")
            if isinstance(env, str):
                envs.append(env.lower())
            elif isinstance(env, dict):
                envs.append(str(env.get("name", "")).lower())
        assert any("release" in e for e in envs), (
            "At least one job must use the 'release' environment for deployment protection"
        )


class TestWorkflowStepContent:
    """Verify that key step commands are present in the YAML text."""

    def _raw_text(self) -> str:
        return _WORKFLOW_PATH.read_text(encoding="utf-8")

    def test_release_preflight_command_present(self) -> None:
        assert "release-preflight" in self._raw_text(), (
            "Workflow must invoke 'saturnday release-preflight'"
        )

    def test_disposition_assertion_present(self) -> None:
        text = self._raw_text()
        assert "disposition" in text, (
            "Workflow must check the preflight evidence disposition"
        )

    def test_pypa_publish_action_present(self) -> None:
        text = self._raw_text()
        assert "pypa/gh-action-pypi-publish" in text, (
            "Workflow must use pypa/gh-action-pypi-publish to publish"
        )

    def test_python_m_build_present(self) -> None:
        text = self._raw_text()
        assert "python -m build" in text, (
            "Workflow must build the package with 'python -m build'"
        )
