"""``saturnday start`` writes plan.json to the repo, not /tmp.

Before this fix, the interactive ``guided_run`` flow
(``saturnday start``) wrote the generated plan to a
``tempfile.mkdtemp(prefix="saturnday-plan-")`` directory.  The file
landed at ``/tmp/saturnday-plan-<random>/plan.json``, which:

* disappeared on reboot (tmp cleanup);
* was invisible to the standard ``find .saturnday -name plan.json``
  discovery flow everyone uses to locate a saturnday plan;
* made ``saturnday start`` behave inconsistently with ``saturnday
  plan`` (which writes to ``.saturnday/plan.json``) and ``saturnday
  run`` (which archives each run's plan to
  ``.saturnday/run/<timestamp>/plan.json``).

The fix writes the plan to ``{repo_path}/.saturnday/plan.json`` —
same canonical location every other command uses.

Cross-project safety: different projects live in different repos
with their own ``.saturnday/`` directories, so there is no
cross-project collision.  Within a single repo, re-running
``saturnday start`` overwrites the working plan.json, but every
prior run already has its own archived copy at
``.saturnday/run/<timestamp>/plan.json`` via ``run_plan``, so
historical plans are preserved.
"""
from __future__ import annotations

import inspect

from saturnday import interactive


# ---------------------------------------------------------------------------
# Source-level pins — cheapest way to verify without driving the full
# interactive flow end-to-end (which requires stdin, backend selection,
# coder stubs, etc.)
# ---------------------------------------------------------------------------


class TestGuidedRunPlanPath:
    def test_source_no_longer_uses_tempfile_mkdtemp_for_plan_dir(self) -> None:
        """The tempfile.mkdtemp call that wrote plans to /tmp is gone.

        We deliberately only pin the CALL (``tempfile.mkdtemp(...)``)
        rather than the string ``saturnday-plan-`` because comments and
        docstrings in the fix itself legitimately reference the old
        pre-fix pattern.
        """
        src = inspect.getsource(interactive.guided_run)
        # Strip out comments / docstrings before searching so a
        # historical mention of the old pattern doesn't false-fail.
        src_no_comments = "\n".join(
            line.split("#", 1)[0]
            for line in src.splitlines()
            if line.split("#", 1)[0].strip()
        )
        assert "tempfile.mkdtemp(" not in src_no_comments, (
            "tempfile.mkdtemp call still present in guided_run — "
            "plans will still land in /tmp"
        )

    def test_source_writes_plan_under_repo_saturnday_dir(self) -> None:
        """New path: <repo>/.saturnday/plan.json."""
        src = inspect.getsource(interactive.guided_run)
        assert "Path(repo_path) / \".saturnday\"" in src, (
            "expected guided_run to resolve the plan directory as "
            "<repo_path>/.saturnday"
        )
        assert "\"plan.json\"" in src
        # The directory is created on-demand.
        assert "mkdir(parents=True, exist_ok=True)" in src

    def test_source_prints_plan_location_for_operator(self) -> None:
        """After successful generation, the operator should be told
        where the plan landed — no more hunting."""
        src = inspect.getsource(interactive.guided_run)
        assert "Plan written to:" in src

    def test_source_no_longer_imports_tempfile_locally_in_guided_run(
        self,
    ) -> None:
        """The local ``import tempfile`` inside guided_run is removed
        as part of the fix.  Other functions in interactive.py may
        still use tempfile legitimately — this test only pins
        guided_run's local import."""
        src = inspect.getsource(interactive.guided_run)
        assert "import tempfile" not in src
