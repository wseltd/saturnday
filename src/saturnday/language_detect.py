"""Detect programming language of files by extension."""

from __future__ import annotations

from pathlib import Path

# Extension → language mapping
_EXT_MAP: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".sh": "shell",
    ".bash": "shell",
    ".zsh": "shell",
    ".md": "markdown",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".env": "env",
    ".tf": "terraform",
    ".hcl": "terraform",
    ".css": "css",
    ".html": "html",
    ".htm": "html",
}

# Languages considered TypeScript/JavaScript for TS check routing
TS_JS_EXTENSIONS: frozenset[str] = frozenset({
    ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
})

PYTHON_EXTENSIONS: frozenset[str] = frozenset({".py", ".pyi"})

SHELL_EXTENSIONS: frozenset[str] = frozenset({".sh", ".bash", ".zsh"})


_NAME_MAP: dict[str, str] = {
    ".env": "env",
    "Makefile": "make",
    "Dockerfile": "docker",
}


def detect_language(path: str | Path) -> str | None:
    """Return the language string for a file path, or None if unknown."""
    p = Path(path)
    # Check full filename for dotfiles and special names
    name = p.name
    if name in _NAME_MAP:
        return _NAME_MAP[name]
    ext = p.suffix.lower()
    return _EXT_MAP.get(ext)


def is_ts_js(path: str | Path) -> bool:
    """Return True if the file is a TypeScript or JavaScript file."""
    return Path(path).suffix.lower() in TS_JS_EXTENSIONS


def is_python(path: str | Path) -> bool:
    """Return True if the file is a Python file."""
    return Path(path).suffix.lower() in PYTHON_EXTENSIONS


def is_shell(path: str | Path) -> bool:
    """Return True if the file is a shell script."""
    return Path(path).suffix.lower() in SHELL_EXTENSIONS


def classify_files(paths: list[str]) -> dict[str, list[str]]:
    """Group file paths by detected language.

    Returns a dict mapping language names to lists of file paths.
    Files with unknown extensions are grouped under 'unknown'.
    """
    result: dict[str, list[str]] = {}
    for p in paths:
        lang = detect_language(p) or "unknown"
        result.setdefault(lang, []).append(p)
    return result
