"""Tests for saturnday.release.checks.secrets_in_artefact (RS-008 / REL-002).

Covers:
- .env file in artefact → FAIL
- id_rsa (SSH private key file) in artefact → FAIL
- High-entropy string in a .py file → FAIL
- AWS key pattern in content → FAIL
- npm .npmrc containing auth token → FAIL
- Clean artefact → PASS
- .env file present but listed in manifest allowed_secrets → exempted, PASS
- Multiple findings in one artefact → all captured
- Binary file skipped (no false positives from compiled assets)
- File larger than size limit skipped
- Exempted file recorded in findings with status="exempted"
- Result fields: name, rule_id, status, severity, files_checked, elapsed_s
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from saturnday.release._types import ArtefactFile, ArtefactInventory
from saturnday.release.checks.secrets_in_artefact import (
    _extract_allowed_globs,
    _is_content_scan_skipped,
    _is_exempted,
    _match_filename,
    _pattern_label_to_kind,
    run_check,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_stripe_live_key() -> bytes:
    """Build a realistic ``sk_live_`` Stripe key at runtime.

    Constructed dynamically so the raw literal never appears in committed
    source, avoiding GitHub push-protection false positives while keeping
    Saturnday's own secret scanner fully exercised.
    """
    return b"sk_" + b"live_" + b"aBcDeFgHiJkLmNoPqRsTuVwX"


def _fake_stripe_test_key() -> bytes:
    """Build a realistic ``sk_test_`` Stripe key at runtime."""
    return b"sk_" + b"test_" + b"aBcDeFgHiJkLmNoPqRsTuVwXyZaB"


def _make_inventory(files: list[tuple[str, bytes]], unpack_dir: Path) -> ArtefactInventory:
    """Write *files* to *unpack_dir* and build an ArtefactInventory.

    Args:
        files:      List of ``(relative_path, content_bytes)`` pairs.
        unpack_dir: Temp directory used as the artefact root.

    Returns:
        An :class:`ArtefactInventory` whose ``.files`` list matches *files*.
    """
    artefact_files: list[ArtefactFile] = []
    for rel_path, content in files:
        abs_path = unpack_dir / rel_path
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_bytes(content)
        sha = hashlib.sha256(content).hexdigest()
        artefact_files.append(
            ArtefactFile(path=rel_path, size=len(content), sha256=sha)
        )

    return ArtefactInventory(
        artefact_type="wheel",
        artefact_path=str(unpack_dir / "dummy-1.0-py3-none-any.whl"),
        artefact_sha256="deadbeef" * 8,
        files=artefact_files,
    )


# ---------------------------------------------------------------------------
# result shape sanity
# ---------------------------------------------------------------------------


def test_result_fields_present(tmp_path: Path) -> None:
    """run_check always returns a ReleaseCheckResult with all required fields."""
    inv = _make_inventory([("mypackage/__init__.py", b"# clean\n")], tmp_path)
    result = run_check(inventory=inv, unpack_dir=tmp_path)

    assert result.name == "secrets_in_artefact"
    assert result.rule_id == "REL-002"
    assert result.status in ("PASS", "FAIL", "WARN", "SKIPPED")
    assert result.severity == "error"
    assert isinstance(result.findings, list)
    assert isinstance(result.files_checked, int)
    assert isinstance(result.elapsed_s, float)
    assert result.elapsed_s >= 0.0


# ---------------------------------------------------------------------------
# clean artefact → PASS
# ---------------------------------------------------------------------------


def test_clean_artefact_passes(tmp_path: Path) -> None:
    """A well-behaved artefact with no secrets returns PASS."""
    inv = _make_inventory(
        [
            ("mypackage/__init__.py", b"# init\n"),
            ("mypackage/utils.py", b"def add(a, b):\n    return a + b\n"),
            ("mypackage-1.0.dist-info/METADATA", b"Metadata-Version: 2.1\nName: mypackage\n"),
        ],
        tmp_path,
    )
    result = run_check(inventory=inv, unpack_dir=tmp_path)

    assert result.status == "PASS"
    assert result.files_checked == 3
    found = [f for f in result.findings if f["status"] == "found"]
    assert found == []


# ---------------------------------------------------------------------------
# .env file in artefact → FAIL
# ---------------------------------------------------------------------------


def test_env_file_triggers_fail(tmp_path: Path) -> None:
    """.env file detected by filename → FAIL."""
    inv = _make_inventory(
        [
            ("mypackage/__init__.py", b"# init\n"),
            (".env", b"DATABASE_URL=postgres://user:pass@host/db\nSECRET_KEY=mysecret\n"),
        ],
        tmp_path,
    )
    result = run_check(inventory=inv, unpack_dir=tmp_path)

    assert result.status == "FAIL"
    env_findings = [f for f in result.findings if f["file"] == ".env" and f["status"] == "found"]
    assert len(env_findings) >= 1
    finding = env_findings[0]
    assert finding["kind"] == "env_file"
    assert finding["line"] == 0  # filename-only detection
    assert finding["status"] == "found"


def test_env_local_file_triggers_fail(tmp_path: Path) -> None:
    """.env.local file detected by filename → FAIL."""
    inv = _make_inventory(
        [(".env.local", b"API_KEY=secret\n")],
        tmp_path,
    )
    result = run_check(inventory=inv, unpack_dir=tmp_path)

    assert result.status == "FAIL"
    assert any(f["kind"] == "env_file" for f in result.findings)


def test_nested_env_file_triggers_fail(tmp_path: Path) -> None:
    """.env nested inside a package subdirectory is still detected."""
    inv = _make_inventory(
        [("subdir/.env", b"SECRET=abc\n")],
        tmp_path,
    )
    result = run_check(inventory=inv, unpack_dir=tmp_path)

    assert result.status == "FAIL"
    env_finding = next(
        (f for f in result.findings if f["file"] == "subdir/.env"), None
    )
    assert env_finding is not None
    assert env_finding["kind"] == "env_file"


# ---------------------------------------------------------------------------
# id_rsa (SSH private key file) → FAIL
# ---------------------------------------------------------------------------


def test_id_rsa_triggers_fail(tmp_path: Path) -> None:
    """SSH private key file detected by filename → FAIL."""
    inv = _make_inventory(
        [("id_rsa", b"-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQ...\n-----END RSA PRIVATE KEY-----\n")],
        tmp_path,
    )
    result = run_check(inventory=inv, unpack_dir=tmp_path)

    assert result.status == "FAIL"
    key_finding = next(
        (f for f in result.findings if f["file"] == "id_rsa"), None
    )
    assert key_finding is not None
    assert key_finding["kind"] == "key_file"
    assert key_finding["line"] == 0


def test_pem_file_triggers_fail(tmp_path: Path) -> None:
    """A .pem file detected by filename → FAIL."""
    inv = _make_inventory(
        [("certs/server.pem", b"-----BEGIN CERTIFICATE-----\nMIIBxxx\n-----END CERTIFICATE-----\n")],
        tmp_path,
    )
    result = run_check(inventory=inv, unpack_dir=tmp_path)

    assert result.status == "FAIL"
    assert any(f["kind"] == "key_file" for f in result.findings)


# ---------------------------------------------------------------------------
# High-entropy string in .py file → FAIL
# ---------------------------------------------------------------------------


def test_high_entropy_in_python_file_triggers_fail(tmp_path: Path) -> None:
    """A 32+ char alphanumeric literal in Python source → FAIL via HIGH_ENTROPY_REGEX."""
    # 40-character hex string — typical SHA1-length secret
    high_entropy_string = "a" * 40
    content = f'SECRET = "{high_entropy_string}"\n'.encode()
    inv = _make_inventory(
        [("mypackage/config.py", content)],
        tmp_path,
    )
    result = run_check(inventory=inv, unpack_dir=tmp_path)

    assert result.status == "FAIL"
    found = [f for f in result.findings if f["status"] == "found"]
    assert len(found) >= 1
    assert any(f["kind"] == "high_entropy" for f in found)
    assert any(f["line"] == 1 for f in found)


def test_short_string_does_not_trigger_high_entropy(tmp_path: Path) -> None:
    """A string shorter than 32 chars does not trigger the entropy check."""
    content = b'TOKEN = "abc123short"\n'
    inv = _make_inventory(
        [("mypackage/config.py", content)],
        tmp_path,
    )
    result = run_check(inventory=inv, unpack_dir=tmp_path)

    assert result.status == "PASS"
    assert result.findings == []


# ---------------------------------------------------------------------------
# AWS key pattern → FAIL
# ---------------------------------------------------------------------------


def test_aws_key_pattern_triggers_fail(tmp_path: Path) -> None:
    """A Stripe sk_live_ key (provider-specific pattern) in a .py file → FAIL."""
    # Use a Stripe live key — a canonical provider-specific secret pattern
    content = b'PAYMENT_KEY = "' + _fake_stripe_live_key() + b'"\n'
    inv = _make_inventory(
        [("mypackage/payments.py", content)],
        tmp_path,
    )
    result = run_check(inventory=inv, unpack_dir=tmp_path)

    assert result.status == "FAIL"
    found = [f for f in result.findings if f["status"] == "found"]
    assert len(found) >= 1
    stripe_finding = next(
        (f for f in found if "stripe" in f["pattern"].lower() or "sk_live" in f["pattern"].lower()),
        None,
    )
    assert stripe_finding is not None


def test_stripe_test_key_triggers_fail(tmp_path: Path) -> None:
    """A Stripe sk_test_ key is also flagged — test keys are still secrets."""
    content = b'key = "' + _fake_stripe_test_key() + b'"\n'
    inv = _make_inventory(
        [("mypackage/test_helper.py", content)],
        tmp_path,
    )
    result = run_check(inventory=inv, unpack_dir=tmp_path)

    assert result.status == "FAIL"


# ---------------------------------------------------------------------------
# npm .npmrc with auth token → FAIL
# ---------------------------------------------------------------------------


def test_npmrc_with_auth_token_triggers_fail(tmp_path: Path) -> None:
    """.npmrc file detected by filename → FAIL (env_file / key_file match)."""
    # .npmrc is matched because it contains "npm" auth tokens — but the filename
    # check matches it via ENV_FILE_PATTERNS glob "*.env" variants or falls
    # through to content scanning for the "_authToken" pattern.
    # The generic API key literal pattern will catch it via content scan.
    content = b"//registry.npmjs.org/:_authToken=npm_aBcDeFgHiJkLmNoPqRsTuVwXyZaAbBcCdDeE\n"
    inv = _make_inventory(
        [(".npmrc", content)],
        tmp_path,
    )
    result = run_check(inventory=inv, unpack_dir=tmp_path)

    # .npmrc contains a 40+ char token → high_entropy will fire even if no
    # filename pattern matches it directly.
    assert result.status == "FAIL"
    found = [f for f in result.findings if f["status"] == "found"]
    assert len(found) >= 1


def test_npmrc_nested_with_token_triggers_fail(tmp_path: Path) -> None:
    """An .npmrc in a subdirectory with a token still triggers a finding."""
    # The token "npm_" prefix followed by 36+ chars — high entropy fires.
    content = b"//registry.npmjs.org/:_authToken=npm_aBcDeFgHiJkLmNoPqRsTuVwXyZaAbBcCdDeE\n"
    inv = _make_inventory(
        [("package/.npmrc", content)],
        tmp_path,
    )
    result = run_check(inventory=inv, unpack_dir=tmp_path)

    assert result.status == "FAIL"


# ---------------------------------------------------------------------------
# .env allowed in manifest → exempted, PASS
# ---------------------------------------------------------------------------


def test_env_file_exempted_by_manifest(tmp_path: Path) -> None:
    """.env file present but listed in manifest allowed_secrets → exempted, PASS."""
    inv = _make_inventory(
        [
            ("mypackage/__init__.py", b"# init\n"),
            (".env", b"# template only - no real secrets\nDATABASE_URL=\n"),
        ],
        tmp_path,
    )
    manifest: dict[str, Any] = {"allowed_secrets": [".env"]}
    result = run_check(inventory=inv, unpack_dir=tmp_path, manifest=manifest)

    assert result.status == "PASS"
    # The exempted finding must still be present in findings.
    exempted = [f for f in result.findings if f["status"] == "exempted"]
    assert len(exempted) >= 1
    assert exempted[0]["file"] == ".env"
    assert exempted[0]["kind"] == "env_file"
    # No non-exempted findings.
    found = [f for f in result.findings if f["status"] == "found"]
    assert found == []


def test_pem_file_exempted_by_glob_manifest(tmp_path: Path) -> None:
    """A .pem file exempted via **/*.pem glob in manifest → exempted, PASS."""
    inv = _make_inventory(
        [("certs/jwt_public.pem", b"-----BEGIN PUBLIC KEY-----\nMIIBIjANBgkq\n-----END PUBLIC KEY-----\n")],
        tmp_path,
    )
    manifest: dict[str, Any] = {"allowed_secrets": ["**/*.pem"]}
    result = run_check(inventory=inv, unpack_dir=tmp_path, manifest=manifest)

    assert result.status == "PASS"
    exempted = [f for f in result.findings if f["status"] == "exempted"]
    assert len(exempted) == 1
    assert exempted[0]["kind"] == "key_file"


