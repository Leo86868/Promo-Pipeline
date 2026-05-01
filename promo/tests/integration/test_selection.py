"""Sprint 16 — selector seam tests.

Covers AC1 (package shape), AC2 (Protocols runtime-checkable + types),
AC3 (FORMAT_TEMPLATES registry), AC4 (load_persona extraction +
behaviour byte-identical), AC5 (variant loop selector threading via
PROMO_FORMAT_SELECTOR + --seed), AC6 (BUG-1 sidecar caller-duration),
AC7 (CLI / config additions live in safe zones), AC8 (architecture.md
section + same-commit + no new doc files).
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import os
import subprocess
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
#  AC1 — Selector package exists (Shape B)
# ---------------------------------------------------------------------------


class TestSprint16AC1PackageShape:
    """`promo/core/selection/` ships the five files in the contract; the
    package re-exports the six public symbols."""

    def test_five_files_present(self):
        import promo.core.selection as sel_pkg
        pkg_dir = os.path.dirname(sel_pkg.__file__)
        for name in ("__init__.py", "protocols.py", "format_selectors.py",
                     "persona_selectors.py", "_seed.py"):
            assert os.path.isfile(os.path.join(pkg_dir, name)), (
                f"Sprint 16 AC1: missing {name} in promo/core/selection/"
            )

    def test_public_exports_match_all_set(self):
        """Stronger than an import-smoke: pin `__all__` exactly so a
        future export drift (rename, missing add, accidental removal)
        breaks this test rather than silently succeeding at import."""
        import promo.core.selection as sel_pkg
        # Sprint 16 audit-fix: SingleFormatSelector added post-Codex
        # review; the canonical export set is now seven names.
        expected = {
            "FormatSelector",
            "PersonaSelector",
            "RandomFormatSelector",
            "RandomPersonaSelector",
            "SingleFormatSelector",
            "SinglePersonaSelector",
            "make_seeded_random",
        }
        assert set(sel_pkg.__all__) == expected, (
            f"selection.__all__ drift: extra={set(sel_pkg.__all__) - expected}, "
            f"missing={expected - set(sel_pkg.__all__)}"
        )


# ---------------------------------------------------------------------------
#  AC2 — Protocols runtime-checkable, typed correctly
# ---------------------------------------------------------------------------


class TestSprint16AC2ProtocolsRuntimeCheckable:
    """Both Protocols are `@runtime_checkable`; the default
    implementations isinstance-pass against them."""

    def test_random_format_selector_is_format_selector(self):
        from promo.core.selection import FormatSelector, RandomFormatSelector
        assert isinstance(RandomFormatSelector(seed=42), FormatSelector) is True

    def test_single_persona_selector_is_persona_selector(self):
        from promo.core.selection import PersonaSelector, SinglePersonaSelector
        assert isinstance(SinglePersonaSelector(), PersonaSelector) is True

    def test_random_persona_selector_is_persona_selector(self):
        from promo.core.selection import PersonaSelector, RandomPersonaSelector
        assert isinstance(RandomPersonaSelector(seed=42), PersonaSelector) is True

    def test_select_signatures_match_contract(self):
        """`select(n_variants, *, poi_name, clip_metadata)` — keyword-only
        ``poi_name`` + ``clip_metadata`` so future smart selectors can
        consume them without breaking call sites."""
        from promo.core.selection import (
            RandomFormatSelector, SinglePersonaSelector,
        )
        for cls in (RandomFormatSelector, SinglePersonaSelector):
            sig = inspect.signature(cls.select)
            params = list(sig.parameters.values())
            # self + n_variants positional, poi_name + clip_metadata keyword-only
            assert params[1].name == "n_variants"
            assert params[2].name == "poi_name"
            assert params[2].kind is inspect.Parameter.KEYWORD_ONLY
            assert params[3].name == "clip_metadata"
            assert params[3].kind is inspect.Parameter.KEYWORD_ONLY


# ---------------------------------------------------------------------------
#  AC3 — FORMAT_TEMPLATES registry
# ---------------------------------------------------------------------------


class TestSprint16AC3FormatTemplatesRegistry:
    """`FORMAT_TEMPLATES` exists in `format_profiles.py` and the random
    selector samples from it (not a hardcoded tuple)."""

    def test_format_templates_dict_exact_contents(self):
        from promo.core.format_profiles import (
            FORMAT_TEMPLATES, SHORT_PROFILE, LONG_PROFILE,
        )
        assert FORMAT_TEMPLATES == {"short": SHORT_PROFILE, "long": LONG_PROFILE}

    def test_random_format_selector_references_format_templates(self):
        """Source-grep confirms the selector pulls from the registry,
        not a private hardcoded tuple."""
        from promo.core.selection import format_selectors
        source = inspect.getsource(format_selectors)
        assert "FORMAT_TEMPLATES" in source, (
            "Sprint 16 AC3: RandomFormatSelector must reference "
            "FORMAT_TEMPLATES, not a hardcoded tuple"
        )

    def test_random_format_selector_yields_only_registered_modes(self):
        """50-draw fuzz: every output mode must be a key in the registry."""
        from promo.core.format_profiles import FORMAT_TEMPLATES
        from promo.core.selection import RandomFormatSelector
        sel = RandomFormatSelector(seed=12345)
        for _ in range(50):
            out = sel.select(1, poi_name="X", clip_metadata=[])
            assert len(out) == 1
            assert out[0].mode in FORMAT_TEMPLATES


# ---------------------------------------------------------------------------
#  AC4 — load_persona extracted; behaviour byte-identical
# ---------------------------------------------------------------------------


class TestSprint16AC4LoadPersonaExtraction:
    """``promo/arsenal/personas/_loader.py`` exposes ``load_persona``
    (Commit 7 relocation; pre-Sprint-Arsenal it lived in
    ``promo/personas/_loader.py``); the function definition is no
    longer in ``script_generator.py``; loading the bundled YAML
    produces the same field-by-field NarratorPersona."""

    def test_no_def_load_persona_in_script_generator(self):
        from promo.core.script import script_generator
        source = inspect.getsource(script_generator)
        # Verify clause: `grep -n "^def load_persona"` returns 0 lines.
        for line in source.splitlines():
            assert not line.startswith("def load_persona"), (
                "Sprint 16 AC4: `def load_persona` must NOT appear at "
                "module scope in promo/core/script_generator.py"
            )

    def test_script_generator_imports_load_persona_from_loader(self):
        """``from promo.arsenal.personas._loader import load_persona``
        is in the script_generator import block."""
        from promo.core.script import script_generator
        source = inspect.getsource(script_generator)
        assert (
            "from promo.arsenal.personas._loader import load_persona" in source
        ), (
            "Sprint Arsenal Externalization Commit 7: script_generator.py "
            "must import load_persona from promo.arsenal.personas._loader"
        )

    def test_loaded_persona_field_identity(self):
        """Field-by-field equality between the new loader path and a
        reference NarratorPersona built from the YAML file directly."""
        import yaml
        from promo.arsenal.personas._loader import load_persona
        from promo.core.script.script_generator import NarratorPersona

        # Resolve the YAML path through the relocated arsenal package.
        import promo.arsenal.personas as personas_pkg
        yaml_path = os.path.join(
            os.path.dirname(personas_pkg.__file__),
            "third_person_promo.yaml",
        )

        loaded = load_persona(yaml_path)
        with open(yaml_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        known = set(NarratorPersona.__dataclass_fields__)
        reference = NarratorPersona(
            **{k: v for k, v in data.items() if k in known}
        )
        assert dataclasses.asdict(loaded) == dataclasses.asdict(reference)


# ---------------------------------------------------------------------------
#  AC5 — Variant loop uses selectors per-variant
# ---------------------------------------------------------------------------


class TestSprint16AC5VariantLoopSelectors:
    """`compile_promo._build_variant_selections` emits one profile per
    variant, the variant loop threads `formats[i]` / `personas[i]`
    forward, and the format selector is config-resolved with a clear
    error on unknown values."""

    def test_random_selector_n3_seed7_yields_exact_sequence(self):
        """Builder-pinned seed 7 — the live-evidence template in
        sprint-16-reflection.md hardcodes this exact sequence so the
        operator receipt can assert against ffprobe outputs without
        guessing. If RandomFormatSelector's RNG implementation drifts
        (e.g. someone adds shuffling, changes sort order of the keys
        tuple, or swaps random.choice for random.choices), this test
        fails LOUDLY before the operator's evidence run goes stale."""
        from promo.cli.compile_promo import _build_variant_selections

        with patch.dict(os.environ, {"PROMO_FORMAT_SELECTOR": "random"},
                        clear=False):
            profiles, personas = _build_variant_selections(
                n_variants=3, poi_name="Test POI",
                clips_metadata=[{"id": "0001"}], seed=7,
            )
        modes = [p.mode for p in profiles]
        assert modes == ["short", "long", "short"], (
            f"seed=7 sequence drift — reflection template expects "
            f"['short', 'long', 'short'], got {modes}. Either a "
            f"selector change broke determinism OR the reflection's "
            f"evidence template needs to follow the new sequence."
        )
        # Persona side ships single-persona by default → one repeated id.
        assert len({p.id for p in personas}) == 1

    def test_formats_threaded_into_loop_have_correct_length_and_type(self):
        from promo.core.format_profiles import PromoFormatProfile
        from promo.cli.compile_promo import _build_variant_selections

        with patch.dict(os.environ, {"PROMO_FORMAT_SELECTOR": "random"},
                        clear=False):
            profiles, personas = _build_variant_selections(
                n_variants=4, poi_name="Test POI",
                clips_metadata=[{"id": "0001"}], seed=42,
            )
        assert isinstance(profiles, list)
        assert len(profiles) == 4
        assert all(isinstance(p, PromoFormatProfile) for p in profiles)

    def test_unknown_format_selector_raises_config_error(self):
        from promo.core.config import ConfigError
        from promo.cli.compile_promo import _build_variant_selections

        with patch.dict(os.environ, {"PROMO_FORMAT_SELECTOR": "xxx-no-such"},
                        clear=False):
            with pytest.raises(ConfigError, match="PROMO_FORMAT_SELECTOR"):
                _build_variant_selections(
                    n_variants=1, poi_name="X",
                    clips_metadata=[{"id": "0001"}], seed=0,
                )


