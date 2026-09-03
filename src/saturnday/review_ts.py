"""TypeScript/JavaScript review checks for governance scanning.

Each check function returns a dict matching the standard tool result format:
    {name, status, findings, exit_code, raw_output, error}

Check names use the _ts suffix to avoid collision with Python checks.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import npm_registry
from .language_detect import is_ts_js

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Max file size for static analysis (1 MB)
MAX_FILE_SIZE_BYTES = 1_048_576

# Node.js built-in modules (Node 20 LTS)
NODE_BUILTINS: frozenset[str] = frozenset({
    "assert", "async_hooks", "buffer", "child_process", "cluster",
    "console", "constants", "crypto", "dgram", "diagnostics_channel",
    "dns", "domain", "events", "fs", "http", "http2", "https",
    "inspector", "module", "net", "os", "path", "perf_hooks",
    "process", "punycode", "querystring", "readline", "repl",
    "stream", "string_decoder", "sys", "test", "timers", "tls",
    "trace_events", "tty", "url", "util", "v8", "vm", "wasi",
    "worker_threads", "zlib",
    # node: prefixed versions are handled by stripping the prefix
})

# Top npm packages — used ONLY as the comparison set for typosquat edit
# distance scoring.  NOT used as an allowlist for hallucinated_imports (which
# now uses registry verification).  ~800 entries, organized by ecosystem.
_TOP_NPM_PACKAGES: frozenset[str] = frozenset({
    # -- Web frameworks --
    "express", "fastify", "koa", "hapi", "hono", "polka", "micro", "restify",
    "sails", "feathers", "loopback", "adonis", "strapi", "keystone",
    "nest", "nestjs", "@nestjs/core", "@nestjs/common", "@nestjs/platform-express",
    # -- React ecosystem --
    "react", "react-dom", "next", "react-router", "react-router-dom",
    "@tanstack/react-query", "swr", "zustand", "jotai", "recoil", "redux",
    "react-hook-form", "formik", "react-select", "@tanstack/react-table",
    "react-beautiful-dnd", "@dnd-kit/core", "framer-motion", "react-spring",
    "@headlessui/react", "@radix-ui/react-dialog", "@radix-ui/react-popover",
    "@radix-ui/react-dropdown-menu", "@radix-ui/react-tooltip",
    "react-icons", "lucide-react", "@heroicons/react",
    "recharts", "nivo", "victory", "@react-three/fiber", "@react-three/drei",
    "react-native", "expo", "react-test-renderer",
    "@next/font", "@next/image", "@next/env",
    "next-auth", "@auth/core", "@clerk/nextjs", "@clerk/clerk-sdk-node",
    # -- Vue ecosystem --
    "vue", "vue-router", "vuex", "pinia", "nuxt", "@nuxt/kit", "@nuxt/content",
    "@vue/test-utils", "vueuse", "quasar", "vuetify", "primevue",
    # -- Svelte/Solid/Other frameworks --
    "svelte", "@sveltejs/kit", "@sveltejs/adapter-auto", "solid-js",
    "preact", "lit", "alpine", "htmx", "qwik",
    "gatsby", "remix", "astro", "@astrojs/node", "@astrojs/react",
    # -- Build tools --
    "vite", "webpack", "rollup", "esbuild", "parcel", "turbopack",
    "tsup", "unbuild", "microbundle", "@swc/core", "swc-loader",
    "babel", "@babel/core", "@babel/preset-env", "@babel/preset-typescript",
    "postcss", "autoprefixer", "sass", "less", "cssnano", "lightningcss",
    "tailwindcss", "windicss", "unocss", "daisyui",
    "@vitejs/plugin-react", "@vitejs/plugin-vue",
    # -- TypeScript/JS core --
    "typescript", "tslib", "core-js", "regenerator-runtime",
    "whatwg-fetch", "cross-fetch", "isomorphic-fetch", "abort-controller",
    "web-streams-polyfill",
    # -- Testing --
    "jest", "vitest", "mocha", "chai", "sinon", "supertest", "nock", "msw",
    "@testing-library/react", "@testing-library/jest-dom",
    "@testing-library/user-event", "@testing-library/dom",
    "@playwright/test", "playwright", "puppeteer", "puppeteer-core",
    "cypress", "selenium-webdriver", "testcafe",
    "@jest/globals", "tape", "ava", "c8", "nyc", "istanbul",
    "storybook", "@storybook/react", "chromatic",
    "expect", "assert", "power-assert",
    # -- Blockchain / Web3 --
    "ethers", "viem", "web3", "wagmi", "@rainbow-me/rainbowkit",
    "@solana/web3.js", "@solana/spl-token", "@solana/wallet-adapter-react",
    "hardhat", "@openzeppelin/contracts", "@openzeppelin/test-helpers",
    "moralis", "thirdweb", "@thirdweb-dev/sdk",
    "bs58", "bip39", "bitcoinjs-lib", "solc", "truffle", "ganache",
    "@coral-xyz/anchor", "@ethersproject/providers", "@ethersproject/abi",
    # -- Database / ORM --
    "mongoose", "sequelize", "prisma", "@prisma/client",
    "typeorm", "drizzle-orm", "drizzle-kit", "knex", "objection", "bookshelf",
    "pg", "mysql2", "sqlite3", "better-sqlite3", "sqlite",
    "mongodb", "redis", "ioredis",
    "@planetscale/database", "@libsql/client",
    "kysely", "kysely-d1", "mikro-orm",
    "typeorm", "mongoose-paginate-v2",
    # -- API / HTTP --
    "axios", "node-fetch", "got", "ky", "superagent", "undici",
    "form-data", "formdata-node",
    "graphql", "graphql-request", "@apollo/client", "@apollo/server",
    "apollo-server", "urql",
    "@trpc/server", "@trpc/client", "@trpc/react-query",
    "request", "needle", "phin",
    # -- Auth / Security --
    "passport", "passport-local", "passport-jwt", "passport-google-oauth20",
    "bcrypt", "bcryptjs", "argon2",
    "jsonwebtoken", "jose",
    "helmet", "cors", "csurf", "express-rate-limit",
    "oauth", "openid-client", "oidc-provider",
    # -- Cloud / Services --
    "aws-sdk", "@aws-sdk/client-s3", "@aws-sdk/client-dynamodb",
    "@aws-sdk/client-ses", "@aws-sdk/client-lambda",
    "firebase", "firebase-admin", "@google-cloud/storage",
    "@google-cloud/compute", "@google-cloud/pubsub", "googleapis",
    "@supabase/supabase-js", "supabase",
    "@vercel/blob", "@vercel/kv", "@vercel/analytics",
    "stripe", "@sendgrid/mail", "nodemailer", "twilio",
    "@notionhq/client", "@slack/web-api", "@slack/bolt",
    "octokit", "@octokit/rest", "@octokit/core",
    "@cloudflare/workers-types", "wrangler",
    "cos-nodejs-sdk-v5",
    "paypal", "braintree",
    # -- UI component libraries --
    "antd", "@mui/material", "@mui/icons-material",
    "@chakra-ui/react", "@mantine/core", "@mantine/hooks",
    "bootstrap", "react-bootstrap",
    "styled-components", "@emotion/react", "@emotion/styled", "emotion",
    "clsx", "classnames", "cva", "class-variance-authority",
    "shadcn", "headlessui",
    # -- Content / Markdown --
    "marked", "markdown-it", "remark", "rehype", "unified",
    "remark-gfm", "remark-math", "rehype-prism-plus", "rehype-katex",
    "@mdx-js/react", "@mdx-js/loader", "mdx",
    "gray-matter", "front-matter",
    "highlight.js", "prism", "prismjs", "shiki",
    "turndown", "sanitize-html", "dompurify", "xss",
    # -- Image / Media --
    "sharp", "jimp", "canvas", "remotion",
    "@remotion/cli", "@remotion/renderer", "@remotion/google-fonts",
    "pdfkit", "pdf-lib", "jspdf",
    "ffmpeg", "fluent-ffmpeg", "exif-parser", "file-type", "image-size",
    # -- Data / Validation --
    "zod", "yup", "joi", "ajv", "class-validator", "class-transformer",
    "io-ts", "superstruct", "valibot", "@sinclair/typebox",
    # -- Utility --
    "lodash", "underscore", "ramda",
    "date-fns", "dayjs", "moment", "luxon",
    "uuid", "nanoid", "cuid", "ulid", "ulidx",
    "immer", "rxjs", "mobx",
    "p-limit", "p-queue", "p-retry", "p-map",
    "async", "bluebird", "delay", "ms",
    "bytes", "humanize", "pluralize",
    "camelcase", "change-case", "slugify",
    "deep-equal", "fast-deep-equal", "deepmerge",
    "eventemitter3", "mitt", "tiny-emitter",
    "lru-cache", "keyv", "node-cache",
    "semver", "compare-versions",
    "retry", "async-retry",
    # -- CLI --
    "commander", "yargs", "meow", "inquirer", "prompts",
    "chalk", "ora", "@clack/prompts", "boxen", "listr2",
    "figlet", "cli-table3", "blessed", "ink",
    # -- Config / Env --
    "dotenv", "config", "convict", "cosmiconfig", "envalid",
    "cross-env", "env-var",
    # -- Logging --
    "winston", "pino", "bunyan", "morgan", "debug",
    "consola", "log4js", "signale", "tslog",
    # -- File / IO --
    "fs-extra", "glob", "fast-glob", "globby",
    "chokidar", "rimraf", "mkdirp", "del",
    "archiver", "adm-zip", "tar", "jszip", "unzipper",
    "csv-parse", "papaparse",
    "xml2js", "fast-xml-parser",
    "yaml", "json5", "toml", "ini",
    "execa", "shelljs",
    "node-cron", "cron", "bottleneck", "rate-limiter-flexible",
    # -- Dev tools --
    "eslint", "prettier", "husky", "lint-staged",
    "commitlint", "@commitlint/cli", "@commitlint/config-conventional",
    "semantic-release", "changesets",
    "turbo", "lerna", "nx", "concurrently", "npm-run-all",
    # -- Networking / Protocol --
    "socket.io", "socket.io-client", "ws",
    "mqtt", "amqplib", "bull", "bullmq",
    "kafkajs", "@grpc/grpc-js", "protobufjs",
    # -- Visualization --
    "d3", "chart.js", "three", "pixi.js", "phaser",
    "echarts", "vega", "vega-lite", "plotly.js",
    "apexcharts", "recharts", "nivo", "victory", "mermaid",
    # -- Desktop / Mobile --
    "electron", "@electron/remote", "tauri",
    "react-native", "expo", "capacitor", "@capacitor/core",
    # -- i18n --
    "i18next", "react-i18next", "vue-i18n",
    "intl-messageformat", "@formatjs/intl",
    # -- AI / ML --
    "openai", "anthropic", "@anthropic-ai/sdk",
    "langchain", "@langchain/core", "@langchain/openai",
    "llamaindex", "@tensorflow/tfjs", "onnxruntime-node",
    "@huggingface/inference", "cohere-ai", "replicate",
    "@modelcontextprotocol/sdk", "ai", "@vercel/ai",
    # -- HTTP middleware --
    "body-parser", "cookie-parser", "compression", "serve-static",
    "http-proxy", "http-proxy-middleware",
    "multer", "busboy", "formidable",
    "path-to-regexp", "qs", "query-string",
    "mime", "mime-types", "content-type",
    # -- Misc well-known --
    "open", "cheerio", "jsdom", "iconv-lite", "encoding",
    "handlebars", "ejs", "pug", "nunjucks", "mustache",
    "jsonfile", "conf", "lowdb",
    "puppeteer-extra", "puppeteer-extra-plugin-stealth",
    "yjs", "imap", "curl", "iztro",
    "openclaw",
})

# Prompt-related keywords for prompt_injection heuristic.
# Tightened to LLM-specific compound terms only. Generic words like "system",
# "role", "messages", "assistant" cause massive FP in template literal detection.
# Calibration (2026-03-12): original regex produced 8% precision. These compounds
# + prompt assignment detection should push precision above 50%.
_PROMPT_KEYWORDS = re.compile(
    r"\b(system_prompt|systemPrompt|system_message|systemMessage|"
    r"user_prompt|userPrompt|user_input|userInput|user_message|userMessage|"
    r"assistant_message|assistantMessage|"
    r"chat_completion|chatCompletion|createChatCompletion|"
    r"prompt_template|promptTemplate)\b"
    r"|"
    # Match variable named "prompt" being assigned or used as object key
    r"(?:const|let|var)\s+prompt\s*=",
    re.IGNORECASE,
)

# Lines that are clearly logging/testing, not prompt construction.
# Used to suppress template_injection false positives.
_LOGGING_LINE = re.compile(
    r"^\s*(?:console\.\w+|log(?:ger)?\.\w+|assert(?:\.\w+)?|expect)\s*\(",
)

# LLM API endpoint patterns
_LLM_API_PATTERNS = re.compile(
    r"(openai\.com|anthropic\.com|api\.together\.xyz|"
    r"chat/completions|v1/messages|v1/chat|"
    r"ChatCompletion|createCompletion|createChatCompletion)",
    re.IGNORECASE,
)

# Secret patterns (extends review.py's _scan_secrets patterns for TS/JS)
_SECRET_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("api_key", re.compile(
        r"""(?:api[_-]?key|apikey|api_secret)\s*[:=]\s*["'][A-Za-z0-9_\-/.+=]{16,}["']""",
        re.IGNORECASE,
    )),
    ("bearer_token", re.compile(
        r"""["']Bearer\s+[A-Za-z0-9_\-/.+=]{20,}["']""",
    )),
    ("aws_key", re.compile(
        r"""(?:AKIA|ASIA)[A-Z0-9]{16}""",
    )),
    ("private_key", re.compile(
        r"""-----BEGIN\s+(?:RSA\s+)?PRIVATE\s+KEY-----""",
    )),
    ("github_token", re.compile(
        r"""(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{36,}""",
    )),
    ("generic_secret", re.compile(
        r"""(?:secret|password|passwd|token|auth_token|access_token)\s*[:=]\s*["'][^"']{8,}["']""",
        re.IGNORECASE,
    )),
    ("openai_key", re.compile(
        r"""sk-[A-Za-z0-9]{32,}""",
    )),
    ("anthropic_key", re.compile(
        r"""sk-ant-[A-Za-z0-9_\-]{32,}""",
    )),
    ("slack_token", re.compile(
        r"""xox[bpras]-[A-Za-z0-9\-]{10,}""",
    )),
    ("npm_token", re.compile(
        r"""npm_[A-Za-z0-9]{36}""",
    )),
]