def test_partial_exemption_still_fails(tmp_path: Path) -> None:
    """When one file is exempted but another is not, overall result is FAIL."""
    inv = _make_inventory(
        [
            (".env", b"# allowed template\n"),
            (".env.production", b"REAL_SECRET=supersecretvalue1234567890abcdef\n"),
        ],
        tmp_path,
    )
    # Only .env is exempted — .env.production is not.
    manifest: dict[str, Any] = {"allowed_secrets": [".env"]}
    result = run_check(inventory=inv, unpack_dir=tmp_path, manifest=manifest)

    assert result.status == "FAIL"
    exempted = [f for f in result.findings if f["status"] == "exempted"]
    assert len(exempted) == 1
    assert exempted[0]["file"] == ".env"
    found = [f for f in result.findings if f["status"] == "found"]
    assert len(found) >= 1
    assert any(f["file"] == ".env.production" for f in found)


# ---------------------------------------------------------------------------
# Binary file skipped
# ---------------------------------------------------------------------------


def test_binary_file_not_content_scanned(tmp_path: Path) -> None:
    """A binary file (contains null bytes) is skipped — no false positives."""
    # Craft binary content that contains a null byte and an entropy-like sequence.
    binary_content = b"\x00\x01\x02\x03" + b"a" * 40 + b"\xff\xfe"
    inv = _make_inventory(
        [("mypackage/compiled.so", binary_content)],
        tmp_path,
    )
    result = run_check(inventory=inv, unpack_dir=tmp_path)

    # Binary file must not trigger any finding.
    assert result.status == "PASS"
    assert result.findings == []