# ---------------------------------------------------------------------------
#  AC6 — BUG-1: sidecar records caller-provided duration
# ---------------------------------------------------------------------------


class _FakeGenOne:
    """Returns a deterministic minimal raw script for both modes; bypasses
    Gemini #1 by feeding `_generate_one`. Honours the per-variant profile
    by emitting a script that satisfies the profile's segment count and
    word counts."""

    def __init__(self):
        self.calls = 0

    def __call__(self, prompt, model):
        # The validator checks segment count + per-segment word ranges,
        # not text content. Emit minimum-passable scripts per mode.
        self.calls += 1
        return None  # placeholder; tests below patch `generate_script_variants` directly


def _bypass_gen_for_profile(profile):
    """Build a raw-script payload that passes normalize_script /
    validate_script_only / pacing for the given profile."""
    # One word per "approx_words" per segment, padded to per_segment_min.
    segments = []
    for i, sp in enumerate(profile.segment_plans, 1):
        wcount = max(sp.approx_words, profile.per_segment_min)
        text = " ".join([f"w{i}{j}" for j in range(wcount)])
        segments.append({
            "segment": i,
            "text": text,
            "pause_weight": 1,
        })
    return {
        "segments": segments,
        "total_words": sum(len(s["text"].split()) for s in segments),
        "total_clips": 0,
    }