# Placeholder patterns for TS/JS
_PLACEHOLDER_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("todo_comment", re.compile(r"//\s*(?:TODO|FIXME|HACK|XXX)\b", re.IGNORECASE)),
    ("not_implemented", re.compile(
        r"""throw\s+new\s+Error\s*\(\s*["'](?:Not\s+implemented|TODO|FIXME)""",
        re.IGNORECASE,
    )),
    ("console_todo", re.compile(
        r"""console\.\w+\s*\(\s*["'](?:TODO|FIXME|HACK|not\s+implemented)""",
        re.IGNORECASE,
    )),
]

# Import extraction patterns
_REQUIRE_RE = re.compile(
    r"""require\s*\(\s*["']([^"']+)["']\s*\)"""
)
_IMPORT_FROM_RE = re.compile(
    r"""(?:import\s+.*?\s+from\s+["']([^"']+)["']|import\s*\(\s*["']([^"']+)["']\s*\)|import\s+["']([^"']+)["'])"""
)

# Fake test patterns
_EMPTY_TEST_BODY = re.compile(
    r"""(?:test|it)\s*\(\s*["'][^"']*["']\s*,\s*(?:\(\s*\)\s*=>\s*\{\s*\}|function\s*\(\s*\)\s*\{\s*\})""",
    re.MULTILINE,
)
_TAUTOLOGICAL_EXPECT = re.compile(
    r"""expect\s*\(\s*(?:true|1|"[^"]*"|'[^']*')\s*\)\s*\.to(?:Be|Equal|Strictly\w*)\s*\(\s*(?:true|1|"[^"]*"|'[^']*')\s*\)""",
    re.IGNORECASE,
)
_SKIPPED_TEST = re.compile(
    r"""(?:test\.skip|it\.skip|xit|xdescribe|describe\.skip)\s*\(""",
)
_DESCRIBE_NO_TESTS = re.compile(
    r"""describe\s*\(\s*["'][^"']*["']\s*,\s*(?:\(\s*\)\s*=>\s*\{\s*\}|function\s*\(\s*\)\s*\{\s*\})""",
    re.MULTILINE,
)

# Corpus walk exclusion directories
EXCLUDED_DIRS: frozenset[str] = frozenset({
    ".git", "node_modules", ".venv", "venv", "dist", "build",
    "__pycache__", ".cache", ".next", ".nuxt", ".output",
    "vendor", "bower_components",
    ".saturnday", ".saturnday-repair",
})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_result(
    name: str,
    findings: list[dict],
    *,
    error: str | None = None,
    status_override: str | None = None,
) -> dict:
    """Build a standard check result dict."""
    if status_override:
        status = status_override
    else:
        status = "FAIL" if findings else "PASS"
    return {
        "name": name,
        "status": status,
        "findings": findings,
        "exit_code": 1 if status == "FAIL" else 0,
        "raw_output": "",
        "error": error,
    }


# Path segments that indicate a test directory (forward-slash normalised).
_TEST_DIR_SEGMENTS: tuple[str, ...] = (
    "test/",
    "tests/",
    "spec/",
    "e2e/",
    "__tests__/",
    "cypress/",
)

# Vendor/minified JS patterns — these files are third-party or generated
# and should not produce first-party XSS findings.
_VENDOR_DIR_SEGMENTS: tuple[str, ...] = ("vendor/", "vendors/", "dist/vendor/")
_BUNDLE_RE = re.compile(r"chunk-.+\.js$|bundle\.js$", re.IGNORECASE)


def _is_test_file(path: str) -> bool:
    """Return True if *path* lives inside a test-like directory.

    Checks are performed against the forward-slash-normalised path so that
    both POSIX and Windows separators are handled consistently.
    """
    normalised = path.replace("\\", "/")
    return any(segment in normalised for segment in _TEST_DIR_SEGMENTS)


def _is_vendored_or_minified(path: str) -> bool:
    """Return True if *path* is a vendored, minified, or bundled JS file.

    These are third-party or build-tool outputs and are not subject to
    first-party XSS scanning.
    """
    normalised = path.replace("\\", "/")
    if any(segment in normalised for segment in _VENDOR_DIR_SEGMENTS):
        return True
    basename = normalised.rsplit("/", 1)[-1]
    if basename.endswith(".min.js") or basename.endswith(".min.mjs"):
        return True
    if _BUNDLE_RE.search(basename):
        return True
    return False


def _collect_ts_files(
    repo_path: Path,
    changed_files: list[str] | None = None,
) -> list[tuple[str, str]]:
    """Collect TS/JS files as (relative_path, content) pairs.

    If changed_files is provided, only those files are checked.
    Skips files larger than MAX_FILE_SIZE_BYTES.
    Returns list of (rel_path, content) tuples.
    """
    result = []
    files = changed_files if changed_files is not None else _walk_ts_files(repo_path)
    for rel_path in files:
        if not is_ts_js(rel_path):
            continue
        full = repo_path / rel_path
        if not full.exists() or not full.is_file():
            continue
        try:
            size = full.stat().st_size
        except OSError:
            continue
        if size > MAX_FILE_SIZE_BYTES:
            continue
        try:
            content = full.read_text(errors="replace")
        except Exception:
            continue
        result.append((rel_path, content))
    return result


def _walk_ts_files(repo_path: Path) -> list[str]:
    """Walk repo_path for TS/JS files, skipping excluded dirs."""
    result = []
    for child in sorted(repo_path.rglob("*")):
        if not child.is_file():
            continue
        # Check if any parent is excluded
        parts = child.relative_to(repo_path).parts
        if any(p in EXCLUDED_DIRS for p in parts[:-1]):
            continue
        if is_ts_js(child):
            result.append(str(child.relative_to(repo_path)))
    return result