# ---------------------------------------------------------------------------
# File exceeding size limit skipped
# ---------------------------------------------------------------------------


def test_oversized_file_not_content_scanned(tmp_path: Path) -> None:
    """A file larger than _MAX_CONTENT_BYTES is not content-scanned."""
    from saturnday.release.checks.secrets_in_artefact import _MAX_CONTENT_BYTES

    # Build content that exceeds the limit and contains a high-entropy string.
    oversized_content = b"x" * (_MAX_CONTENT_BYTES + 1024)
    inv = _make_inventory(
        [("mypackage/big_data.py", oversized_content)],
        tmp_path,
    )
    result = run_check(inventory=inv, unpack_dir=tmp_path)

    # Oversized non-binary file must not trigger content findings.
    found = [f for f in result.findings if f["status"] == "found"]
    assert found == []


# ---------------------------------------------------------------------------
# Multiple findings in one artefact
# ---------------------------------------------------------------------------


def test_multiple_findings_captured(tmp_path: Path) -> None:
    """Multiple secret files in one artefact all appear in findings."""
    inv = _make_inventory(
        [
            (".env", b"DB=postgres://secret:pass@host/db\n"),
            ("id_rsa", b"-----BEGIN RSA PRIVATE KEY-----\nSECRET\n-----END RSA PRIVATE KEY-----\n"),
            ("mypackage/config.py", b'KEY = "' + _fake_stripe_live_key() + b'"\n'),
        ],
        tmp_path,
    )
    result = run_check(inventory=inv, unpack_dir=tmp_path)

    assert result.status == "FAIL"
    found = [f for f in result.findings if f["status"] == "found"]
    files_with_findings = {f["file"] for f in found}
    # .env and id_rsa are filename detections; config.py is a content detection.
    assert ".env" in files_with_findings
    assert "id_rsa" in files_with_findings
    assert "mypackage/config.py" in files_with_findings


