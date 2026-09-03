from saturnday.policy import evaluate_policy


def _base_policy_input():
    return {
        "version": 1,
        "scope": {
            "allowed_globs": [],
            "forbidden_globs": [],
            "budgets": {
                "max_files_changed": None,
                "max_total_diff_lines": None,
                "max_per_file_diff_lines": None,
            },
        },
        "changes": {"files_changed": [], "diff_total_lines": 0, "diff_lines_by_file": {}},
        "verification": {"status": "RAN", "exit_code": 0, "verify_cmd": "verify"},
        "waivers": [],
    }


def test_verification_skipped_denies():
    policy_input = _base_policy_input()
    policy_input["verification"] = {"status": "SKIPPED", "exit_code": None, "verify_cmd": "verify"}
    decision = evaluate_policy(policy_input)
    assert decision["allow"] is False
    assert "verification_skipped" in decision["deny_reasons"]


def test_verification_failed_denies():
    policy_input = _base_policy_input()
    policy_input["verification"]["exit_code"] = 2
    decision = evaluate_policy(policy_input)
    assert decision["allow"] is False
    assert "verification_failed" in decision["deny_reasons"]


def test_verification_pass_allows():
    policy_input = _base_policy_input()
    decision = evaluate_policy(policy_input)
    assert decision["allow"] is True
    assert decision["deny_reasons"] == []


def test_forbidden_path_denies():
    policy_input = _base_policy_input()
    policy_input["scope"]["forbidden_globs"] = ["secret/*"]
    policy_input["changes"]["files_changed"] = ["secret/creds.txt"]
    decision = evaluate_policy(policy_input)
    assert decision["allow"] is False
    assert "forbidden_path:secret/creds.txt" in decision["deny_reasons"]


def test_outside_allowlist_denies():
    policy_input = _base_policy_input()
    policy_input["scope"]["allowed_globs"] = ["src/**"]
    policy_input["changes"]["files_changed"] = ["docs/readme.md"]
    decision = evaluate_policy(policy_input)
    assert decision["allow"] is False
    assert "outside_allowlist:docs/readme.md" in decision["deny_reasons"]


def test_allow_root_match_for_double_star():
    policy_input = _base_policy_input()
    policy_input["scope"]["allowed_globs"] = ["**/*.md"]
    policy_input["changes"]["files_changed"] = ["README.md"]
    decision = evaluate_policy(policy_input)
    assert decision["allow"] is True
    assert decision["deny_reasons"] == []


def test_allow_mid_double_star_match():
    policy_input = _base_policy_input()
    policy_input["scope"]["allowed_globs"] = ["tests/**/*.py"]
    policy_input["changes"]["files_changed"] = ["tests/conftest.py"]
    decision = evaluate_policy(policy_input)
    assert decision["allow"] is True
    assert decision["deny_reasons"] == []


def test_allow_directory_scope_with_trailing_slash():
    policy_input = _base_policy_input()
    policy_input["scope"]["allowed_globs"] = ["app/"]
    policy_input["changes"]["files_changed"] = ["app/main.py"]
    decision = evaluate_policy(policy_input)
    assert decision["allow"] is True
    assert decision["deny_reasons"] == []


def test_max_files_changed_budget_denies():
    policy_input = _base_policy_input()
    policy_input["scope"]["budgets"]["max_files_changed"] = 1
    policy_input["changes"]["files_changed"] = ["a.txt", "b.txt"]
    decision = evaluate_policy(policy_input)
    assert decision["allow"] is False
    assert "max_files_changed_exceeded" in decision["deny_reasons"]


def test_max_total_diff_lines_budget_denies():
    policy_input = _base_policy_input()
    policy_input["scope"]["budgets"]["max_total_diff_lines"] = 1
    policy_input["changes"]["diff_total_lines"] = 2
    decision = evaluate_policy(policy_input)
    assert decision["allow"] is False
    assert "max_total_diff_lines_exceeded" in decision["deny_reasons"]


def test_max_per_file_diff_lines_budget_denies():
    policy_input = _base_policy_input()
    policy_input["scope"]["budgets"]["max_per_file_diff_lines"] = 1
    policy_input["changes"]["files_changed"] = ["a.txt"]
    policy_input["changes"]["diff_lines_by_file"] = {"a.txt": 2}
    decision = evaluate_policy(policy_input)
    assert decision["allow"] is False
    assert "max_per_file_diff_lines_exceeded:a.txt" in decision["deny_reasons"]


def test_waiver_removes_matching_denies():
    policy_input = _base_policy_input()
    policy_input["scope"]["forbidden_globs"] = ["secret/*"]
    policy_input["changes"]["files_changed"] = ["secret/creds.txt"]
    policy_input["waivers"] = [{"id": "forbidden_path", "reason": "approved", "expires_utc": None}]
    decision = evaluate_policy(policy_input)
    assert decision["allow"] is True
    assert decision["deny_reasons"] == []
    assert decision["waivers_used"] == ["forbidden_path"]


def test_knowledge_trust_denied_blocks_policy():
    policy_input = _base_policy_input()
    policy_input["knowledge"] = {
        "status": "PASS",
        "trust": {"allow": False, "deny_reasons": ["trust_tier_denied:pack:community"]},
    }
    decision = evaluate_policy(policy_input)
    assert decision["allow"] is False
    assert "knowledge_trust_denied" in decision["deny_reasons"]


def test_knowledge_invalid_blocks_policy():
    policy_input = _base_policy_input()
    policy_input["knowledge"] = {"status": "ERROR", "trust": {"allow": False}}
    decision = evaluate_policy(policy_input)
    assert decision["allow"] is False
    assert "knowledge_pack_invalid" in decision["deny_reasons"]


def test_cloud_preflight_failed_blocks_policy():
    policy_input = _base_policy_input()
    policy_input["cloud_preflight"] = {"status": "FAIL"}
    decision = evaluate_policy(policy_input)
    assert decision["allow"] is False
    assert "cloud_preflight_failed" in decision["deny_reasons"]


def test_shell_sandbox_failed_blocks_policy():
    policy_input = _base_policy_input()
    policy_input["shell_sandbox"] = {"status": "FAIL", "enforced": True}
    decision = evaluate_policy(policy_input)
    assert decision["allow"] is False
    assert "shell_sandbox_failed" in decision["deny_reasons"]