def _extract_package_name(specifier: str) -> str | None:
    """Extract the root package name from an import specifier.

    './foo' → None (relative import)
    '@/lib/utils' → None (path alias)
    '~/utils' → None (path alias)
    'lodash/fp' → 'lodash'
    '@scope/name/sub' → '@scope/name'
    'fs' → 'fs'
    'node:fs' → 'fs'
    'bun:test' → 'test'
    """
    if not specifier:
        return None
    # Relative imports
    if specifier.startswith(".") or specifier.startswith("/"):
        return None
    # Path aliases (not packages)
    if specifier.startswith("@/") or specifier.startswith("~/"):
        return None
    # Strip node: and bun: runtime prefixes
    if specifier.startswith("node:"):
        specifier = specifier[5:]
    elif specifier.startswith("bun:"):
        specifier = specifier[4:]
    # Scoped packages
    if specifier.startswith("@"):
        parts = specifier.split("/")
        if len(parts) >= 2:
            return f"{parts[0]}/{parts[1]}"
        return specifier
    # Regular packages — take first segment
    return specifier.split("/")[0]


def _get_package_json_deps(repo_path: Path) -> frozenset[str]:
    """Read package.json and return all declared dependency names."""
    pkg_json = repo_path / "package.json"
    if not pkg_json.exists():
        return frozenset()
    try:
        data = json.loads(pkg_json.read_text())
    except Exception:
        return frozenset()
    deps: set[str] = set()
    for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        section = data.get(key)
        if isinstance(section, dict):
            deps.update(section.keys())
    return frozenset(deps)


def _get_node_modules_packages(repo_path: Path) -> frozenset[str]:
    """List packages in node_modules if it exists."""
    nm = repo_path / "node_modules"
    if not nm.is_dir():
        return frozenset()
    pkgs: set[str] = set()
    try:
        for child in nm.iterdir():
            name = child.name
            if name.startswith("."):
                continue
            if name.startswith("@") and child.is_dir():
                for sub in child.iterdir():
                    if sub.is_dir() and not sub.name.startswith("."):
                        pkgs.add(f"{name}/{sub.name}")
            elif child.is_dir():
                pkgs.add(name)
    except OSError:
        pass
    return frozenset(pkgs)


def _levenshtein(a: str, b: str) -> int:
    """Compute Levenshtein edit distance."""
    if len(a) < len(b):
        return _levenshtein(b, a)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a):
        curr = [i + 1]
        for j, cb in enumerate(b):
            curr.append(min(prev[j + 1] + 1, curr[j] + 1, prev[j] + (0 if ca == cb else 1)))
        prev = curr
    return prev[-1]


# ---------------------------------------------------------------------------
# Import classification (Stage 1)
# ---------------------------------------------------------------------------

@dataclass
class ClassifiedImport:
    """A classified import specifier with resolution metadata."""
    raw_specifier: str
    file_path: str
    line_number: int
    import_type: str  # relative, builtin, alias, scoped, subpath, external, url
    base_package: str | None  # Resolved root package name (None for relative/alias/url)
    resolved: bool = False  # True if confirmed to exist locally


def _load_tsconfig_paths(repo_path: Path) -> set[str]:
    """Extract path alias prefixes from tsconfig.json compilerOptions.paths.

    Handles JSONC (comments in JSON) by stripping // comments before parsing.
    Returns set of alias prefixes like {'@/', '~/', '#/'}.
    """
    tsconfig = repo_path / "tsconfig.json"
    if not tsconfig.exists():
        return set()
    try:
        text = tsconfig.read_text(errors="replace")
        # Strip single-line comments (JSONC)
        text = re.sub(r"//.*$", "", text, flags=re.MULTILINE)
        # Strip block comments
        text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
        data = json.loads(text)
    except Exception:
        return set()

    paths = data.get("compilerOptions", {}).get("paths", {})
    prefixes: set[str] = set()
    for alias in paths:
        # Convert "alias/*" → "alias/" as prefix; "alias" → "alias"
        clean = alias.replace("/*", "/")
        if not clean.endswith("/"):
            clean += "/"
        prefixes.add(clean)
    return prefixes


def _parse_lockfile_packages(repo_path: Path) -> frozenset[str]:
    """Extract package names from package-lock.json or yarn.lock.

    Returns frozenset of package names found in the lockfile.
    """
    pkgs: set[str] = set()

    # Try package-lock.json first (v2 and v3 format)
    lock = repo_path / "package-lock.json"
    if lock.exists():
        try:
            data = json.loads(lock.read_text(errors="replace"))
            # v2/v3: packages key with "node_modules/..." paths
            packages = data.get("packages", {})
            for key in packages:
                if key.startswith("node_modules/"):
                    pkg_name = key[len("node_modules/"):]
                    # Handle nested: node_modules/@scope/name/node_modules/dep → dep
                    if "/node_modules/" in pkg_name:
                        pkg_name = pkg_name.rsplit("/node_modules/", 1)[-1]
                    if pkg_name:
                        pkgs.add(pkg_name)
            # v1: dependencies key
            deps = data.get("dependencies", {})
            if isinstance(deps, dict):
                pkgs.update(deps.keys())
        except Exception:
            pass
        return frozenset(pkgs)

    # Try yarn.lock
    yarn_lock = repo_path / "yarn.lock"
    if yarn_lock.exists():
        try:
            text = yarn_lock.read_text(errors="replace")
            # Yarn v1 format: "package@version": or package@version:
            # Yarn berry format: "package@npm:version":
            for m in re.finditer(r'^"?(@?[^@\s"]+)@', text, re.MULTILINE):
                name = m.group(1)
                if name and not name.startswith("#"):
                    pkgs.add(name)
        except Exception:
            pass

    return frozenset(pkgs)


def _classify_import(
    specifier: str,
    file_path: str,
    line_number: int,
    *,
    tsconfig_aliases: set[str] | None = None,
) -> ClassifiedImport:
    """Classify a single import specifier.

    Classification types:
    - relative: ./foo, ../bar, /absolute
    - builtin: fs, path, node:crypto, bun:test
    - alias: @/lib/utils, ~/utils, tsconfig path aliases
    - url: https://esm.sh/..., http://...
    - scoped: @scope/name
    - subpath: lodash/fp → base_package=lodash
    - external: bare package name
    """
    if not specifier:
        return ClassifiedImport(specifier, file_path, line_number, "external", None)

    # Relative imports
    if specifier.startswith(("./", "../", "/")):
        return ClassifiedImport(specifier, file_path, line_number, "relative", None)

    # URL imports
    if specifier.startswith(("https:", "http:", "data:")):
        return ClassifiedImport(specifier, file_path, line_number, "url", None)

    # Built-in path aliases @/ and ~/
    if specifier.startswith("@/") or specifier.startswith("~/"):
        return ClassifiedImport(specifier, file_path, line_number, "alias", None)

    # tsconfig.json path aliases
    if tsconfig_aliases:
        for prefix in tsconfig_aliases:
            if specifier.startswith(prefix) or specifier == prefix.rstrip("/"):
                return ClassifiedImport(specifier, file_path, line_number, "alias", None)

    # Node/Bun builtins with prefix
    if specifier.startswith("node:") or specifier.startswith("bun:"):
        stripped = specifier.split(":", 1)[1]
        return ClassifiedImport(specifier, file_path, line_number, "builtin", stripped)

    # Scoped packages
    if specifier.startswith("@"):
        parts = specifier.split("/")
        if len(parts) >= 2:
            base = f"{parts[0]}/{parts[1]}"
        else:
            base = specifier
        # Check if it's a builtin (unlikely but handle)
        if base in NODE_BUILTINS:
            return ClassifiedImport(specifier, file_path, line_number, "builtin", base)
        return ClassifiedImport(specifier, file_path, line_number, "scoped", base)

    # Extract root package name
    root = specifier.split("/")[0]

    # Node builtins
    if root in NODE_BUILTINS:
        return ClassifiedImport(specifier, file_path, line_number, "builtin", root)

    # Subpath import
    if "/" in specifier:
        return ClassifiedImport(specifier, file_path, line_number, "subpath", root)

    # Plain external package
    return ClassifiedImport(specifier, file_path, line_number, "external", root)


def _resolve_locally(
    imp: ClassifiedImport,
    *,
    pkg_json_deps: frozenset[str],
    lockfile_pkgs: frozenset[str],
    node_modules_pkgs: frozenset[str],
) -> ClassifiedImport:
    """Try to resolve an import against local sources.

    Priority: package.json → lockfile → node_modules.
    Returns a new ClassifiedImport with resolved=True if found.
    """
    if imp.base_package is None or imp.resolved:
        return imp

    pkg = imp.base_package
    pkg_lower = pkg.lower()

    # Check package.json deps (case-sensitive — npm names are case-sensitive)
    if pkg in pkg_json_deps:
        return ClassifiedImport(
            imp.raw_specifier, imp.file_path, imp.line_number,
            imp.import_type, imp.base_package, resolved=True,
        )

    # Check lockfile
    if pkg in lockfile_pkgs:
        return ClassifiedImport(
            imp.raw_specifier, imp.file_path, imp.line_number,
            imp.import_type, imp.base_package, resolved=True,
        )

    # Check node_modules (case-insensitive fallback)
    if pkg in node_modules_pkgs:
        return ClassifiedImport(
            imp.raw_specifier, imp.file_path, imp.line_number,
            imp.import_type, imp.base_package, resolved=True,
        )
    # Case-insensitive check for node_modules
    if pkg_lower in {p.lower() for p in node_modules_pkgs}:
        return ClassifiedImport(
            imp.raw_specifier, imp.file_path, imp.line_number,
            imp.import_type, imp.base_package, resolved=True,
        )

    return imp


@dataclass
class TyposquatRisk:
    """Typosquat risk assessment for a package."""
    package: str
    similar_to: str
    distance: int
    score: float  # 0.0-1.0, higher = more suspicious
    undeclared: bool  # Not in package.json
    candidate_downloads: int = -1   # weekly downloads of flagged package
    target_downloads: int = -1      # weekly downloads of similar popular package


# Download thresholds for typosquat gating
_CANDIDATE_MAX_DOWNLOADS = 1_000     # candidate must have fewer than this
_TARGET_MIN_DOWNLOADS = 100_000      # target must have more than this


