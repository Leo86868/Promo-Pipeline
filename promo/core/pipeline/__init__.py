"""Pipeline orchestration subpackage.

Extracted from ``promo/cli/compile_promo.py`` in
promo-handoff-readiness Sprint 4 A-001 (narrow — ``compile_promo.py``
decomposition). See ``architecture.md`` "Module graph" for the new
producer/consumer layout.

Public surface: :func:`full_pipeline`. The submodule layout (steps,
variant_loop, bgm_voice_resolver, sidecar_writer, pipeline) is
deliberately private — tests that reach for private symbols import
them from the specific submodule path or via the re-exports retained
in ``promo.cli.compile_promo``.
"""

from promo.core.pipeline.pipeline import full_pipeline

__all__ = ["full_pipeline"]
