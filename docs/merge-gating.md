# Merge Gating with Saturnday Guard

Saturnday Guard provides a composite GitHub Action that runs governance checks
on every pull request diff and blocks merge when the disposition is FAIL.

## What it does

1. Installs the `saturnday` package in the runner.
2. Runs `saturnday check --repo . --diff <base>..HEAD --strict` against the PR diff.
3. Fails the check step (exit code 1) if disposition is FAIL.
4. Optionally posts a PR comment with the failure summary when the `gh` CLI and
   `GITHUB_TOKEN` are available.

When the step is added to your repository's **required status checks**, GitHub
will block the merge button until the check passes.

## Setup

### Step 1 — add the workflow

Create `.github/workflows/saturnday-guard.yml` in your repository:

```yaml
name: Saturnday Guard

on:
  pull_request:
    branches: ["main", "master"]

permissions:
  contents: read
  pull-requests: write   # required for PR comment posting

jobs:
  guard:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0    # needed for accurate diff range

      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Run Saturnday Guard
        uses: saturnday/saturnday/.github/actions/saturnday-guard@main
        with:
          repo-path: "."
          post-comment: "true"
```

### Step 2 — protect the branch

1. Go to **Settings > Branches** in your repository.
2. Add or edit the branch protection rule for `main` (or `master`).
3. Enable **Require status checks to pass before merging**.
4. Search for and select `guard` (the job name from your workflow).
5. Save the rule.

From this point on, any PR whose diff triggers a governance FAIL will be blocked
from merging until the author resolves the findings.

## Inputs

| Input | Default | Description |
|-------|---------|-------------|
| `repo-path` | `.` | Path to the git repository to check |
| `diff-range` | PR base..HEAD | Git diff range; auto-resolved from PR context |
| `policy` | _(built-in)_ | Path to a custom `saturnday-policy.yaml` |
| `post-comment` | `true` | Post PR comment with findings on FAIL |
| `saturnday-version` | _(latest)_ | Pin a specific `saturnday` package version |

## Outputs

| Output | Description |
|--------|-------------|
| `disposition` | `PASS` or `FAIL` |
| `evidence-path` | Path to the evidence pack directory on the runner |

## Custom policy

To use a repository-level policy file:

```yaml
      - name: Run Saturnday Guard
        uses: saturnday/saturnday/.github/actions/saturnday-guard@main
        with:
          policy: ".github/saturnday-policy.yaml"
```

## Pinning the version

For production use, pin to a specific version to avoid unexpected behaviour:

```yaml
      - name: Run Saturnday Guard
        uses: saturnday/saturnday/.github/actions/saturnday-guard@main
        with:
          saturnday-version: "0.9.0"
```

## Local reproduction

When a PR is blocked, developers can reproduce locally with:

```bash
git fetch origin main
saturnday check --repo . --diff origin/main..HEAD --strict
```
