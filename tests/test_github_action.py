"""Tests for GitHub Action and CLI scan command."""

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from saturnday.cli import build_parser, main


class TestActionYml:
    """Verify the action.yml exists and has required structure."""

    def test_action_yml_exists(self):
        action = Path(__file__).resolve().parents[1] / ".github" / "actions" / "saturnday-check" / "action.yml"
        assert action.exists(), f"action.yml not found at {action}"

    def test_action_yml_structure(self):
        action = Path(__file__).resolve().parents[1] / ".github" / "actions" / "saturnday-check" / "action.yml"
        text = action.read_text()
        # Basic structural checks
        assert "name:" in text
        assert "description:" in text
        assert "inputs:" in text
        assert "runs:" in text
        assert "using: 'composite'" in text
        # Must reference check_diff.py
        assert "check_diff.py" in text
        # Must reference verified artifact names
        assert "final-disposition.json" in text
        assert "summary.md" in text

    def test_action_mentions_permissions(self):
        action = Path(__file__).resolve().parents[1] / ".github" / "actions" / "saturnday-check" / "action.yml"
        text = action.read_text()
        # Required permissions documented in comments
        assert "contents: read" in text
        assert "pull-requests: write" in text

    def test_comment_posting_does_not_break_check(self):
        """The action must use || true on comment posting."""
        action = Path(__file__).resolve().parents[1] / ".github" / "actions" / "saturnday-check" / "action.yml"
        text = action.read_text()
        assert "|| true" in text  # comment failure must not break the action


class TestCliScanParser:
    """Test CLI parser accepts scan subcommand."""

    def test_scan_subcommand_exists(self):
        parser = build_parser()
        args = parser.parse_args(["scan", "--openclaw", "/tmp/corpus", "--output", "/tmp/out"])
        assert args.command == "scan"
        assert args.openclaw == "/tmp/corpus"
        assert args.output == "/tmp/out"

    def test_scan_skill_mode(self):
        parser = build_parser()
        args = parser.parse_args(["scan", "--skill", "/tmp/skill", "--output", "/tmp/out"])
        assert args.command == "scan"
        assert args.skill == "/tmp/skill"

    def test_scan_mutually_exclusive(self):
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["scan", "--openclaw", "/tmp", "--skill", "/tmp/s", "--output", "/tmp/o"])

    def test_scan_output_required(self):
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["scan", "--openclaw", "/tmp"])

    def test_scan_optional_flags(self):
        parser = build_parser()
        args = parser.parse_args([
            "scan", "--openclaw", "/tmp", "--output", "/tmp/out",
            "--limit", "100", "--top-n", "10",
            "--strict",
        ])
        assert args.limit == 100
        assert args.top_n == 10
        assert args.strict is True

    def test_scan_format_default_is_both(self):
        parser = build_parser()
        args = parser.parse_args([
            "scan", "--openclaw", "/tmp", "--output", "/tmp/out",
        ])
        assert args.format == "both"

    def test_scan_format_choices(self):
        parser = build_parser()
        for fmt in ("json", "markdown", "both"):
            args = parser.parse_args([
                "scan", "--openclaw", "/tmp", "--output", "/tmp/out",
                "--format", fmt,
            ])
            assert args.format == fmt


class TestPresetFile:
    """Verify the OpenClaw policy preset exists and is valid."""

    def test_preset_exists(self):
        preset = Path(__file__).resolve().parents[1] / "presets" / "openclaw-skills.yaml"
        assert preset.exists()

    def test_preset_loadable(self):
        preset = Path(__file__).resolve().parents[1] / "presets" / "openclaw-skills.yaml"
        try:
            import yaml
            data = yaml.safe_load(preset.read_text())
            assert "checks" in data
            checks = data["checks"]
            # Verify family names
            assert checks["secrets"] == "error"
            assert checks["hallucinated_imports"] == "warning"
            assert checks["fake_tests"] == "error"
            assert checks["prompt_injection"] == "error"
            assert checks["typosquat"] == "off"
            assert checks["placeholders"] == "warning"
            assert checks["syntax"] == "warning"
        except ImportError:
            pytest.skip("pyyaml not installed")