class TestSprint16AC6Bug1CallerDuration:
    """The sidecar `target_duration_sec` field reflects the caller's
    intent, not `profile.target_duration_sec` (the canonical 30/65)."""

    def _patched_call(self, *, target_duration_sec, profile=None,
                      profiles=None, n_variants=1, persona_path=None):
        """Common machinery: stub Gemini #1 + validators + persona, run
        generate_script_variants under the kwargs the test cares about."""
        from promo.core.format_profiles import SHORT_PROFILE, LONG_PROFILE
        from promo.core.script.script_generator import (
            generate_script_variants, NarratorPersona,
        )

        # Stub persona so load_persona doesn't touch disk in the singular path.
        stub_persona = NarratorPersona(
            id="stub", display_name="Stub", perspective="third_person",
            wpm=180, voice_id="test", system_prompt="",
            tone_keywords=[], forbidden_phrases=[], forbidden_openers=[],
            example_scripts=[], pause_guidelines="", gemini={},
        )

        # Build per-variant raw scripts — must differ to defeat seen_texts dedup.
        if profiles is not None:
            profile_iter = list(profiles)
        elif profile is not None:
            profile_iter = [profile] * n_variants
        else:
            from promo.core.format_profiles import get_promo_format_profile
            profile_iter = [get_promo_format_profile(target_duration_sec)] * n_variants
        raws = []
        for idx, p in enumerate(profile_iter):
            raw = _bypass_gen_for_profile(p)
            # tweak first segment to differentiate variants
            raw["segments"][0]["text"] = raw["segments"][0]["text"] + f" salt{idx}"
            raws.append(raw)

        with patch("promo.core.script.script_generator.resolve_gemini_model",
                   return_value=object()), \
             patch("promo.core.script.script_generator._generate_one",
                   side_effect=raws), \
             patch("promo.core.script.script_generator._build_prompt",
                   return_value="prompt"), \
             patch("promo.core.script.script_validator.normalize_script",
                   return_value=None), \
             patch("promo.core.script.script_validator.validate_script_only",
                   return_value=None), \
             patch("promo.core.script.script_generator._enforce_pacing_gate",
                   return_value=None):
            kwargs = dict(
                poi_name="X", location="",
                clips_metadata=[
                    {"id": f"{i:04d}", "category": "scenic",
                     "scene_description": "clip"} for i in range(1, 21)
                ],
                hotel_description="", notable_details="",
                n_variants=n_variants, n_candidates=1, max_retries=0,
                target_duration_sec=target_duration_sec,
                persona=stub_persona,
            )
            if profile is not None:
                kwargs["profile"] = profile
            if profiles is not None:
                kwargs["profiles"] = profiles
            return generate_script_variants(**kwargs)

    def test_short_profile_with_caller_duration_45(self):
        from promo.core.format_profiles import SHORT_PROFILE
        scripts = self._patched_call(
            target_duration_sec=45.0, profile=SHORT_PROFILE, n_variants=1,
        )
        assert scripts[0]["target_duration_sec"] == 45.0

    def test_long_profile_with_caller_duration_65(self):
        from promo.core.format_profiles import LONG_PROFILE
        scripts = self._patched_call(
            target_duration_sec=65.0, profile=LONG_PROFILE, n_variants=1,
        )
        assert scripts[0]["target_duration_sec"] == 65.0

    def test_mixed_per_variant_durations(self):
        """Selector-driven case: each variant's sidecar matches the
        per-variant profile.target_duration_sec."""
        from promo.core.format_profiles import SHORT_PROFILE, LONG_PROFILE
        scripts = self._patched_call(
            target_duration_sec=None,
            profiles=[SHORT_PROFILE, LONG_PROFILE, SHORT_PROFILE],
            n_variants=3,
        )
        durations = [s["target_duration_sec"] for s in scripts]
        assert durations == [30, 65, 30]