def _calculate_typosquat_risk(
    pkg: str,
    *,
    pkg_json_deps: frozenset[str],
    candidate_downloads: int = -1,
    target_downloads: int = -1,
) -> TyposquatRisk | None:
    """Score typosquat risk against _TOP_NPM_PACKAGES with download-count gating.

    Returns None if:
    - no similar popular package found
    - candidate has >= 1,000 weekly downloads (legitimate package)
    - target has < 100,000 weekly downloads (not popular enough to be a target)
    - download data unavailable for either package

    ALL conditions must be met to flag:
    - edit distance 1-2 from a popular package (>100K downloads)
    - candidate has <1,000 weekly downloads
    - target has >100,000 weekly downloads
    """
    pkg_norm = pkg.lower().replace("-", "_")
    if len(pkg_norm) < 3:
        return None

    best_match: str | None = None
    best_dist: int = 999

    for known_pkg in _TOP_NPM_PACKAGES:
        known_norm = known_pkg.lower().replace("-", "_")
        if len(known_norm) < 3:
            continue
        dist = _levenshtein(pkg_norm, known_norm)
        if 0 < dist <= 2 and dist < best_dist:
            best_dist = dist
            best_match = known_pkg

    if best_match is None:
        return None

    # Download-count gating: NEVER flag if downloads unavailable
    if candidate_downloads < 0 or target_downloads < 0:
        return None

    # NEVER flag if candidate has >= 1,000 downloads (legitimate package)
    if candidate_downloads >= _CANDIDATE_MAX_DOWNLOADS:
        return None

    # NEVER flag if target has < 100,000 downloads (not popular enough)
    if target_downloads < _TARGET_MIN_DOWNLOADS:
        return None

    # All gates passed — calculate risk score
    score = 0.0
    if best_dist == 1:
        score += 0.6
    elif best_dist == 2:
        score += 0.3

    undeclared = pkg not in pkg_json_deps
    if undeclared:
        score += 0.2

    # Download ratio amplifier: very low downloads near very popular package
    if candidate_downloads < 100 and target_downloads > 1_000_000:
        score += 0.2

    return TyposquatRisk(
        package=pkg,
        similar_to=best_match,
        distance=best_dist,
        score=score,
        undeclared=undeclared,
        candidate_downloads=candidate_downloads,
        target_downloads=target_downloads,
    )


# ---------------------------------------------------------------------------
# Check functions
# ---------------------------------------------------------------------------

def check_secrets_ts(
    repo_path: Path,
    changed_files: list[str] | None = None,
) -> dict:
    """Scan TS/JS files for hardcoded secrets, API keys, and tokens."""
    findings = []
    ts_files = _collect_ts_files(repo_path, changed_files)

    # Also check SKILL.md and .env files
    extra_files: list[tuple[str, str]] = []
    scan_paths = changed_files if changed_files is not None else None
    if scan_paths is None:
        for name in ("SKILL.md", ".env", ".env.local", ".env.production"):
            p = repo_path / name
            if p.exists() and p.is_file():
                try:
                    extra_files.append((name, p.read_text(errors="replace")))
                except Exception:
                    pass
    else:
        for rel in scan_paths:
            pname = Path(rel).name
            if pname in (".env", ".env.local", ".env.production", "SKILL.md"):
                p = repo_path / rel
                if p.exists():
                    try:
                        extra_files.append((rel, p.read_text(errors="replace")))
                    except Exception:
                        pass

    all_files = ts_files + extra_files
    for rel_path, content in all_files:
        for lineno, line in enumerate(content.splitlines(), 1):
            for kind, pattern in _SECRET_PATTERNS:
                if pattern.search(line):
                    findings.append({
                        "file": rel_path,
                        "line": lineno,
                        "kind": kind,
                        "detail": f"Potential {kind} found in {rel_path}:{lineno}",
                    })

    return _make_result("secrets_ts", findings)


def check_hallucinated_imports_ts(
    repo_path: Path,
    changed_files: list[str] | None = None,
    *,
    timeout_s: float | None = None,
) -> dict:
    """Check for imports of packages that do not exist.

    Three-stage pipeline:
    1. Classify imports (relative, builtin, alias, url → skip)
    2. Resolve locally (package.json → lockfile → node_modules)
    3. Verify unresolved packages against npm registry

    Only flags packages confirmed not to exist on npm (404).
    timeout_s: remaining per-skill budget passed to registry calls.
    """
    findings = []
    ts_files = _collect_ts_files(repo_path, changed_files)

    # Load local resolution sources
    pkg_json_deps = _get_package_json_deps(repo_path)
    lockfile_pkgs = _parse_lockfile_packages(repo_path)
    node_modules_pkgs = _get_node_modules_packages(repo_path)
    tsconfig_aliases = _load_tsconfig_paths(repo_path)

    # Collect and classify all imports
    seen_packages: set[str] = set()
    unresolved: list[ClassifiedImport] = []

    for rel_path, content in ts_files:
        for lineno, line in enumerate(content.splitlines(), 1):
            specifiers: list[str] = []
            for m in _REQUIRE_RE.finditer(line):
                specifiers.append(m.group(1))
            for m in _IMPORT_FROM_RE.finditer(line):
                spec = m.group(1) or m.group(2) or m.group(3)
                if spec:
                    specifiers.append(spec)

            for spec in specifiers:
                # Skip template literal interpolations — not real package names
                if "${" in spec or spec.startswith("`"):
                    continue
                # Skip ALL_UPPER_CASE variable names used in dynamic require()
                # Real npm packages are lowercase with hyphens, not UPPER_CASE
                if spec == spec.upper() and "_" in spec and "/" not in spec:
                    continue

                # Stage 1: Classify
                imp = _classify_import(
                    spec, rel_path, lineno,
                    tsconfig_aliases=tsconfig_aliases,
                )

                # Skip non-external imports
                if imp.import_type in ("relative", "builtin", "alias", "url"):
                    continue
                if imp.base_package is None:
                    continue
                # Deduplicate by package name
                if imp.base_package in seen_packages:
                    continue
                seen_packages.add(imp.base_package)

                # Stage 2: Resolve locally
                imp = _resolve_locally(
                    imp,
                    pkg_json_deps=pkg_json_deps,
                    lockfile_pkgs=lockfile_pkgs,
                    node_modules_pkgs=node_modules_pkgs,
                )
                if imp.resolved:
                    continue

                unresolved.append(imp)

    # Stage 3: Verify against npm registry
    for imp in unresolved:
        info = npm_registry.check_package_exists(imp.base_package, timeout_s=timeout_s)  # type: ignore[arg-type]
        if not info.exists:
            confidence = "high"
            if imp.import_type == "scoped":
                confidence = "medium"
            findings.append({
                "file": imp.file_path,
                "line": imp.line_number,
                "kind": "hallucinated_import",
                "detail": f"Package '{imp.base_package}' does not exist on npm — imported in {imp.file_path}:{imp.line_number}",
                "package": imp.base_package,
                "confidence": confidence,
            })

    return _make_result("hallucinated_imports_ts", findings)


def check_typosquat_ts(
    repo_path: Path,
    changed_files: list[str] | None = None,
    *,
    timeout_s: float | None = None,
) -> dict:
    """Check for package names that are suspiciously close to popular packages.

    Download-count gated pipeline:
    1. Classify imports, skip non-external
    2. Skip packages that ARE top npm packages
    3. Find edit-distance matches against _TOP_NPM_PACKAGES
    4. Verify candidate exists on npm AND has <1,000 weekly downloads
    5. Verify target has >100,000 weekly downloads
    6. Only flag when ALL conditions met — NEVER flag if downloads unavailable

    timeout_s: remaining per-skill budget passed to registry/download API calls.
    """
    findings = []
    ts_files = _collect_ts_files(repo_path, changed_files)

    pkg_json_deps = _get_package_json_deps(repo_path)
    tsconfig_aliases = _load_tsconfig_paths(repo_path)

    seen: set[str] = set()
    candidates: list[ClassifiedImport] = []

    for rel_path, content in ts_files:
        for lineno, line in enumerate(content.splitlines(), 1):
            specifiers: list[str] = []
            for m in _REQUIRE_RE.finditer(line):
                specifiers.append(m.group(1))
            for m in _IMPORT_FROM_RE.finditer(line):
                spec = m.group(1) or m.group(2) or m.group(3)
                if spec:
                    specifiers.append(spec)

            for spec in specifiers:
                imp = _classify_import(
                    spec, rel_path, lineno,
                    tsconfig_aliases=tsconfig_aliases,
                )
                if imp.import_type in ("relative", "builtin", "alias", "url"):
                    continue
                if imp.base_package is None:
                    continue
                if imp.base_package in seen:
                    continue
                seen.add(imp.base_package)

                # Skip if it IS a top npm package (no typosquat risk)
                if imp.base_package in _TOP_NPM_PACKAGES:
                    continue

                candidates.append(imp)

    for imp in candidates:
        pkg = imp.base_package
        assert pkg is not None  # guaranteed by filter above

        # Verify package actually exists before any scoring.
        # A package that doesn't exist is a hallucinated import issue,
        # not a typosquat issue.
        info = npm_registry.check_package_exists(pkg, timeout_s=timeout_s)
        if not info.exists:
            continue

        # Get download counts for candidate and find the best matching target
        candidate_downloads = npm_registry.get_weekly_downloads(pkg, timeout_s=timeout_s)

        # Find the closest matching top package for download lookup
        pkg_norm = pkg.lower().replace("-", "_")
        best_match: str | None = None
        best_dist: int = 999
        for known_pkg in _TOP_NPM_PACKAGES:
            known_norm = known_pkg.lower().replace("-", "_")
            if len(known_norm) < 3:
                continue
            dist = _levenshtein(pkg_norm, known_norm)
            if 0 < dist <= 2 and dist < best_dist:
                best_dist = dist
                best_match = known_pkg

        if best_match is None:
            continue

        target_downloads = npm_registry.get_weekly_downloads(best_match, timeout_s=timeout_s)

        # Compute typosquat risk with download-count gating
        risk = _calculate_typosquat_risk(
            pkg,
            pkg_json_deps=pkg_json_deps,
            candidate_downloads=candidate_downloads,
            target_downloads=target_downloads,
        )
        if risk is None:
            continue
        if risk.score < 0.5:
            continue

        findings.append({
            "file": imp.file_path,
            "line": imp.line_number,
            "kind": "typosquat",
            "detail": (
                f"Package '{pkg}' ({candidate_downloads} downloads/week) is edit distance "
                f"{risk.distance} from '{risk.similar_to}' ({target_downloads} downloads/week) "
                f"— review recommended (risk score: {risk.score:.1f})"
            ),
            "package": pkg,
            "similar_to": risk.similar_to,
            "distance": risk.distance,
            "risk_score": risk.score,
            "undeclared": risk.undeclared,
            "candidate_downloads": candidate_downloads,
            "target_downloads": target_downloads,
        })

    return _make_result("typosquat_ts", findings)


