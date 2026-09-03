"""Closes Nick #12 — Terraform coverage was thin (3 rules) given §7.5
advertised coverage.  This batch adds 7 rules that match the kinds of
issues operators expect a security check to catch on real RDS / S3 /
ALB / ECS / IAM Terraform.

The 10 rules now in ``_check_terraform``:

  1. tf_hardcoded_credential   (pre-existing)
  2. tf_public_access_cidr     (pre-existing)
  3. tf_sensitive_in_state     (pre-existing)
  4. tf_s3_unencrypted         NEW — file-scope check
  5. tf_s3_public_acl          NEW
  6. tf_rds_unencrypted        NEW
  7. tf_rds_public             NEW
  8. tf_iam_wildcard_resource  NEW
  9. tf_iam_wildcard_action    NEW
 10. tf_ecs_privileged         NEW

Every new rule has one positive fixture (should fire) and one
negative fixture (should not).  Pre-existing rules are also pinned
so the rewrite did not regress them.
"""
from __future__ import annotations

_AWS_KEY = "AKIA" + "0" * 16


from pathlib import Path

import pytest

from saturnday.review import _check_terraform


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _run(tmp_path: Path, filename: str, content: str) -> dict:
    target = tmp_path / filename
    target.write_text(content, encoding="utf-8")
    return _check_terraform(tmp_path, [filename])


def _kinds(result: dict) -> set[str]:
    return {f.get("kind") for f in result.get("findings", [])}


# ---------------------------------------------------------------------------
# Pre-existing rules — pin so the refactor did not regress them
# ---------------------------------------------------------------------------


class TestPreExistingRulesStillFire:
    def test_hardcoded_credential(self, tmp_path: Path) -> None:
        r = _run(tmp_path, "creds.tf",
                 f'access_key = "{_AWS_KEY}"\n')
        assert "tf_hardcoded_credential" in _kinds(r)

    def test_public_access_cidr(self, tmp_path: Path) -> None:
        r = _run(tmp_path, "sg.tf",
                 'resource "aws_security_group_rule" "x" {\n'
                 '  cidr_blocks = ["0.0.0.0/0"]\n'
                 '}\n')
        assert "tf_public_access_cidr" in _kinds(r)

    def test_sensitive_in_state(self, tmp_path: Path) -> None:
        r = _run(tmp_path, "var.tf",
                 'variable "secret" {\n'
                 '  default = "abcdefghijklmnopqrstuvwxyz0123456789"\n'
                 '}\n')
        assert "tf_sensitive_in_state" in _kinds(r)


# ---------------------------------------------------------------------------
# New rules — positive
# ---------------------------------------------------------------------------


class TestNewRulesPositive:
    def test_s3_unencrypted_fires_when_no_sse_in_file(
        self, tmp_path: Path,
    ) -> None:
        """File declares an S3 bucket but never mentions
        server_side_encryption_configuration → flag."""
        r = _run(tmp_path, "s3.tf",
                 'resource "aws_s3_bucket" "data" {\n'
                 '  bucket = "my-data-bucket"\n'
                 '}\n')
        assert "tf_s3_unencrypted" in _kinds(r)

    def test_s3_public_acl_public_read(self, tmp_path: Path) -> None:
        r = _run(tmp_path, "s3.tf",
                 'resource "aws_s3_bucket_acl" "x" {\n'
                 '  acl = "public-read"\n'
                 '}\n')
        assert "tf_s3_public_acl" in _kinds(r)

    def test_s3_public_acl_authenticated_read(self, tmp_path: Path) -> None:
        r = _run(tmp_path, "s3.tf",
                 '  acl = "authenticated-read"\n')
        assert "tf_s3_public_acl" in _kinds(r)

    def test_rds_unencrypted(self, tmp_path: Path) -> None:
        r = _run(tmp_path, "rds.tf",
                 'resource "aws_db_instance" "main" {\n'
                 '  storage_encrypted = false\n'
                 '}\n')
        assert "tf_rds_unencrypted" in _kinds(r)

    def test_rds_public(self, tmp_path: Path) -> None:
        r = _run(tmp_path, "rds.tf",
                 'resource "aws_db_instance" "main" {\n'
                 '  publicly_accessible = true\n'
                 '}\n')
        assert "tf_rds_public" in _kinds(r)

    def test_iam_wildcard_resource(self, tmp_path: Path) -> None:
        r = _run(tmp_path, "iam.tf",
                 'data "aws_iam_policy_document" "x" {\n'
                 '  statement {\n'
                 '    "Resource": "*"\n'
                 '  }\n'
                 '}\n')
        assert "tf_iam_wildcard_resource" in _kinds(r)

    def test_iam_wildcard_action(self, tmp_path: Path) -> None:
        r = _run(tmp_path, "iam.tf",
                 '"Action": "*"\n')
        assert "tf_iam_wildcard_action" in _kinds(r)

    def test_iam_wildcard_in_array_form(self, tmp_path: Path) -> None:
        r = _run(tmp_path, "iam.tf",
                 '"Action": [ "*" ]\n')
        assert "tf_iam_wildcard_action" in _kinds(r)

    def test_ecs_privileged(self, tmp_path: Path) -> None:
        r = _run(tmp_path, "ecs.tf",
                 'resource "aws_ecs_task_definition" "x" {\n'
                 '  privileged = true\n'
                 '}\n')
        assert "tf_ecs_privileged" in _kinds(r)


