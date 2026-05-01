"""Persona YAML loader — backwards-compatible shim.

Sprint Arsenal Externalization (Commit 7) folded the actual loader
logic into ``promo.core.arsenal_loader.load_persona``; this module
re-exports it under the historical import path so existing call sites
that do ``from promo.arsenal.personas._loader import load_persona``
(or, pre-relocation, ``from promo.personas._loader import load_persona``)
continue to work.

The pre-relocation lazy-import dance against
``promo.core.script.script_generator.NarratorPersona`` is gone —
Commit 0 moved the dataclass to ``promo.core.schema``, so the cycle
that required deferring the import to function-body time has been
broken.
"""

from __future__ import annotations

from promo.core.arsenal_loader import load_persona

__all__ = ["load_persona"]
