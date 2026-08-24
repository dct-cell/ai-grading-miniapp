"""Verified legacy grading logic behind the Worker adapter.

These modules are imported from ``worker.runtime.legacy_codex`` and must not
be edited only for verified correctness or lifecycle fixes. Process groups,
bounded log streaming and canonical page ordering have been added without
changing prompts, scoring rules or report rendering.

The original modules lived in ``app/`` and were copied from git history at
``6445f11^`` (the last commit before the legacy project was migrated out of
this repository). ``settings.py`` keeps its original dataclass shape so
``codex_runner.py``'s ``from .settings import Settings`` import works
unchanged; the Worker adapter overrides the handful of fields the runner
actually reads.
"""