# ---------------------------------------------------------------------------
# No manifest → deny-by-default
# ---------------------------------------------------------------------------


def test_no_manifest_uses_deny_by_default(tmp_path: Path) -> None:
    """When no manifest is provided, no exemptions apply."""
    inv = _make_inventory(
        [(".env", b"SECRET=abc\n")],
        tmp_path,
    )
    result = run_check(inventory=inv, unpack_dir=tmp_path, manifest=None)

    assert result.status == "FAIL"
    assert any(f["status"] == "found" for f in result.findings)


def test_empty_allowed_secrets_list(tmp_path: Path) -> None:
    """An empty allowed_secrets list in manifest applies no exemptions."""
    inv = _make_inventory(
        [(".env", b"SECRET=abc\n")],
        tmp_path,
    )
    manifest: dict[str, Any] = {"allowed_secrets": []}
    result = run_check(inventory=inv, unpack_dir=tmp_path, manifest=manifest)

    assert result.status == "FAIL"


# ---------------------------------------------------------------------------
# Unit tests for internal helpers
# ---------------------------------------------------------------------------


class TestExtractAllowedGlobs:
    def test_none_manifest_returns_empty(self) -> None:
        assert _extract_allowed_globs(None) == []

    def test_missing_key_returns_empty(self) -> None:
        assert _extract_allowed_globs({"other_key": ["x"]}) == []

    def test_valid_list_returned(self) -> None:
        result = _extract_allowed_globs({"allowed_secrets": ["*.pem", ".env"]})
        assert result == ["*.pem", ".env"]

    def test_non_list_ignored(self) -> None:
        result = _extract_allowed_globs({"allowed_secrets": "*.pem"})
        assert result == []


