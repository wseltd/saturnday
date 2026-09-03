# Release-security checks package.
#
# Each module in this package exposes a single ``run_check`` function with
# the signature::
#
#     def run_check(
#         inventory: ArtefactInventory,
#         unpack_dir: Path,
#         manifest: dict | None = None,
#     ) -> ReleaseCheckResult:
#
# The orchestrator (``saturnday.release.orchestrator``) imports each check
# module dynamically by name.  Missing modules are silently recorded as
# SKIPPED, so checks can be added incrementally without breaking the pipeline.
