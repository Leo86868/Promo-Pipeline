"""Persona library — operator-curated YAMLs + their loader.

Sprint Arsenal Externalization (Commit 7) relocated this package from
``promo/personas/`` to ``promo/arsenal/personas/`` so the four library-
shape "weapons" (system prompts / voices / personas / script skeletons)
all live under one ``arsenal/`` umbrella. Adding a new persona is
"drop a YAML in this directory" — no code changes required. See
``architecture.md`` "Pool conventions" + "Selector seams" for how the
selector layer discovers the YAML library.

``arsenal_loader.load_persona`` (in ``promo/core/arsenal_loader.py``)
is the canonical reader; ``promo.arsenal.personas._loader.load_persona``
re-exports it for backwards compatibility with existing call sites.
"""