def check_fake_tests_ts(
    repo_path: Path,
    changed_files: list[str] | None = None,
) -> dict:
    """Detect empty test bodies, tautological assertions, and skipped tests."""
    findings = []
    ts_files = _collect_ts_files(repo_path, changed_files)

    for rel_path, content in ts_files:
        # Skip non-test files
        name_lower = Path(rel_path).name.lower()
        is_test = any(p in name_lower for p in (".test.", ".spec.", "_test.", "_spec."))
        if not is_test and "test" not in Path(rel_path).parts:
            continue

        for m in _EMPTY_TEST_BODY.finditer(content):
            findings.append({
                "file": rel_path,
                "line": content[:m.start()].count("\n") + 1,
                "kind": "empty_test",
                "detail": f"Empty test body in {rel_path}",
            })

        for m in _TAUTOLOGICAL_EXPECT.finditer(content):
            findings.append({
                "file": rel_path,
                "line": content[:m.start()].count("\n") + 1,
                "kind": "tautological_assertion",
                "detail": f"Tautological assertion in {rel_path}: {m.group(0)[:80]}",
            })

        for m in _SKIPPED_TEST.finditer(content):
            findings.append({
                "file": rel_path,
                "line": content[:m.start()].count("\n") + 1,
                "kind": "skipped_test",
                "detail": f"Skipped test in {rel_path}",
            })

        for m in _DESCRIBE_NO_TESTS.finditer(content):
            findings.append({
                "file": rel_path,
                "line": content[:m.start()].count("\n") + 1,
                "kind": "empty_describe",
                "detail": f"Empty describe block in {rel_path}",
            })

    return _make_result("fake_tests_ts", findings)


def check_prompt_injection_ts(
    repo_path: Path,
    changed_files: list[str] | None = None,
) -> dict:
    """Heuristic detection of suspicious prompt-construction patterns.

    This is NOT taint analysis, framework-specific dataflow modelling,
    or exploit proof. It is a heuristic review aid for suspicious
    prompt-construction patterns.
    """
    findings = []
    ts_files = _collect_ts_files(repo_path, changed_files)

    for rel_path, content in ts_files:
        lines = content.splitlines()
        for lineno, line in enumerate(lines, 1):
            # Skip lines that are clearly logging or test assertions
            if _LOGGING_LINE.match(line.lstrip()):
                continue

            # Heuristic 1: template literal with ${...} near prompt keywords
            if "${" in line and "`" in line and _PROMPT_KEYWORDS.search(line):
                findings.append({
                    "file": rel_path,
                    "line": lineno,
                    "kind": "template_injection",
                    "detail": (
                        f"Suspicious prompt-construction pattern: "
                        f"template literal with interpolation near prompt keyword in {rel_path}:{lineno}"
                    ),
                    "confidence": "high",
                })

            # Heuristic 2: string concatenation near prompt keywords
            if "+" in line and _PROMPT_KEYWORDS.search(line):
                # Check if it looks like string concatenation building a prompt
                if re.search(r"""["']\s*\+\s*\w+|\w+\s*\+\s*["']""", line):
                    findings.append({
                        "file": rel_path,
                        "line": lineno,
                        "kind": "concat_injection",
                        "detail": (
                            f"Suspicious prompt-construction pattern: "
                            f"string concatenation near prompt keyword in {rel_path}:{lineno}"
                        ),
                        "confidence": "medium",
                    })

            # Heuristic 3: LLM API call with template literal in body
            if _LLM_API_PATTERNS.search(line) and "${" in line:
                findings.append({
                    "file": rel_path,
                    "line": lineno,
                    "kind": "api_template_injection",
                    "detail": (
                        f"Heuristic prompt-injection risk indicator: "
                        f"LLM API pattern with template interpolation in {rel_path}:{lineno}"
                    ),
                    "confidence": "high",
                })

    # Note: external_prompt_url heuristic (SKILL.md frontmatter URLs) was
    # removed after calibration showed 0% precision (0 TP in 43 samples).
    # Every SKILL.md has URLs; flagging all of them is pure noise.

    return _make_result("prompt_injection_ts", findings)


def check_placeholders_ts(
    repo_path: Path,
    changed_files: list[str] | None = None,
) -> dict:
    """Detect TODO, FIXME, throw new Error('Not implemented') patterns."""
    findings = []
    ts_files = _collect_ts_files(repo_path, changed_files)

    for rel_path, content in ts_files:
        for lineno, line in enumerate(content.splitlines(), 1):
            for kind, pattern in _PLACEHOLDER_PATTERNS:
                if pattern.search(line):
                    findings.append({
                        "file": rel_path,
                        "line": lineno,
                        "kind": kind,
                        "detail": f"Placeholder found in {rel_path}:{lineno}",
                    })

    return _make_result("placeholders_ts", findings)


def check_syntax_ts(
    repo_path: Path,
    changed_files: list[str] | None = None,
    *,
    timeout_s: float | None = None,
) -> dict:
    """Run tsc --noEmit or node --check for syntax validation.

    Degrades to SKIPPED with explicit metadata if Node/tsc not available.
    Always emits a full result object regardless of outcome.
    timeout_s: remaining per-skill budget for subprocess calls.
    """
    # Check Node availability
    node_path = shutil.which("node")
    if not node_path:
        return _make_result(
            "syntax_ts", [],
            error="node_not_found",
            status_override="SKIPPED",
        )

    findings = []
    ts_files = _collect_ts_files(repo_path, changed_files)
    if not ts_files:
        return _make_result("syntax_ts", [])

    # Check if tsc is available
    tsc_path = shutil.which("tsc")
    has_tsconfig = (repo_path / "tsconfig.json").exists()

    # Compute effective timeout for subprocess calls
    effective_timeout = timeout_s if timeout_s is not None else 30

    # If we have tsc and tsconfig, use tsc for .ts files
    if tsc_path and has_tsconfig:
        try:
            result = subprocess.run(
                [tsc_path, "--noEmit"],
                cwd=str(repo_path),
                capture_output=True,
                text=True,
                timeout=effective_timeout,
            )
            if result.returncode != 0:
                for line in (result.stdout or "").splitlines():
                    # Parse tsc error format: file(line,col): error TS1234: message
                    m = re.match(r"(.+?)\((\d+),\d+\):\s*error\s+(TS\d+):\s*(.+)", line)
                    if m:
                        findings.append({
                            "file": m.group(1),
                            "line": int(m.group(2)),
                            "kind": "typescript_error",
                            "detail": f"{m.group(3)}: {m.group(4)}",
                        })
        except (subprocess.TimeoutExpired, OSError):
            pass
    else:
        # Fall back to node --check for .js files
        for rel_path, _ in ts_files:
            if not rel_path.endswith((".js", ".mjs", ".cjs")):
                continue
            full_path = repo_path / rel_path
            try:
                result = subprocess.run(
                    [node_path, "--check", str(full_path)],
                    capture_output=True,
                    text=True,
                    timeout=min(effective_timeout, 10),
                )
                if result.returncode != 0:
                    findings.append({
                        "file": rel_path,
                        "line": 0,
                        "kind": "syntax_error",
                        "detail": (result.stderr or "").strip()[:200],
                    })
            except (subprocess.TimeoutExpired, OSError):
                pass

    if not tsc_path and has_tsconfig:
        return _make_result(
            "syntax_ts", findings,
            error="tsc_not_found",
            status_override="SKIPPED" if not findings else None,
        )

    return _make_result("syntax_ts", findings)


# ---------------------------------------------------------------------------
# Security governance TS checks (SEC-001 through SEC-018 + SEC-OPS-001)
# ---------------------------------------------------------------------------

def _ts_files(repo_path: Path, changed_files: list[str] | None) -> list[tuple[str, str]]:
    """Yield (rel_path, content) for TS/JS files.

    Skips ``.d.ts`` declaration files — they contain only type information
    and cannot be route handlers, auth code, or executable logic.
    """
    from .language_detect import is_ts_js
    results = []
    targets = changed_files if changed_files else []
    for rel_path in sorted(targets):
        if not is_ts_js(rel_path):
            continue
        if rel_path.endswith(".d.ts"):
            continue
        path = repo_path / rel_path
        if not path.exists() or not path.is_file():
            continue
        try:
            results.append((rel_path, path.read_text()))
        except Exception:
            continue
    return results


def check_hardcoded_jwt_ts(repo_path: Path, changed_files: list[str] | None = None) -> dict:
    """SEC-001 TS: Hardcoded JWT secrets."""
    findings = []
    _secret_var = re.compile(r"""\b(secret|jwt|signing_key|token_key|auth_key|private_key)\b""", re.IGNORECASE)
    _literal_assign = re.compile(r"""(const|let|var)\s+\w*(secret|jwt|signing|token_key)\w*\s*=\s*['"]""", re.IGNORECASE)
    _jwt_sign_literal = re.compile(r"""jwt\.sign\s*\([^,]+,\s*['"]""")
    _env_fallback = re.compile(r"""process\.env\.\w+\s*\|\|\s*['"]([^'"]+)['"]""")

    for rel_path, content in _ts_files(repo_path, changed_files):
        for lineno, line in enumerate(content.splitlines(), 1):
            if _literal_assign.search(line):
                findings.append({"rule_id": "SEC-001", "file": rel_path, "line": lineno,
                    "kind": "hardcoded_secret", "detail": "JWT/auth secret assigned as string literal",
                    "remediation": "Use process.env.SECRET_KEY with no fallback default.",
                    "confidence": "high", "cwe": "CWE-798", "owasp": "A02:2021"})
            if _jwt_sign_literal.search(line):
                findings.append({"rule_id": "SEC-001", "file": rel_path, "line": lineno,
                    "kind": "jwt_literal_secret", "detail": "jwt.sign called with string literal secret",
                    "remediation": "Pass secret from environment variable.",
                    "confidence": "high", "cwe": "CWE-798", "owasp": "A02:2021"})
            m = _env_fallback.search(line)
            if m and _secret_var.search(line):
                findings.append({"rule_id": "SEC-001", "file": rel_path, "line": lineno,
                    "kind": "env_fallback_secret", "detail": f"Environment variable with string fallback: '{m.group(1)[:20]}'",
                    "remediation": "Remove fallback default. Fail if env var is missing.",
                    "confidence": "high", "cwe": "CWE-798", "owasp": "A02:2021"})
    return _make_result("hardcoded_jwt_ts", findings)