# ---------------------------------------------------------------------------
#  AC7 — CLI + config additions in safe zones
# ---------------------------------------------------------------------------


class TestSprint16AC7SafeZones:
    """`--seed` flag is registered; lines 1290-1306 of base SHA are
    untouched (covered structurally by N3); config.py only gets
    appended additions."""

    def test_seed_in_compile_promo_help(self):
        out = subprocess.check_output(
            ["python3", "-m", "promo.cli.compile_promo", "--help"],
            stderr=subprocess.STDOUT,
        ).decode()
        assert "--seed" in out, (
            "Sprint 16 AC7: --seed CLI flag must surface in --help"
        )

    def test_promo_format_selector_resolver_default_single(self):
        """Sprint 16 audit-fix HIGH-1: default is `single` to preserve the
        pre-Sprint-16 operator contract (`--target-duration-sec X` pins all
        variants). Random is opt-in via PROMO_FORMAT_SELECTOR=random."""
        from promo.core.config import promo_format_selector
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PROMO_FORMAT_SELECTOR", None)
            assert promo_format_selector() == "single"

    def test_promo_format_selector_resolver_accepts_random_optin(self):
        from promo.core.config import promo_format_selector
        with patch.dict(os.environ,
                        {"PROMO_FORMAT_SELECTOR": "random"}, clear=False):
            assert promo_format_selector() == "random"

    def test_promo_format_selector_resolver_rejects_unknown(self):
        from promo.core.config import promo_format_selector, ConfigError
        with patch.dict(os.environ,
                        {"PROMO_FORMAT_SELECTOR": "smart"}, clear=False):
            with pytest.raises(ConfigError, match="PROMO_FORMAT_SELECTOR"):
                promo_format_selector()


# ---------------------------------------------------------------------------
#  Sprint 16 post-Codex-review fix — SingleFormatSelector + default=single
#  (restores the --target-duration-sec contract; HIGH-1 from Codex review)
# ---------------------------------------------------------------------------