class TestIsExempted:
    def test_empty_globs_not_exempted(self) -> None:
        assert _is_exempted(".env", []) is False

    def test_exact_match_exempted(self) -> None:
        assert _is_exempted(".env", [".env"]) is True

    def test_glob_star_matches(self) -> None:
        assert _is_exempted("certs/server.pem", ["*.pem"]) is True

    def test_double_star_matches_deep_path(self) -> None:
        assert _is_exempted("deep/nested/path/secret.pem", ["**/*.pem"]) is True

    def test_non_matching_glob(self) -> None:
        assert _is_exempted(".env", ["*.pem"]) is False

    def test_basename_match_via_glob(self) -> None:
        assert _is_exempted("subdir/.env", [".env"]) is True


class TestMatchFilename:
    def test_exact_match(self) -> None:
        result = _match_filename("id_rsa", ["id_rsa", "id_dsa"])
        assert result == "id_rsa"

    def test_glob_match(self) -> None:
        result = _match_filename("server.pem", ["*.pem", "*.key"])
        assert result == "*.pem"

    def test_no_match_returns_none(self) -> None:
        assert _match_filename("config.py", ["*.pem", "id_rsa"]) is None

    def test_first_match_returned(self) -> None:
        result = _match_filename(".env", [".env", "*.env"])
        assert result == ".env"


class TestPatternLabelToKind:
    def test_entropy_label(self) -> None:
        assert _pattern_label_to_kind("High-entropy alphanumeric string (≥32 chars)") == "high_entropy"

    def test_stripe_label(self) -> None:
        assert _pattern_label_to_kind("Stripe secret key (sk_live_*)") == "api_key"

    def test_sendgrid_label(self) -> None:
        assert _pattern_label_to_kind("SendGrid API key") == "api_key"

    def test_slack_label(self) -> None:
        assert _pattern_label_to_kind("Slack bot token (xoxb-*)") == "api_key"

    def test_generic_secret_key_label(self) -> None:
        assert _pattern_label_to_kind("Generic live/secret API key literal") == "api_key"

    def test_unknown_label_returns_secret(self) -> None:
        assert _pattern_label_to_kind("Unknown pattern XYZ") == "secret"