def check_auth_bypass_ts(repo_path: Path, changed_files: list[str] | None = None) -> dict:
    """SEC-002 TS: Routes without auth middleware."""
    findings = []
    _route = re.compile(r"""(app|router)\.(get|post|put|patch|delete)\s*\(\s*['"]([^'"]+)['"]""")
    _auth_mw = re.compile(r"""\b(authenticate|requireAuth|isAuthenticated|verifyToken|passport\.authenticate|authMiddleware|protect|guard)\b""", re.IGNORECASE)
    _public = {"/health", "/healthz", "/login", "/register", "/signup", "/signin", "/", "/docs", "/favicon.ico"}

    for rel_path, content in _ts_files(repo_path, changed_files):
        has_auth = _auth_mw.search(content)
        for lineno, line in enumerate(content.splitlines(), 1):
            m = _route.search(line)
            if m:
                path = m.group(3).split("?")[0].rstrip("/") or "/"
                if path in _public:
                    continue
                if not has_auth or not _auth_mw.search(line):
                    findings.append({"rule_id": "SEC-002", "file": rel_path, "line": lineno,
                        "kind": "route_no_auth", "detail": f"Route '{path}' without auth middleware in handler chain",
                        "remediation": "Add auth middleware to the route handler chain.",
                        "confidence": "medium", "cwe": "CWE-284", "owasp": "A01:2021"})
    return _make_result("auth_bypass_ts", findings)


def check_websocket_auth_ts(repo_path: Path, changed_files: list[str] | None = None) -> dict:
    """SEC-003 TS: WebSocket handlers without auth."""
    findings = []
    _ws = re.compile(r"""(io\.on|socket\.on|\.on\s*\(\s*['"]connect|WebSocket|ws\.on)""", re.IGNORECASE)
    _ws_auth = re.compile(r"""(verify_token|authenticate|jwt\.verify|socket\.handshake\.auth|middleware|token)""", re.IGNORECASE)
    _origin = re.compile(r"""\borigin\b.*\b(check|allow|verify|cors)\b""", re.IGNORECASE)

    for rel_path, content in _ts_files(repo_path, changed_files):
        if not _ws.search(content):
            continue
        if not _ws_auth.search(content):
            findings.append({"rule_id": "SEC-003", "file": rel_path, "line": 1,
                "kind": "ws_no_auth", "detail": "WebSocket handler without authentication",
                "remediation": "Add auth verification to WebSocket connection handler.",
                "confidence": "medium", "cwe": "CWE-287", "owasp": "A07:2021"})
        if not _origin.search(content):
            findings.append({"rule_id": "SEC-003", "file": rel_path, "line": 1,
                "kind": "ws_no_origin_check", "detail": "WebSocket without Origin validation",
                "remediation": "Validate Origin header against allowlist.",
                "confidence": "low", "cwe": "CWE-346", "owasp": "A07:2021"})
    return _make_result("websocket_auth_ts", findings)


def check_oauth_flow_ts(repo_path: Path, changed_files: list[str] | None = None) -> dict:
    """SEC-004 TS: OAuth flow integrity."""
    findings = []
    _callback = re.compile(r"""\b(callback|oauth.*redirect|auth.*callback)\b""", re.IGNORECASE)
    _state_val = re.compile(r"""\bstate\b.*\b(verify|validate|check|===|!==)\b""", re.IGNORECASE)
    _pkce = re.compile(r"""\b(code_verifier|code_challenge|pkce)\b""", re.IGNORECASE)

    for rel_path, content in _ts_files(repo_path, changed_files):
        if _is_test_file(rel_path):
            continue
        if not _callback.search(content):
            continue
        if not _state_val.search(content):
            findings.append({"rule_id": "SEC-004", "file": rel_path, "line": 1,
                "kind": "missing_oauth_state", "detail": "OAuth callback without state validation",
                "remediation": "Validate state parameter in callback.",
                "confidence": "medium", "cwe": "CWE-352", "owasp": "A07:2021"})
        if not _pkce.search(content):
            findings.append({"rule_id": "SEC-004", "file": rel_path, "line": 1,
                "kind": "missing_pkce", "detail": "OAuth flow without PKCE",
                "remediation": "Implement PKCE with code_verifier and code_challenge.",
                "confidence": "low", "cwe": "CWE-352", "owasp": "A07:2021"})
    return _make_result("oauth_flow_ts", findings)


def check_cookie_security_hard_ts(repo_path: Path, changed_files: list[str] | None = None) -> dict:
    """SEC-005 TS: Critical cookie misconfigurations."""
    findings = []
    _set_cookie = re.compile(r"""(res\.cookie|response\.cookie|setCookie|set-cookie)\s*\(""", re.IGNORECASE)
    _samesite_none = re.compile(r"""sameSite\s*:\s*['"]?none['"]?""", re.IGNORECASE)
    _secure = re.compile(r"""\bsecure\s*:\s*true\b""", re.IGNORECASE)
    _httponly = re.compile(r"""\bhttpOnly\s*:\s*true\b""", re.IGNORECASE)
    _session_name = re.compile(r"""['"]?(session|refresh|token|access_token|sid|jwt)['"]?""", re.IGNORECASE)

    for rel_path, content in _ts_files(repo_path, changed_files):
        lines = content.splitlines()
        for lineno, line in enumerate(lines, 1):
            if not _set_cookie.search(line):
                continue
            ctx = "\n".join(lines[lineno-1:min(len(lines), lineno+5)])
            if _samesite_none.search(ctx) and not _secure.search(ctx):
                findings.append({"rule_id": "SEC-005", "file": rel_path, "line": lineno,
                    "kind": "samesite_none_no_secure", "detail": "SameSite=None without Secure",
                    "remediation": "Add secure: true when using sameSite: 'none'.",
                    "confidence": "high", "cwe": "CWE-614", "owasp": "A02:2021"})
            if _session_name.search(ctx) and not _httponly.search(ctx):
                findings.append({"rule_id": "SEC-005", "file": rel_path, "line": lineno,
                    "kind": "session_cookie_no_httponly", "detail": "Session cookie without httpOnly",
                    "remediation": "Add httpOnly: true.",
                    "confidence": "high", "cwe": "CWE-614", "owasp": "A02:2021"})
    return _make_result("cookie_security_hard_ts", findings)


def check_weak_randomness_ts(repo_path: Path, changed_files: list[str] | None = None) -> dict:
    """SEC-006 TS: Non-cryptographic random in auth contexts."""
    findings = []
    _weak = re.compile(r"""\bMath\.random\s*\(""")
    _auth_ctx = re.compile(r"""\b(token|secret|session|reset|invite|code|otp|key|nonce|password|room)\b""", re.IGNORECASE)

    for rel_path, content in _ts_files(repo_path, changed_files):
        lines = content.splitlines()
        for lineno, line in enumerate(lines, 1):
            if _weak.search(line):
                ctx = "\n".join(lines[max(0,lineno-4):min(len(lines),lineno+3)])
                if _auth_ctx.search(ctx):
                    findings.append({"rule_id": "SEC-006", "file": rel_path, "line": lineno,
                        "kind": "weak_random_in_auth", "detail": "Math.random() in auth context. Use crypto.randomBytes().",
                        "remediation": "Replace Math.random() with crypto.randomBytes() or crypto.randomUUID().",
                        "confidence": "medium", "cwe": "CWE-338", "owasp": "A02:2021"})
    return _make_result("weak_randomness_ts", findings)


def check_token_revocation_ts(repo_path: Path, changed_files: list[str] | None = None) -> dict:
    """SEC-007 TS: Token revocation in logout/reset handlers."""
    findings = []
    _handler = re.compile(r"""\b(logout|signOut|sign_out|resetPassword|reset_password)\b""", re.IGNORECASE)
    _invalidation = re.compile(r"""\b(blacklist|revoke|invalidate|delete.*token|destroy.*session|clearCookie|res\.clearCookie)\b""", re.IGNORECASE)

    for rel_path, content in _ts_files(repo_path, changed_files):
        if _is_test_file(rel_path):
            continue
        if not _handler.search(content):
            continue
        if not _invalidation.search(content):
            findings.append({"rule_id": "SEC-007", "file": rel_path, "line": 1,
                "kind": "missing_token_invalidation", "detail": "Logout/reset handler without token invalidation",
                "remediation": "Add token blacklisting or session destruction.",
                "confidence": "medium", "cwe": "CWE-613", "owasp": "A07:2021"})
    return _make_result("token_revocation_ts", findings)


def check_jwt_verification_ts(repo_path: Path, changed_files: list[str] | None = None) -> dict:
    """SEC-008 TS: JWT verification policy."""
    findings = []
    _verify = re.compile(r"""\bjwt\.verify\s*\(""")
    _algorithms = re.compile(r"""\balgorithms\s*:\s*\[""")
    _alg_none = re.compile(r"""['"]none['"]""", re.IGNORECASE)

    for rel_path, content in _ts_files(repo_path, changed_files):
        lines = content.splitlines()
        for lineno, line in enumerate(lines, 1):
            if not _verify.search(line):
                continue
            ctx = "\n".join(lines[lineno-1:min(len(lines), lineno+3)])
            if not _algorithms.search(ctx):
                findings.append({"rule_id": "SEC-008", "file": rel_path, "line": lineno,
                    "kind": "unpinned_algorithm", "detail": "jwt.verify without pinning algorithms",
                    "remediation": "Pass algorithms: ['HS256'] in options.",
                    "confidence": "high", "cwe": "CWE-327", "owasp": "A02:2021"})
            if _alg_none.search(ctx):
                findings.append({"rule_id": "SEC-008", "file": rel_path, "line": lineno,
                    "kind": "alg_none_allowed", "detail": "alg='none' allowed in JWT verification",
                    "remediation": "Remove 'none' from algorithms list.",
                    "confidence": "high", "cwe": "CWE-327", "owasp": "A02:2021"})
    return _make_result("jwt_verification_ts", findings)