# ---------------------------------------------------------------------------
# New rules — negative (the rule must NOT fire on benign code)
# ---------------------------------------------------------------------------


class TestNewRulesNegative:
    def test_s3_unencrypted_silent_when_sse_present(
        self, tmp_path: Path,
    ) -> None:
        r = _run(tmp_path, "s3.tf",
                 'resource "aws_s3_bucket" "data" {\n'
                 '  bucket = "my-data-bucket"\n'
                 '}\n'
                 'resource "aws_s3_bucket_server_side_encryption_configuration" "x" {\n'
                 '  rule { apply_server_side_encryption_by_default { sse_algorithm = "AES256" } }\n'
                 '}\n')
        assert "tf_s3_unencrypted" not in _kinds(r)

    def test_s3_public_acl_silent_on_private(self, tmp_path: Path) -> None:
        r = _run(tmp_path, "s3.tf",
                 '  acl = "private"\n')
        assert "tf_s3_public_acl" not in _kinds(r)

    def test_rds_unencrypted_silent_on_true(self, tmp_path: Path) -> None:
        r = _run(tmp_path, "rds.tf",
                 'storage_encrypted = true\n')
        assert "tf_rds_unencrypted" not in _kinds(r)

    def test_rds_public_silent_on_false(self, tmp_path: Path) -> None:
        r = _run(tmp_path, "rds.tf",
                 'publicly_accessible = false\n')
        assert "tf_rds_public" not in _kinds(r)

    def test_iam_wildcard_silent_on_specific_action(
        self, tmp_path: Path,
    ) -> None:
        r = _run(tmp_path, "iam.tf",
                 '"Action": "s3:GetObject"\n')
        assert "tf_iam_wildcard_action" not in _kinds(r)
        assert "tf_iam_wildcard_resource" not in _kinds(r)

    def test_ecs_privileged_silent_on_false(self, tmp_path: Path) -> None:
        r = _run(tmp_path, "ecs.tf",
                 'privileged = false\n')
        assert "tf_ecs_privileged" not in _kinds(r)

    def test_comment_lines_dont_trip_rules(self, tmp_path: Path) -> None:
        """Comments must not be scanned (line.strip().startswith('#')
        guard).  Pin so a future refactor doesn't drop the guard."""
        r = _run(tmp_path, "x.tf",
                 '# privileged = true\n'
                 '# acl = "public-read"\n'
                 '# storage_encrypted = false\n')
        kinds = _kinds(r)
        for k in ("tf_ecs_privileged", "tf_s3_public_acl",
                  "tf_rds_unencrypted"):
            assert k not in kinds


# ---------------------------------------------------------------------------
# End-to-end shape — combined real-world fixture
# ---------------------------------------------------------------------------


class TestRealishFixture:
    def test_combined_terraform_module_fires_expected_rules(
        self, tmp_path: Path,
    ) -> None:
        """A small but realistic-shaped TF module exercising several
        rules at once.  Closes Nick's "zero findings on RDS/ALB/ECS"
        complaint by demonstrating the check now produces several
        findings on a typical insecure module."""
        r = _run(tmp_path, "main.tf",
                 'resource "aws_db_instance" "main" {\n'
                 '  engine              = "postgres"\n'
                 '  storage_encrypted   = false\n'
                 '  publicly_accessible = true\n'
                 '}\n'
                 '\n'
                 'resource "aws_s3_bucket" "data" {\n'
                 '  bucket = "data-bucket"\n'
                 '}\n'
                 'resource "aws_s3_bucket_acl" "data" {\n'
                 '  bucket = aws_s3_bucket.data.id\n'
                 '  acl    = "public-read"\n'
                 '}\n'
                 '\n'
                 'resource "aws_ecs_task_definition" "task" {\n'
                 '  privileged = true\n'
                 '}\n'
                 '\n'
                 'data "aws_iam_policy_document" "wide" {\n'
                 '  statement {\n'
                 '    "Action":   "*"\n'
                 '    "Resource": "*"\n'
                 '  }\n'
                 '}\n')
        kinds = _kinds(r)
        for expected in (
            "tf_rds_unencrypted",
            "tf_rds_public",
            "tf_s3_unencrypted",
            "tf_s3_public_acl",
            "tf_ecs_privileged",
            "tf_iam_wildcard_resource",
            "tf_iam_wildcard_action",
        ):
            assert expected in kinds, (
                f"expected {expected} in findings; got {kinds}"
            )


# ---------------------------------------------------------------------------
# Empty-input + non-tf-file negative pin
# ---------------------------------------------------------------------------


class TestNoOpInputs:
    def test_no_tf_files_returns_pass(self, tmp_path: Path) -> None:
        (tmp_path / "main.py").write_text("x = 1\n", encoding="utf-8")
        r = _check_terraform(tmp_path, ["main.py"])
        assert r["status"] == "PASS"
        assert r["findings"] == []

    def test_clean_terraform_file_passes(self, tmp_path: Path) -> None:
        r = _run(tmp_path, "ok.tf",
                 'resource "aws_db_instance" "main" {\n'
                 '  storage_encrypted   = true\n'
                 '  publicly_accessible = false\n'
                 '}\n'
                 'resource "aws_s3_bucket" "data" {\n'
                 '  bucket = "x"\n'
                 '}\n'
                 'resource "aws_s3_bucket_server_side_encryption_configuration" "x" {\n'
                 '  rule {}\n'
                 '}\n')
        assert r["status"] == "PASS"
        assert r["findings"] == []