class TestSprint16SingleFormatSelectorDefault:
    """The default `single` selector pins every variant to the profile
    derived from `target_duration_sec`, restoring the pre-Sprint-16
    operator contract that `--target-duration-sec X` produces an
    X-second video for every variant."""

    def test_single_selector_pins_all_variants_to_short(self):
        from promo.core.format_profiles import SHORT_PROFILE
        from promo.core.selection import SingleFormatSelector
        sel = SingleFormatSelector(target_duration_sec=30)
        out = sel.select(3, poi_name="X", clip_metadata=[])
        assert all(p is SHORT_PROFILE for p in out)

    def test_single_selector_pins_all_variants_to_long(self):
        from promo.core.format_profiles import LONG_PROFILE
        from promo.core.selection import SingleFormatSelector
        sel = SingleFormatSelector(target_duration_sec=65)
        out = sel.select(3, poi_name="X", clip_metadata=[])
        assert all(p is LONG_PROFILE for p in out)

    def test_default_build_variant_selections_pins_to_target(self):
        """When PROMO_FORMAT_SELECTOR is unset (default `single`) and
        target_duration_sec=30 is passed, all variants are short."""
        from promo.cli.compile_promo import _build_variant_selections
        env = {k: v for k, v in os.environ.items() if k != "PROMO_FORMAT_SELECTOR"}
        with patch.dict(os.environ, env, clear=True):
            profiles, _ = _build_variant_selections(
                n_variants=3, poi_name="X",
                clips_metadata=[{"id": "0001"}], seed=7,
                target_duration_sec=30,
            )
        assert [p.mode for p in profiles] == ["short", "short", "short"]


# ---------------------------------------------------------------------------
#  Sprint 16 post-Codex round-4 fix — preflight uses the operator's
#  --target-duration-sec, not the worst-case long profile. This catches
#  the regression where a 30s short request against an 8-13 clip POI
#  was rejected at preflight by an over-strict 14-clip-min check.
# ---------------------------------------------------------------------------


class TestSprint16PreflightHonorsTargetDuration:
    """Preflight (`_step_prepare_clips`) must reject only when the pool
    is below the requested-profile floor. A short request against a
    pool that meets short's floor (8) but is below long's floor (14)
    must NOT abort early — that would silently break the live-service
    path on any POI material/<slug>/clips/ with 8-13 clips."""

    def _stub_backend(self, n_clips):
        backend = MagicMock()
        backend.fetch_clips.return_value = {
            f"{i:04d}": f"/tmp/clip_{i:04d}.mp4" for i in range(1, n_clips + 1)
        }
        backend.clips_dir.return_value = None
        return backend

    def test_short_target_passes_with_11_clips(self, tmp_path):
        """11 clips ≥ short min 8 → preflight passes for target=30."""
        from promo.cli.compile_promo import _step_prepare_clips
        backend = self._stub_backend(11)
        with patch("promo.core.pipeline.steps.analyze_clips_for_script",
                   return_value=[{"id": f"{i:04d}"} for i in range(1, 12)]), \
             patch("promo.core.render.remotion_renderer.get_clip_duration",
                   return_value=5.0):
            result = _step_prepare_clips(
                backend=backend, poi_name="Test POI",
                tmp_dir=str(tmp_path), target_duration_sec=30,
                skip_analysis=True,
            )
        assert result is not None, (
            "Sprint 16 audit-fix: preflight must NOT reject 11 clips for "
            "a 30s target — short profile only requires 8 clips. The "
            "earlier 'preflight against worst case' logic broke hotel-y "
            "(11 clips) against the live-service path."
        )

    def test_long_target_rejects_below_long_floor(self, tmp_path):
        """11 clips < long min 14 → preflight rejects for target=65."""
        from promo.cli.compile_promo import _step_prepare_clips
        backend = self._stub_backend(11)
        result = _step_prepare_clips(
            backend=backend, poi_name="Test POI",
            tmp_dir=str(tmp_path), target_duration_sec=65,
            skip_analysis=True,
        )
        assert result is None, (
            "Long profile requires 14 clips; 11 should fail preflight."
        )


# ---------------------------------------------------------------------------
#  Sprint 16 audit — WPM calibration cross-mode isolation regression test
#  (covers logic-auditor L-001 / data-integrity-auditor D-001 / D-002 fixes)
# ---------------------------------------------------------------------------