def check_csrf_state_change_ts(repo_path: Path, changed_files: list[str] | None = None) -> dict:
    """SEC-009 TS: CSRF on cookie-backed state-changing requests."""
    findings = []
    _cookie_auth = re.compile(r"""\b(session|cookie|credentials.*include|withCredentials)\b""", re.IGNORECASE)
    _state_change = re.compile(r"""(app|router)\.(post|put|patch|delete)\s*\(""", re.IGNORECASE)
    _csrf = re.compile(r"""\b(csurf|CSRFProtection|csrfToken|_csrf\b|X-CSRF-Token|csrfMiddleware|sameSite\s*:\s*['"]?strict['"]?|SameSite\s*=\s*Strict)\b""", re.IGNORECASE)

    for rel_path, content in _ts_files(repo_path, changed_files):
        if not _cookie_auth.search(content):
            continue
        if _csrf.search(content):
            continue
        for lineno, line in enumerate(content.splitlines(), 1):
            if _state_change.search(line):
                findings.append({"rule_id": "SEC-009", "file": rel_path, "line": lineno,
                    "kind": "csrf_missing", "detail": "State-changing route with cookie auth but no CSRF protection",
                    "remediation": "Add csurf middleware or equivalent CSRF protection.",
                    "confidence": "medium", "cwe": "CWE-352", "owasp": "A01:2021"})
    return _make_result("csrf_state_change_ts", findings)


def check_rate_limit_wiring_ts(repo_path: Path, changed_files: list[str] | None = None) -> dict:
    """SEC-010 TS: Rate limiting imported but not applied."""
    findings = []
    _import = re.compile(r"""\b(express-rate-limit|rate-limiter-flexible|express-slow-down)\b""")
    _applied = re.compile(r"""\b(app\.use\s*\(\s*\w*[Ll]imit|rateLimiter|limiter\))\b""")

    for rel_path, content in _ts_files(repo_path, changed_files):
        if _import.search(content) and not _applied.search(content):
            findings.append({"rule_id": "SEC-010", "file": rel_path, "line": 1,
                "kind": "rate_limit_not_applied", "detail": "Rate limiter imported but not applied",
                "remediation": "Wire limiter to app: app.use(limiter) or app.use('/auth', limiter).",
                "confidence": "medium", "cwe": "CWE-307", "owasp": "A07:2021"})
    return _make_result("rate_limit_wiring_ts", findings)


def check_cookie_security_soft_ts(repo_path: Path, changed_files: list[str] | None = None) -> dict:
    """SEC-011 TS: Non-critical cookie concerns."""
    findings = []
    _set_cookie = re.compile(r"""(res\.cookie|setCookie)\s*\(""", re.IGNORECASE)
    _samesite = re.compile(r"""\bsameSite\s*:""", re.IGNORECASE)
    _session_name = re.compile(r"""['"]?(session|refresh|token|sid|jwt)['"]?""", re.IGNORECASE)

    for rel_path, content in _ts_files(repo_path, changed_files):
        lines = content.splitlines()
        for lineno, line in enumerate(lines, 1):
            if not _set_cookie.search(line):
                continue
            ctx = "\n".join(lines[lineno-1:min(len(lines), lineno+5)])
            if _session_name.search(ctx) and not _samesite.search(ctx):
                findings.append({"rule_id": "SEC-011", "file": rel_path, "line": lineno,
                    "kind": "missing_samesite", "detail": "Session cookie without explicit sameSite",
                    "remediation": "Set sameSite: 'lax' or 'strict'.",
                    "confidence": "medium", "cwe": "CWE-614", "owasp": "A02:2021"})
    return _make_result("cookie_security_soft_ts", findings)


def check_token_expiry_ts(repo_path: Path, changed_files: list[str] | None = None) -> dict:
    """SEC-012 TS: JWT creation without expiration."""
    findings = []
    _jwt_sign = re.compile(r"""\bjwt\.sign\s*\(""")
    _expires = re.compile(r"""\b(expiresIn|exp)\b""")

    for rel_path, content in _ts_files(repo_path, changed_files):
        lines = content.splitlines()
        for lineno, line in enumerate(lines, 1):
            if _jwt_sign.search(line):
                ctx = "\n".join(lines[lineno-1:min(len(lines), lineno+4)])
                if not _expires.search(ctx):
                    findings.append({"rule_id": "SEC-012", "file": rel_path, "line": lineno,
                        "kind": "missing_token_expiry", "detail": "jwt.sign without expiresIn",
                        "remediation": "Add expiresIn option: jwt.sign(payload, secret, { expiresIn: '1h' }).",
                        "confidence": "medium", "cwe": "CWE-613", "owasp": "A07:2021"})
    return _make_result("token_expiry_ts", findings)


def check_xss_check_ts(repo_path: Path, changed_files: list[str] | None = None) -> dict:
    """SEC-013 TS: Dangerous XSS sinks."""
    findings = []
    _sinks = [
        (re.compile(r"""\bdangerouslySetInnerHTML\b"""), "dangerouslySetInnerHTML in JSX"),
        (re.compile(r"""\binnerHTML\s*="""), "innerHTML assignment"),
        (re.compile(r"""\bv-html\b"""), "v-html directive in Vue"),
    ]

    for rel_path, content in _ts_files(repo_path, changed_files):
        if _is_vendored_or_minified(rel_path):
            continue
        for lineno, line in enumerate(content.splitlines(), 1):
            for pat, desc in _sinks:
                if pat.search(line):
                    findings.append({"rule_id": "SEC-013", "file": rel_path, "line": lineno,
                        "kind": "dangerous_xss_sink", "detail": f"XSS sink: {desc}",
                        "remediation": "Escape or sanitize user input. Avoid dangerous sinks.",
                        "confidence": "medium", "cwe": "CWE-79", "owasp": "A03:2021"})
    return _make_result("xss_check_ts", findings)


def check_idor_check_ts(repo_path: Path, changed_files: list[str] | None = None) -> dict:
    """SEC-014 TS: User-supplied IDs without ownership checks."""
    findings = []
    _route_with_id = re.compile(r"""(app|router)\.(get|post|put|delete)\s*\(\s*['"]([^'"]*:\w*[Ii]d[^'"]*)['"]""")
    _ownership = re.compile(r"""\b(req\.user|currentUser|request\.user)\b""")

    for rel_path, content in _ts_files(repo_path, changed_files):
        lines = content.splitlines()
        for lineno, line in enumerate(lines, 1):
            m = _route_with_id.search(line)
            if not m:
                continue
            # Check nearby lines for ownership
            ctx = "\n".join(lines[lineno-1:min(len(lines), lineno+10)])
            if not _ownership.search(ctx):
                findings.append({"rule_id": "SEC-014", "file": rel_path, "line": lineno,
                    "kind": "idor_no_ownership", "detail": f"Route with ID param but no ownership check",
                    "remediation": "Verify req.user owns the requested resource.",
                    "confidence": "low", "cwe": "CWE-639", "owasp": "A01:2021"})
    return _make_result("idor_check_ts", findings)


def check_sql_injection_ts(repo_path: Path, changed_files: list[str] | None = None) -> dict:
    """SEC-015 TS: SQL string building.

    I.2: narrowed from the original "any SQL verb + template literal" heuristic
    to require evidence of ACTUAL SQL context.  The bare verbs SELECT /
    INSERT / UPDATE / DELETE / DROP are ambiguous in TypeScript code —
    ``DELETE`` appears in HTTP method values and URL paths (``fetch(\\`/api/delete/${id}\\`,
    { method: 'DELETE' })``) far more often than in SQL strings.  To
    distinguish, the rule now ALSO requires one of:

      * an SQL structural keyword on the same line
        (``FROM``, ``WHERE``, ``VALUES``, ``JOIN``, ``INTO``, ``SET``, ``TABLE``)
      * a tagged SQL template literal or DB-library call pattern
        (``sql\\``, ``SQL\\``, ``db.raw(\\``, ``knex.raw(\\``, ``.query(\\``,
        ``.execute(\\``)

    That removes HTTP-DELETE false positives while keeping real SQL-string-
    building cases (``\\`DELETE FROM users WHERE id=${id}\\``,
    ``sql\\`UPDATE … SET …\\```, ``db.query(\\`INSERT INTO …\\`)``) as
    positives.
    """
    findings = []
    _sql = re.compile(r"""\b(SELECT|INSERT|UPDATE|DELETE|DROP)\b""", re.IGNORECASE)
    _template = re.compile(r"""`[^`]*\$\{""")
    # I.2: SQL-context guards — at least one must match in addition to the
    # verb + template literal, or the line is not flagged.
    _sql_companion = re.compile(
        r"""\b(FROM|WHERE|VALUES|JOIN|INTO|SET|TABLE)\b""",
        re.IGNORECASE,
    )
    # Tagged-template literals and DB-library calls that wrap the template
    # literal.  We look for the tag immediately followed by a backtick.
    _sql_tagged = re.compile(
        r"""(?:(?<![\w.])(?:sql|SQL|Sql)\s*`)"""
        r"""|(?:\b(?:db|conn|client|pg|pool)\.(?:raw|query|execute)\s*\(\s*`)"""
        r"""|(?:\bknex\.raw\s*\(\s*`)"""
        r"""|(?:\b(?:query|sqlQuery)\s*=\s*`)"""
    )

    for rel_path, content in _ts_files(repo_path, changed_files):
        for lineno, line in enumerate(content.splitlines(), 1):
            if not (_sql.search(line) and _template.search(line)):
                continue
            if not (_sql_companion.search(line) or _sql_tagged.search(line)):
                # Looks SQL-verb-shaped but no SQL context on the line —
                # likely an HTTP DELETE method or URL path.  Skip.
                continue
            findings.append({"rule_id": "SEC-015", "file": rel_path, "line": lineno,
                "kind": "sql_string_building", "detail": "SQL in template literal with interpolation",
                "remediation": "Use parameterized queries.",
                "confidence": "medium", "cwe": "CWE-89", "owasp": "A03:2021"})
    return _make_result("sql_injection_ts", findings)