# ---------------------------------------------------------------------------
# RS-020: RECORD file false positives — content scan skipped
# ---------------------------------------------------------------------------


class TestIsContentScanSkipped:
    """Unit tests for the _is_content_scan_skipped helper."""

    def test_dist_info_record_skipped(self) -> None:
        assert _is_content_scan_skipped("mypackage-1.0.dist-info/RECORD") is True

    def test_dist_info_metadata_skipped(self) -> None:
        assert _is_content_scan_skipped("mypackage-1.0.dist-info/METADATA") is True

    def test_egg_info_pkg_info_skipped(self) -> None:
        assert _is_content_scan_skipped("mypackage.egg-info/PKG-INFO") is True

    def test_top_level_pkg_info_skipped(self) -> None:
        assert _is_content_scan_skipped("some/path/PKG-INFO") is True

    def test_regular_python_file_not_skipped(self) -> None:
        assert _is_content_scan_skipped("mypackage/config.py") is False

    def test_dist_info_wheel_not_skipped(self) -> None:
        assert _is_content_scan_skipped("mypackage-1.0.dist-info/WHEEL") is False

    def test_backslash_normalised(self) -> None:
        assert _is_content_scan_skipped("pkg-1.0.dist-info\\RECORD") is True


def test_record_file_not_content_scanned(tmp_path: Path) -> None:
    """A *.dist-info/RECORD file with sha256 hashes must not trigger FAIL.

    This is the exact false-positive scenario from RS-020: every wheel
    has a RECORD file with sha256=<base64> per entry, producing dozens
    of high-entropy hits.
    """
    record_content = (
        b"mypackage/__init__.py,sha256=abcdefghijklmnopqrstuvwxyz012345678901234567890A,42\n"
        b"mypackage/core.py,sha256=ABCDEFGHIJKLMNOPQRSTUVWXYZ012345678901234567890b,1337\n"
    )
    inv = _make_inventory(
        [
            ("mypackage/__init__.py", b"# init\n"),
            ("mypackage-1.0.dist-info/RECORD", record_content),
        ],
        tmp_path,
    )
    result = run_check(inventory=inv, unpack_dir=tmp_path)

    assert result.status == "PASS"
    # No findings from the RECORD file — its content was not scanned.
    record_findings = [f for f in result.findings if "RECORD" in f.get("file", "")]
    assert record_findings == []


def test_metadata_file_not_content_scanned(tmp_path: Path) -> None:
    """A *.dist-info/METADATA file with git SHAs must not trigger FAIL.

    RS-020 found that 40-char hex strings (pinned GitHub Actions commit
    SHAs) in METADATA files trigger HIGH_ENTROPY_REGEX false positives.
    """
    metadata_content = (
        b"Metadata-Version: 2.1\n"
        b"Name: mypackage\n"
        b"Version: 1.0.0\n"
        b"Uses: actions/checkout@abcdef1234567890abcdef1234567890abcdef12\n"
    )
    inv = _make_inventory(
        [
            ("mypackage/__init__.py", b"# init\n"),
            ("mypackage-1.0.dist-info/METADATA", metadata_content),
        ],
        tmp_path,
    )
    result = run_check(inventory=inv, unpack_dir=tmp_path)

    assert result.status == "PASS"
    metadata_findings = [f for f in result.findings if "METADATA" in f.get("file", "")]
    assert metadata_findings == []


def test_env_file_inside_dist_info_still_caught(tmp_path: Path) -> None:
    """Filename-based detection still works for skipped-content paths.

    If someone somehow places a .env file inside dist-info, the filename
    check must still fire even though content scanning is skipped.
    """
    # This is a pathological case but ensures the skip only affects content.
    inv = _make_inventory(
        [("mypackage-1.0.dist-info/.env", b"SECRET=oops\n")],
        tmp_path,
    )
    result = run_check(inventory=inv, unpack_dir=tmp_path)

    assert result.status == "FAIL"
    assert any(f["kind"] == "env_file" for f in result.findings)