class TestSprint16WpmCalibrationCrossModeIsolation:
    """When a mixed-mode sidecar contains both 30s and 65s WPM entries,
    `load_calibrated_wpm` must filter by per-entry `target_duration_sec`
    so the next 30s run does not blend long-form WPM into its calibration.
    """

    def test_filter_drops_other_duration_entries(self, tmp_path):
        import json
        from promo.core.script.pause_budget import load_calibrated_wpm

        sidecar = tmp_path / "tts_metrics_test_poi_30s.json"
        sidecar.write_text(json.dumps([
            {"variant_index": 1, "measured_wpm": 160.0,
             "target_duration_sec": 30, "backend": "elevenlabs"},
            {"variant_index": 2, "measured_wpm": 200.0,
             "target_duration_sec": 65, "backend": "elevenlabs"},
            {"variant_index": 3, "measured_wpm": 158.0,
             "target_duration_sec": 30, "backend": "elevenlabs"},
        ]))
        wpm_30 = load_calibrated_wpm(
            "test_poi", 30, [str(tmp_path)], backend="elevenlabs",
        )
        # Only the two 30s entries (160 + 158) / 2 = 159
        assert wpm_30 == 159, (
            "Sprint 16 audit fix: 65s entry must be filtered out of 30s calibration; "
            f"got {wpm_30!r}"
        )

    def test_filter_preserves_pre_sprint_16_back_compat(self, tmp_path):
        """Entries lacking `target_duration_sec` (pre-Sprint-16 sidecars)
        are still counted — same back-compat posture as the `backend`
        filter in `load_calibrated_wpm`."""
        import json
        from promo.core.script.pause_budget import load_calibrated_wpm

        sidecar = tmp_path / "tts_metrics_test_poi_30s.json"
        sidecar.write_text(json.dumps([
            {"variant_index": 1, "measured_wpm": 150.0, "backend": "elevenlabs"},
            {"variant_index": 2, "measured_wpm": 152.0, "backend": "elevenlabs"},
        ]))
        wpm = load_calibrated_wpm(
            "test_poi", 30, [str(tmp_path)], backend="elevenlabs",
        )
        assert wpm == 151


# ---------------------------------------------------------------------------
#  AC8 — architecture.md updated; no new doc files
# ---------------------------------------------------------------------------


class TestSprint16AC8ArchitectureDoc:
    """`architecture.md` gains the Sprint 16 selector-seam section
    naming the four cold-reader entry points."""

    def _arch_path(self):
        # Walk up from this test file to the repo root holding architecture.md.
        here = os.path.dirname(os.path.abspath(__file__))
        cur = here
        for _ in range(6):
            candidate = os.path.join(cur, "architecture.md")
            if os.path.isfile(candidate):
                return candidate
            cur = os.path.dirname(cur)
        raise AssertionError("architecture.md not found above test file")

    def test_section_present_with_four_entry_points(self):
        with open(self._arch_path(), "r", encoding="utf-8") as f:
            text = f.read()
        assert "## Selector seams" in text
        # The four entry points named in the contract. Sprint Arsenal
        # Externalization (Commit 7) relocated personas; the path moved
        # from `promo/personas/*.yaml` → `promo/arsenal/personas/*.yaml`.
        assert "promo/core/format_profiles.py::FORMAT_TEMPLATES" in text
        assert "promo/arsenal/personas/*.yaml" in text
        assert "promo/core/selection/__init__.py" in text
        assert "promo/core/config.py::promo_format_selector" in text

    def test_no_new_doc_files_under_docs(self):
        # Find repo root + check no SELECTION/SELECTOR files were added.
        here = os.path.dirname(os.path.abspath(__file__))
        cur = here
        for _ in range(6):
            docs_dir = os.path.join(cur, "docs")
            if os.path.isdir(docs_dir):
                for root, _dirs, files in os.walk(docs_dir):
                    for f in files:
                        upper = f.upper()
                        assert not upper.startswith("SELECTION"), (
                            f"Sprint 16 AC8: unexpected new doc {f}"
                        )
                        assert not upper.startswith("SELECTOR"), (
                            f"Sprint 16 AC8: unexpected new doc {f}"
                        )
                return
            cur = os.path.dirname(cur)
        # No docs dir is also fine; AC8 only forbids new SELECTION* / SELECTOR* files.