def check_client_trusted_logic_ts(repo_path: Path, changed_files: list[str] | None = None) -> dict:
    """SEC-016 TS: Client-supplied authority fields."""
    findings = []
    _fields = re.compile(r"""\breq\.body\.(score|role|balance|admin|isAdmin|is_admin|permissions|price|amount|rank|level)\b""", re.IGNORECASE)

    for rel_path, content in _ts_files(repo_path, changed_files):
        for lineno, line in enumerate(content.splitlines(), 1):
            if _fields.search(line):
                findings.append({"rule_id": "SEC-016", "file": rel_path, "line": lineno,
                    "kind": "client_trusted_field", "detail": "Server accepts client-supplied authority field",
                    "remediation": "Validate or recompute server-side.",
                    "confidence": "low", "cwe": "CWE-602", "owasp": "A04:2021"})
    return _make_result("client_trusted_logic_ts", findings)


def check_user_enumeration_ts(repo_path: Path, changed_files: list[str] | None = None) -> dict:
    """SEC-017 TS: User enumeration via differentiated messages."""
    findings = []
    _auth = re.compile(r"""\b(login|signin|register|signup|resetPassword|forgotPassword)\b""", re.IGNORECASE)
    _enum_msg = re.compile(r"""['"].*\b(user not found|email not found|account not found|already registered|already exists|username taken)\b.*['"]""", re.IGNORECASE)

    for rel_path, content in _ts_files(repo_path, changed_files):
        if not _auth.search(content):
            continue
        for lineno, line in enumerate(content.splitlines(), 1):
            if _enum_msg.search(line):
                findings.append({"rule_id": "SEC-017", "file": rel_path, "line": lineno,
                    "kind": "user_enumeration", "detail": "Differentiated error reveals account existence",
                    "remediation": "Use generic messages like 'Invalid credentials'.",
                    "confidence": "medium", "cwe": "CWE-204", "owasp": "A07:2021"})
    return _make_result("user_enumeration_ts", findings)


def check_rate_limit_backend_quality_ts(repo_path: Path, changed_files: list[str] | None = None) -> dict:
    """SEC-018 TS: Rate limiter with in-memory store."""
    findings = []
    _limiter = re.compile(r"""\b(rateLimit|RateLimiter|express-rate-limit)\b""")
    _external = re.compile(r"""\b(RedisStore|redis|memcached|rate-limit-redis|rate-limit-mongo)\b""", re.IGNORECASE)

    for rel_path, content in _ts_files(repo_path, changed_files):
        if _limiter.search(content) and not _external.search(content):
            findings.append({"rule_id": "SEC-018", "file": rel_path, "line": 1,
                "kind": "memory_store_rate_limit", "detail": "Rate limiter with default in-memory store",
                "remediation": "Configure external store (Redis) for production.",
                "confidence": "medium", "cwe": "CWE-307", "owasp": "A07:2021"})
    return _make_result("rate_limit_backend_quality_ts", findings)


def check_security_event_logging_ts(repo_path: Path, changed_files: list[str] | None = None) -> dict:
    """SEC-OPS-001 TS: Security event logging."""
    findings = []
    _auth_fn = re.compile(r"""\b(login|logout|signOut|resetPassword|authenticate|verifyToken)\b""", re.IGNORECASE)
    _structured = re.compile(r"""\b(logger\.\w+\s*\(\s*\{|winston|pino|structuredLog|event_type)\b""", re.IGNORECASE)
    _bare = re.compile(r"""\bconsole\.(log|warn|error)\s*\(\s*['"]""")

    for rel_path, content in _ts_files(repo_path, changed_files):
        if _is_test_file(rel_path):
            continue
        if not _auth_fn.search(content):
            continue
        has_structured = _structured.search(content)
        has_bare = _bare.search(content)
        if not has_structured and not has_bare:
            findings.append({"rule_id": "SEC-OPS-001", "file": rel_path, "line": 1,
                "kind": "missing_security_logging", "detail": "Auth handler without security event logging",
                "remediation": "Add structured logging with event_type, actor, outcome fields.",
                "confidence": "medium", "cwe": "CWE-778", "owasp": "A09:2021"})
        elif has_bare and not has_structured:
            findings.append({"rule_id": "SEC-OPS-001", "file": rel_path, "line": 1,
                "kind": "unstructured_security_logging", "detail": "Auth handler with only console.log, not structured events",
                "remediation": "Use structured logger (winston, pino) with event_type, actor, outcome.",
                "confidence": "medium", "cwe": "CWE-778", "owasp": "A09:2021"})
    return _make_result("security_event_logging_ts", findings)


def _load_tsc_baseline(saturnday_dir: Path) -> dict[str, int]:
    """Load tsc error counts per file from baseline JSON.

    Returns a mapping of ``file_path -> error_count`` or an empty dict when
    the baseline file does not exist or cannot be parsed.
    """
    baseline_path = saturnday_dir / "tsc-baseline.json"
    if not baseline_path.exists():
        return {}
    try:
        return json.loads(baseline_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def check_type_check_ts(
    repo_path: Path,
    changed_files: list[str] | None = None,
    *,
    timeout_s: float | None = None,
) -> dict:
    """Run a full tsc type check (separate from syntax_ts).

    This is a WARNING-only, ratcheted type-checking pass.  It differs from
    ``check_syntax_ts`` in that:
    - It is always WARNING severity (never FAIL).
    - It compares against a per-file baseline stored in
      ``.saturnday/tsc-baseline.json`` and only surfaces *new* errors.
    - Its results are registered in ``ALL_TS_CHECKS`` under the name
      ``type_check_ts``.

    Degrades to SKIPPED when tsc or tsconfig.json is missing.
    """
    tsc_path = shutil.which("tsc")
    has_tsconfig = (repo_path / "tsconfig.json").exists()

    if not tsc_path or not has_tsconfig:
        return _make_result(
            "type_check_ts", [],
            error="tsc_not_found" if not tsc_path else "tsconfig_not_found",
            status_override="SKIPPED",
        )

    effective_timeout = timeout_s if timeout_s is not None else 30

    try:
        result = subprocess.run(
            [tsc_path, "--noEmit", "--pretty", "false"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=effective_timeout,
        )
    except subprocess.TimeoutExpired:
        return _make_result(
            "type_check_ts", [],
            error="tsc_timeout",
            status_override="SKIPPED",
        )
    except OSError:
        return _make_result(
            "type_check_ts", [],
            error="tsc_exec_error",
            status_override="SKIPPED",
        )

    raw_output = result.stdout or ""

    # Parse tsc error format: file(line,col): error TSxxxx: message
    error_pattern = re.compile(r"^(.+?)\((\d+),\d+\):\s*error\s+(TS\d+):\s*(.+)$", re.MULTILINE)
    raw_findings: dict[str, list[dict]] = {}
    for m in error_pattern.finditer(raw_output):
        file_path = m.group(1).strip()
        line_no = int(m.group(2))
        ts_code = m.group(3)
        message = m.group(4).strip()
        raw_findings.setdefault(file_path, []).append({
            "file": file_path,
            "line": line_no,
            "kind": "typescript_type_error",
            "detail": f"{ts_code}: {message}",
        })

    # Filter to changed_files when provided
    if changed_files is not None:
        changed_set = set(changed_files)
        raw_findings = {
            fp: flist
            for fp, flist in raw_findings.items()
            if any(fp.endswith(cf) or cf.endswith(fp) for cf in changed_set)
        }

    # Ratchet: only surface errors that exceed the baseline count per file
    saturnday_dir = repo_path / ".saturnday"
    baseline = _load_tsc_baseline(saturnday_dir)
    findings = []
    for file_path, file_findings in raw_findings.items():
        current_count = len(file_findings)
        baseline_count = baseline.get(file_path, 0)
        if current_count > baseline_count:
            findings.extend(file_findings[baseline_count:])

    # WARNING severity: use WARN (not FAIL) so this never blocks governance
    status = "WARN" if findings else "PASS"
    return {
        "name": "type_check_ts",
        "status": status,
        "severity": "warning",
        "findings": findings,
        "exit_code": result.returncode,
        "raw_output": raw_output,
        "error": None,
    }


# ---------------------------------------------------------------------------
# Public API: run all TS checks
# ---------------------------------------------------------------------------

ALL_TS_CHECKS = [
    check_secrets_ts,
    check_hallucinated_imports_ts,
    check_typosquat_ts,
    check_fake_tests_ts,
    check_prompt_injection_ts,
    check_placeholders_ts,
    check_syntax_ts,
    # Security governance TS checks
    check_hardcoded_jwt_ts,
    check_auth_bypass_ts,
    check_websocket_auth_ts,
    check_oauth_flow_ts,
    check_cookie_security_hard_ts,
    check_weak_randomness_ts,
    check_token_revocation_ts,
    check_jwt_verification_ts,
    check_csrf_state_change_ts,
    check_rate_limit_wiring_ts,
    check_cookie_security_soft_ts,
    check_token_expiry_ts,
    check_xss_check_ts,
    check_idor_check_ts,
    check_sql_injection_ts,
    check_client_trusted_logic_ts,
    check_user_enumeration_ts,
    check_rate_limit_backend_quality_ts,
    check_security_event_logging_ts,
    check_type_check_ts,
]

PASSIVE_TS_CHECKS = [
    check_secrets_ts,
    check_hallucinated_imports_ts,
    check_typosquat_ts,
    check_fake_tests_ts,
    check_prompt_injection_ts,
    check_placeholders_ts,
]


def run_all_ts_checks(
    repo_path: Path,
    changed_files: list[str] | None = None,
) -> list[dict]:
    """Run all 7 TS/JS checks and return list of result dicts."""
    results = []
    for check_fn in ALL_TS_CHECKS:
        try:
            results.append(check_fn(repo_path, changed_files))
        except Exception as exc:
            results.append(_make_result(
                check_fn.__name__.replace("check_", ""),
                [],
                error=str(exc),
                status_override="SKIPPED",
            ))
    return results


def run_passive_ts_checks(
    repo_path: Path,
    changed_files: list[str] | None = None,
) -> list[dict]:
    """Run only the passive (no-runtime-needed) TS/JS checks."""
    results = []
    for check_fn in PASSIVE_TS_CHECKS:
        try:
            results.append(check_fn(repo_path, changed_files))
        except Exception as exc:
            results.append(_make_result(
                check_fn.__name__.replace("check_", ""),
                [],
                error=str(exc),
                status_override="SKIPPED",
            ))
    return results
