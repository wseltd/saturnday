"""Tests for language_detect module."""

from saturnday.language_detect import (
    classify_files,
    detect_language,
    is_python,
    is_shell,
    is_ts_js,
)


class TestDetectLanguage:
    def test_python_files(self):
        assert detect_language("app.py") == "python"
        assert detect_language("stubs.pyi") == "python"

    def test_typescript_files(self):
        assert detect_language("index.ts") == "typescript"
        assert detect_language("Component.tsx") == "typescript"

    def test_javascript_files(self):
        assert detect_language("main.js") == "javascript"
        assert detect_language("lib.mjs") == "javascript"
        assert detect_language("util.cjs") == "javascript"
        assert detect_language("App.jsx") == "javascript"

    def test_shell_files(self):
        assert detect_language("run.sh") == "shell"
        assert detect_language("setup.bash") == "shell"
        assert detect_language("init.zsh") == "shell"

    def test_markdown(self):
        assert detect_language("README.md") == "markdown"

    def test_config_files(self):
        assert detect_language("config.json") == "json"
        assert detect_language("config.yaml") == "yaml"
        assert detect_language("config.yml") == "yaml"
        assert detect_language("pyproject.toml") == "toml"

    def test_env_file(self):
        assert detect_language(".env") == "env"

    def test_unknown_extension(self):
        assert detect_language("binary.exe") is None
        assert detect_language("data.csv") is None

    def test_special_names(self):
        assert detect_language("Makefile") == "make"
        assert detect_language("Dockerfile") == "docker"

    def test_case_insensitive(self):
        assert detect_language("APP.PY") == "python"
        assert detect_language("INDEX.TS") == "typescript"

    def test_path_with_directories(self):
        assert detect_language("src/app/main.py") == "python"
        assert detect_language("lib/utils/helper.ts") == "typescript"


class TestIsHelpers:
    def test_is_ts_js(self):
        assert is_ts_js("index.ts") is True
        assert is_ts_js("app.tsx") is True
        assert is_ts_js("main.js") is True
        assert is_ts_js("lib.mjs") is True
        assert is_ts_js("util.cjs") is True
        assert is_ts_js("app.py") is False
        assert is_ts_js("run.sh") is False

    def test_is_python(self):
        assert is_python("app.py") is True
        assert is_python("stubs.pyi") is True
        assert is_python("index.ts") is False

    def test_is_shell(self):
        assert is_shell("run.sh") is True
        assert is_shell("setup.bash") is True
        assert is_shell("app.py") is False


class TestClassifyFiles:
    def test_mixed_files(self):
        files = ["app.py", "index.ts", "main.js", "run.sh", "README.md", "Makefile"]
        result = classify_files(files)
        assert result["python"] == ["app.py"]
        assert result["typescript"] == ["index.ts"]
        assert result["javascript"] == ["main.js"]
        assert result["shell"] == ["run.sh"]
        assert result["markdown"] == ["README.md"]
        assert result["make"] == ["Makefile"]

    def test_empty_list(self):
        assert classify_files([]) == {}

    def test_all_same_language(self):
        files = ["a.py", "b.py", "c.py"]
        result = classify_files(files)
        assert result == {"python": ["a.py", "b.py", "c.py"]}
