from saturnday.scope_rules import is_allowed_new_file, matches_path_patterns


def test_is_allowed_new_file_accepts_jsonl():
    assert is_allowed_new_file("eval_set.jsonl") is True


def test_matches_path_patterns_accepts_directory_scope():
    assert matches_path_patterns("app/main.py", ["app/"]) is True


def test_matches_path_patterns_rejects_sibling_path_for_directory_scope():
    assert matches_path_patterns("main.py", ["app/"]) is False
