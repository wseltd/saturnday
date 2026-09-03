import fnmatch
from pathlib import Path

ALLOWED_NEW_FILE_EXTS = {".py", ".toml", ".md", ".txt", ".json", ".jsonl", ".yml", ".yaml"}


def is_allowed_new_file(path: str) -> bool:
    ext = Path(path).suffix.lower()
    return ext in ALLOWED_NEW_FILE_EXTS


def matches_path_patterns(path: str, patterns: list[str]) -> bool:
    normalized = _normalize_match_path(path)
    if not normalized:
        return False
    for pattern in patterns:
        if not isinstance(pattern, str):
            continue
        candidate = pattern.strip().replace("\\", "/")
        if not candidate:
            continue
        if fnmatch.fnmatchcase(normalized, candidate):
            return True
        if candidate.endswith("/") and normalized.startswith(candidate):
            return True
        if candidate.startswith("**/"):
            if fnmatch.fnmatchcase(normalized, candidate[3:]):
                return True
        if "/**/" in candidate:
            alt = candidate.replace("/**/", "/")
            if fnmatch.fnmatchcase(normalized, alt):
                return True
    return False


def _normalize_match_path(path: str) -> str:
    normalized = path.strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized
