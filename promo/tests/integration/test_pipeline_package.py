"""Structural guardrails for the ``promo.core.pipeline`` subpackage.

promo-handoff-readiness Sprint 4 A-001 narrow — these regression-lock
the shape constraints from the sprint contract:

- AC1: the subpackage contains ≤6 Python modules (+ optional ``py.typed``
  marker).
- AC1(c): ``__init__.py`` stays ≤20 LOC (thin re-export, ``aigc/``
  precedent).
- AC1(b): ``full_pipeline`` is importable from the package root.
- AC2(c): every moved def (``full_pipeline``, ``_step_*``, ``_write_sidecar``,
  ``_emit_run_sidecars``, the BGM/voice resolvers, ``_filter_clips_by_ids``,
  ``analyze_clips_for_script``, ``_build_variant_selections``) is defined
  in the subpackage, not in ``promo/cli/compile_promo.py``.
- AC3: the test suite's ``from promo.cli.compile_promo import <X>``
  surface for the 8 extracted private symbols still resolves without
  ``ImportError``.
- AC4: ``@patch("promo.cli.compile_promo.*")`` decorator form is
  absent from the test suite (regression lock only — context-manager
  ``patch(...)`` form is allowed, but AC4 locks the decorator invariant
  that Sprint 16 already achieved).
- N4: no new Protocol / ABC in ``promo/core/pipeline/``; no
  ``load_dotenv()`` inside the subpackage.
"""

from __future__ import annotations

from pathlib import Path


PIPELINE_DIR = Path(__file__).resolve().parents[2] / "core" / "pipeline"
COMPILE_PROMO = Path(__file__).resolve().parents[2] / "cli" / "compile_promo.py"
TEST_COMPILE_PROMO = Path(__file__).resolve().parent / "test_compile_promo.py"


EXPECTED_MODULES = frozenset({
    "__init__.py",
    "bgm_voice_resolver.py",
    "pipeline.py",
    "sidecar_writer.py",
    "steps.py",
    "variant_loop.py",
})

SHELL_DEFS = frozenset({
    "_build_backend",
    "_build_parser",
    "main",
    "render_from_props_file",
})

MOVED_DEFS = frozenset({
    "full_pipeline",
    "_step_generate_script",
    "_step_tts_narration",
    "_step_prepare_clips",
    "_step_assign_clips",
    "_write_sidecar",
    "_emit_run_sidecars",
    "_discover_bgm_files",
    "_resolve_bgm_paths",
    "_resolve_voice_keys",
    "_variant_output_path",
    "_empty_retrieval_provenance",
    "_build_variant_tts_metrics",
    "_filter_clips_by_ids",
    "analyze_clips_for_script",
    "_build_variant_selections",
})

EXPECTED_COMPILE_PROMO_IMPORTS = frozenset({
    "full_pipeline",
    "_variant_output_path",
    "_discover_bgm_files",
    "_write_sidecar",
    "_step_tts_narration",
    "_step_assign_clips",
    "_filter_clips_by_ids",
    "_build_parser",
})


class TestSprint4SubpackageShape:
    """AC1 + AC1(c) — ``promo/core/pipeline/`` has exactly the 6 contracted
    files (+ optional ``py.typed``), ``__init__.py`` is ≤20 LOC, and
    ``full_pipeline`` imports cleanly from the package root."""

    def test_subpackage_exists_with_expected_files(self):
        assert PIPELINE_DIR.is_dir()
        py_files = {
            p.name for p in PIPELINE_DIR.iterdir()
            if p.is_file() and p.suffix == ".py"
        }
        unexpected = py_files - EXPECTED_MODULES
        missing = EXPECTED_MODULES - py_files
        assert not unexpected, (
            f"Unexpected files: {sorted(unexpected)}; N4(b) caps file count."
        )
        assert not missing, f"Missing contract files: {sorted(missing)}"

    def test_init_thin_and_package_root_import(self):
        init_loc = sum(1 for _ in (PIPELINE_DIR / "__init__.py").open())
        assert init_loc <= 20, (
            f"__init__.py is {init_loc} LOC; AC1(c) caps at 20."
        )
        from promo.core.pipeline import full_pipeline
        assert callable(full_pipeline)


class TestSprint4CompilePromoShellShape:
    """AC2 — ``compile_promo.py`` ≤400 LOC, contains only the 4 shell-level
    defs, and zero definitions of the 16 functions that moved into
    ``promo/core/pipeline/``."""

    def test_loc_ceiling_and_shell_defs(self):
        loc = sum(1 for _ in COMPILE_PROMO.open())
        assert loc <= 400, f"compile_promo.py is {loc} LOC; AC2(a) caps at 400."
        import ast
        tree = ast.parse(COMPILE_PROMO.read_text())
        defs = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
        missing = SHELL_DEFS - defs
        leaked = defs & MOVED_DEFS
        assert not missing, f"compile_promo.py missing shell defs: {sorted(missing)}"
        assert not leaked, (
            f"compile_promo.py still defines moved functions: {sorted(leaked)}"
        )


class TestSprint4ImportSurfaceCompatibility:
    """AC3 — every ``from promo.cli.compile_promo import <X>`` symbol
    the test suite depends on at master HEAD 3bb41ca still resolves
    (natively or via re-export)."""

    def test_all_expected_symbols_resolve(self):
        import promo.cli.compile_promo as cp
        missing = [s for s in EXPECTED_COMPILE_PROMO_IMPORTS if not hasattr(cp, s)]
        assert not missing, (
            f"Missing promo.cli.compile_promo symbols: {missing}"
        )


class TestSprint4PatchDecoratorRegressionLock:
    """AC4 — zero ``@patch("promo.cli.compile_promo.<X>")`` decorator
    usages. Sprint 16 already collapsed the surface; locking it so a
    future sprint doesn't re-introduce the anti-pattern."""

    def test_no_compile_promo_decorator_patches(self):
        import re
        src = TEST_COMPILE_PROMO.read_text()
        matches = re.findall(
            r'@patch\(\s*["\']promo\.cli\.compile_promo\.',
            src,
        )
        assert len(matches) == 0, (
            f"Found {len(matches)} @patch decorator(s) targeting "
            f"promo.cli.compile_promo; AC4(c) requires 0."
        )


class TestSprint4N4NoNewAbstractions:
    """N4 — no new Protocol / ABC inside ``promo/core/pipeline/`` (Sprint 1
    Amendment 3 anti-pattern), and ``load_dotenv()`` stays in
    ``compile_promo.py`` only (CLAUDE.md AIGC extensions rule)."""

    def test_no_protocol_abc_or_load_dotenv_in_pipeline(self):
        import re
        offenders: list[str] = []
        for py_path in PIPELINE_DIR.rglob("*.py"):
            text = py_path.read_text()
            if re.search(r"\bclass\s+\w+\s*\([^)]*Protocol[^)]*\)", text):
                offenders.append(f"{py_path.name}: Protocol subclass")
            if re.search(r"\bABCMeta\b|\babstractmethod\b", text):
                offenders.append(f"{py_path.name}: ABC / abstractmethod")
            if "load_dotenv" in text:
                offenders.append(f"{py_path.name}: load_dotenv call")
        assert not offenders, (
            f"Forbidden abstractions / load_dotenv in pipeline: {offenders}"
        )
