"""Verbatim copy of the verified legacy grading modules.

These modules are imported from ``worker.runtime.legacy_codex`` and must not
be edited except to fix a verified bug. The Phase 04 adapter wraps them
without changing their behaviour so the grading effect stays identical to
the legacy single-machine runner.

The original modules lived in ``app/`` and were copied from git history at
``6445f11^`` (the last commit before the legacy project was migrated out of
this repository). ``settings.py`` keeps its original dataclass shape so
``codex_runner.py``'s ``from .settings import Settings`` import works
unchanged; the Worker adapter overrides the handful of fields the runner
actually reads.
"""
