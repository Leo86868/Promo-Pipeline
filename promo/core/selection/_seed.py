"""Deterministic ``random.Random`` factory shared by Sprint 16 selectors.

Centralizes the seed → ``random.Random`` mapping so every selector in
the package picks variants the same way: a non-``None`` seed produces a
fresh, isolated PRNG (no global ``random`` state pollution); a ``None``
seed delegates to a process-global ``random.Random()`` so successive
selections naturally diverge.
"""

from __future__ import annotations

import random


def make_seeded_random(seed: int | None) -> random.Random:
    """Return a ``random.Random`` instance seeded for reproducibility.

    A non-``None`` ``seed`` produces a fresh PRNG seeded with that value
    (deterministic across processes). A ``None`` ``seed`` returns a
    fresh PRNG seeded from the OS entropy source — equivalent to
    ``random.Random()``. Callers should construct one ``Random`` per
    selector instance and reuse it across ``select()`` calls within
    that selector's lifetime.
    """
    return random.Random(seed)
