"""Unit tests for promo.core.analyze.clip_analyzer."""

import json
import os
import re
import shutil
import sys
import tempfile
from unittest.mock import patch, MagicMock

from pathlib import Path

import pytest

class TestP9MktempSecurity:
    """P9 fix: clip_analyzer should not use insecure tempfile.mktemp."""

    def test_no_mktemp_usage(self):
        """_compress_video should use NamedTemporaryFile, not mktemp."""
        from promo.core.analyze import clip_analyzer
        import inspect
        source = inspect.getsource(clip_analyzer._compress_video)
        assert "mktemp" not in source, (
            "_compress_video still uses insecure tempfile.mktemp"
        )

class TestSprint08ClipCache:
    """Per-clip MiMo cache sidecars: blake2b content hash, atomic writes."""

    def test_clip_cache_key_stable(self, tmp_path):
        from promo.core.analyze.clip_analyzer import clip_cache_key
        p = tmp_path / "clip.mp4"
        p.write_bytes(b"\x00\x01\x02" + b"x" * 1000)
        k1 = clip_cache_key(str(p))
        k2 = clip_cache_key(str(p))
        assert k1 == k2
        assert len(k1) == 32  # blake2b 16-byte digest → 32 hex chars

    def test_clip_cache_key_differs_for_different_content(self, tmp_path):
        from promo.core.analyze.clip_analyzer import clip_cache_key
        a = tmp_path / "a.mp4"
        b = tmp_path / "b.mp4"
        a.write_bytes(b"aaa" + b"1" * 1000)
        b.write_bytes(b"bbb" + b"2" * 1000)
        assert clip_cache_key(str(a)) != clip_cache_key(str(b))

    def test_cache_round_trip(self, tmp_path):
        from promo.core.analyze.clip_analyzer import (
            _load_cached_analysis, _save_cached_analysis,
        )
        cache_dir = str(tmp_path / ".mimo_cache")
        key = "deadbeef"
        assert _load_cached_analysis(cache_dir, key) is None
        _save_cached_analysis(cache_dir, key, {"scene_description": "test", "category": "pool"})
        loaded = _load_cached_analysis(cache_dir, key)
        assert loaded == {"scene_description": "test", "category": "pool"}
        # Verify no .tmp leftovers.
        import os
        tmps = [f for f in os.listdir(cache_dir) if f.endswith(".tmp")]
        assert tmps == []

    def test_analyze_single_clip_cache_hit_skips_openrouter(self, tmp_path):
        from promo.core.analyze import clip_analyzer
        from unittest.mock import patch

        clip = tmp_path / "clip.mp4"
        clip.write_bytes(b"content")
        cache_dir = str(tmp_path / ".mimo_cache")
        cached = {"scene_description": "from-cache", "category": "pool",
                  "camera_motion": "static", "dominant_motion_phase": "middle"}
        # Sprint 09b C3 (Codex #4): cache key now includes prompt+model
        # version suffix. Pre-save under the full versioned key so the
        # cache hit matches analyze_single_clip's lookup.
        resolved_model = clip_analyzer.DEFAULT_MODEL
        key = clip_analyzer.clip_cache_key(
            str(clip),
            prompt=clip_analyzer._ANALYSIS_PROMPT,
            model=resolved_model,
        )
        clip_analyzer._save_cached_analysis(cache_dir, key, cached)

        with patch.object(clip_analyzer, "_call_openrouter") as mock_call:
            result = clip_analyzer.analyze_single_clip(
                str(clip), "0001", cache_dir=cache_dir,
            )
        mock_call.assert_not_called()
        assert result["scene_description"] == "from-cache"

class TestSprint09bC3VersionedCacheKey:
    """Sprint 09b C3 (Codex #4, AC10): clip_cache_key mixes prompt + model
    into an 8-hex sha1 suffix, so prompt or model changes invalidate the
    cache automatically.
    """

    def test_different_prompts_yield_different_keys(self, tmp_path):
        from promo.core.analyze.clip_analyzer import clip_cache_key
        clip = tmp_path / "clip.mp4"
        clip.write_bytes(b"identical-bytes" * 100)
        k1 = clip_cache_key(str(clip), prompt="prompt-A", model="model-X")
        k2 = clip_cache_key(str(clip), prompt="prompt-B", model="model-X")
        assert k1 != k2
        # Same content_hash base is shared.
        assert k1.split("-")[0] == k2.split("-")[0]

    def test_different_models_yield_different_keys(self, tmp_path):
        from promo.core.analyze.clip_analyzer import clip_cache_key
        clip = tmp_path / "clip.mp4"
        clip.write_bytes(b"identical-bytes" * 100)
        k1 = clip_cache_key(str(clip), prompt="prompt-A", model="model-X")
        k2 = clip_cache_key(str(clip), prompt="prompt-A", model="model-Y")
        assert k1 != k2

    def test_same_inputs_yield_same_key(self, tmp_path):
        from promo.core.analyze.clip_analyzer import clip_cache_key
        clip = tmp_path / "clip.mp4"
        clip.write_bytes(b"identical-bytes" * 100)
        k1 = clip_cache_key(str(clip), prompt="prompt-A", model="model-X")
        k2 = clip_cache_key(str(clip), prompt="prompt-A", model="model-X")
        assert k1 == k2

    def test_version_suffix_is_8_hex(self, tmp_path):
        from promo.core.analyze.clip_analyzer import clip_cache_key
        clip = tmp_path / "clip.mp4"
        clip.write_bytes(b"x" * 100)
        key = clip_cache_key(str(clip), prompt="p", model="m")
        parts = key.split("-")
        assert len(parts) == 2
        assert len(parts[0]) == 32  # blake2b16 content hash
        assert len(parts[1]) == 8   # sha1[:8] suffix
        # Suffix must be valid hex.
        int(parts[1], 16)

    def test_legacy_signature_returns_bare_content_hash(self, tmp_path):
        """Backward compat: clip_cache_key(path) still returns content_hash
        only (no suffix) so the Sprint 08 test suite keeps passing."""
        from promo.core.analyze.clip_analyzer import clip_cache_key
        clip = tmp_path / "clip.mp4"
        clip.write_bytes(b"x" * 100)
        key = clip_cache_key(str(clip))
        assert "-" not in key
        assert len(key) == 32

    def test_prompt_change_invalidates_cache_on_next_run(self, tmp_path):
        """End-to-end: saving under prompt-A's key and then looking up
        under prompt-B's key misses, forcing a fresh analysis call."""
        from promo.core.analyze import clip_analyzer
        from unittest.mock import patch

        clip = tmp_path / "clip.mp4"
        clip.write_bytes(b"content")
        cache_dir = str(tmp_path / ".mimo_cache")

        # Step 1: save under "prompt-A"
        key_a = clip_analyzer.clip_cache_key(
            str(clip), prompt="prompt-A", model=clip_analyzer.DEFAULT_MODEL,
        )
        clip_analyzer._save_cached_analysis(
            cache_dir, key_a,
            {"scene_description": "from-cache", "category": "pool",
             "camera_motion": "static", "dominant_motion_phase": "middle"},
        )

        # Step 2: run analyze_single_clip with the REAL (changed) prompt —
        # that key is different, so cache misses and OpenRouter IS called.
        with patch.object(clip_analyzer, "_call_openrouter") as mock_call, \
             patch.object(clip_analyzer, "_parse_response",
                          return_value={"scene_description": "fresh",
                                        "category": "pool",
                                        "camera_motion": "static",
                                        "dominant_motion_phase": "middle"}):
            mock_call.return_value = {}
            result = clip_analyzer.analyze_single_clip(
                str(clip), "0001", cache_dir=cache_dir,
            )
        mock_call.assert_called_once()
        assert result["scene_description"] == "fresh"

class TestSprint09bC4MimoAnalysisRaises:
    """Sprint 09b C4 (Codex #6, ACs 14-18): analyze_single_clip raises
    MimoAnalysisError after retry budget exhaustion; analyze_clips
    propagates; retry budget bumped 1 -> 3; main() surfaces the error
    as a user-facing exit.
    """

    def test_mimo_analysis_error_exists_and_carries_clip_id(self):
        from promo.core.errors import MimoAnalysisError
        err = MimoAnalysisError(clip_id="0008", clip_path="/tmp/a.mp4",
                                cause=RuntimeError("timeout"))
        assert err.clip_id == "0008"
        assert err.clip_path == "/tmp/a.mp4"
        assert "0008" in str(err)
        assert "timeout" in str(err)

    def test_single_clip_raises_on_persistent_failure(self, tmp_path):
        from promo.core.analyze import clip_analyzer
        from promo.core.errors import MimoAnalysisError
        from unittest.mock import patch
        import pytest

        clip = tmp_path / "clip.mp4"
        clip.write_bytes(b"bytes")
        with patch.object(
            clip_analyzer, "_call_openrouter",
            side_effect=RuntimeError("openrouter 500"),
        ):
            with pytest.raises(MimoAnalysisError) as exc_info:
                clip_analyzer.analyze_single_clip(
                    str(clip), "0042", cache_dir=None,
                )
        assert exc_info.value.clip_id == "0042"
        assert str(clip) in str(exc_info.value)

    def test_analyze_clips_propagates_first_failure(self, tmp_path):
        from promo.core.analyze import clip_analyzer
        from promo.core.errors import MimoAnalysisError
        from unittest.mock import patch
        import pytest

        good = tmp_path / "good.mp4"
        good.write_bytes(b"good")
        bad = tmp_path / "bad.mp4"
        bad.write_bytes(b"bad")

        def fake(clip_path, clip_id, model=None, cache_dir=None):
            if clip_id == "0099":
                raise MimoAnalysisError(
                    clip_id=clip_id, clip_path=str(clip_path),
                    cause=RuntimeError("bad clip"),
                )
            return {"scene_description": "ok", "category": "pool",
                    "camera_motion": "static", "dominant_motion_phase": "middle"}

        with patch.object(clip_analyzer, "analyze_single_clip", side_effect=fake):
            with pytest.raises(MimoAnalysisError) as exc_info:
                clip_analyzer.analyze_clips(
                    {"0001": str(good), "0099": str(bad)},
                    max_concurrent=1,
                )
        assert exc_info.value.clip_id == "0099"

    def test_analyze_clips_no_longer_returns_stub_entries(self, tmp_path):
        """Regression guard: the 'analysis failed' stub substitute is gone."""
        import inspect
        from promo.core.analyze import clip_analyzer
        src = inspect.getsource(clip_analyzer.analyze_clips)
        assert '"analysis failed"' not in src

    def test_retry_budget_is_three(self):
        """AC17: max_retries bumped 1 -> 3 in the same commit as the raise."""
        import inspect
        from promo.core.analyze import clip_analyzer
        src = inspect.getsource(clip_analyzer.analyze_single_clip)
        assert "max_retries=3" in src
        # The old floor must be gone.
        assert "max_retries=1" not in src

    def test_main_catches_mimo_analysis_error(self):
        """AC18: main() catches MimoAnalysisError and surfaces a
        user-facing log line rather than a raw traceback."""
        import inspect
        from promo.cli import compile_promo
        src = inspect.getsource(compile_promo.main)
        assert "MimoAnalysisError" in src
        # Exit non-zero on the catch.
        assert "sys.exit(2)" in src or "sys.exit(1)" in src

class TestSprint09bC3LuxuryBiasPrompt:
    """Sprint 09b C3 (AC11): _ANALYSIS_PROMPT contains the luxury-bias
    grounding rule — describe only what is literally visible."""

    def test_prompt_has_grounding_rule(self):
        from promo.core.analyze.clip_analyzer import _ANALYSIS_PROMPT
        text = _ANALYSIS_PROMPT.lower()
        # Core concepts: "literally visible" (or equivalent), no-assume
        assert "visible" in text
        # Explicit luxury-vocabulary guardrails.
        assert "infinity pool" in text or "infinity" in text
        # Anti-inference language somewhere.
        assert ("do not infer" in text or "do not assume" in text
                or "not clearly shown" in text or "unambiguously" in text)

    def test_prompt_still_requests_json(self):
        """Regression guard: the prompt must still request JSON output
        with the required keys. The luxury-bias fix is additive."""
        from promo.core.analyze.clip_analyzer import _ANALYSIS_PROMPT
        assert "scene_description" in _ANALYSIS_PROMPT
        assert "category" in _ANALYSIS_PROMPT
        assert "camera_motion" in _ANALYSIS_PROMPT
        assert "dominant_motion_phase" in _ANALYSIS_PROMPT
        assert "JSON" in _ANALYSIS_PROMPT

class TestSprint09bC8AuditFixes:
    """Sprint 09b C8 — follow-up commit closing two findings from the
    09b post-build audit (L-001, L-002)."""

    def test_empty_scene_description_raises_mimo_analysis_error(self, tmp_path):
        """L-001 fix: _parse_response returning {} (JSON parse failure)
        must NOT silently pass through as an empty-description clip.
        analyze_single_clip validates scene_description is non-empty
        and raises MimoAnalysisError if absent."""
        from promo.core.analyze import clip_analyzer
        from promo.core.errors import MimoAnalysisError
        from unittest.mock import patch
        import pytest

        clip = tmp_path / "clip.mp4"
        clip.write_bytes(b"x")

        # Mock _parse_response to return {} (the dead JSON parse branch).
        with patch.object(clip_analyzer, "_parse_response", return_value={}), \
             patch.object(clip_analyzer, "_call_openrouter", return_value={}):
            with pytest.raises(MimoAnalysisError) as exc_info:
                clip_analyzer.analyze_single_clip(
                    str(clip), "0042", cache_dir=None,
                )
        assert "scene_description" in str(exc_info.value)
        assert exc_info.value.clip_id == "0042"

    def test_whitespace_only_scene_description_raises(self, tmp_path):
        """L-001 fix: scene_description that's present but whitespace-only
        also counts as empty and must raise."""
        from promo.core.analyze import clip_analyzer
        from promo.core.errors import MimoAnalysisError
        from unittest.mock import patch
        import pytest

        clip = tmp_path / "clip.mp4"
        clip.write_bytes(b"x")
        with patch.object(
            clip_analyzer, "_parse_response",
            return_value={"scene_description": "   ", "category": "pool"},
        ), patch.object(clip_analyzer, "_call_openrouter", return_value={}):
            with pytest.raises(MimoAnalysisError):
                clip_analyzer.analyze_single_clip(
                    str(clip), "0042", cache_dir=None,
                )

    def test_valid_response_still_passes_through(self, tmp_path):
        """L-001 fix must not break the happy path — a valid response
        with non-empty scene_description still returns normally."""
        from promo.core.analyze import clip_analyzer
        from unittest.mock import patch

        clip = tmp_path / "clip.mp4"
        clip.write_bytes(b"x")
        with patch.object(
            clip_analyzer, "_parse_response",
            return_value={"scene_description": "a pool at dusk",
                          "category": "pool", "camera_motion": "static",
                          "dominant_motion_phase": "middle"},
        ), patch.object(clip_analyzer, "_call_openrouter", return_value={}):
            result = clip_analyzer.analyze_single_clip(
                str(clip), "0042", cache_dir=None,
            )
        assert result["scene_description"] == "a pool at dusk"

    def test_calibrated_wpm_picks_bumped_sidecar_over_exact(self, tmp_path):
        """L-002 fix: when tts_metrics_foo_65s.json AND tts_metrics_foo_65s-2.json
        both exist in the same dir, the newer (bumped) file wins."""
        import json
        import os
        from promo.core.script.pause_budget import load_calibrated_wpm

        exact = tmp_path / "tts_metrics_foo_65s.json"
        bumped = tmp_path / "tts_metrics_foo_65s-2.json"
        exact.write_text(json.dumps([{"measured_wpm": 170.0}]))
        bumped.write_text(json.dumps([{"measured_wpm": 200.0}]))
        # Force the bumped file to have a newer mtime.
        os.utime(exact, (1000, 1000))
        os.utime(bumped, (2000, 2000))
        result = load_calibrated_wpm("foo", 65, [str(tmp_path)])
        assert result == 200, "bumped sidecar with newer mtime must win"

    def test_calibrated_wpm_reads_bumped_sidecar_when_only_match(self, tmp_path):
        """L-002 fix: if only a bumped sidecar exists (unlikely in
        practice but defensive), load_calibrated_wpm still finds it."""
        import json
        from promo.core.script.pause_budget import load_calibrated_wpm

        bumped = tmp_path / "tts_metrics_foo_65s-3.json"
        bumped.write_text(json.dumps([{"measured_wpm": 190.0}]))
        result = load_calibrated_wpm("foo", 65, [str(tmp_path)])
        assert result == 190


class TestSprint17ECacheHitSceneDescriptionParity:
    """Sprint 17 E — analyze_single_clip cache-hit branch must apply the
    same scene_description non-empty raise the fresh path enforces (Sprint
    09b C8). Pre-C8 cache files written under the current _ANALYSIS_PROMPT
    + model survive the version-suffix invalidation today and would
    otherwise replay malformed entries through to Gemini.
    """

    def _seed_cache(self, tmp_path, scene_description):
        from promo.core.analyze import clip_analyzer
        clip = tmp_path / "clip.mp4"
        clip.write_bytes(b"x" * 4096)
        cache_dir = str(tmp_path / ".mimo_cache")
        cached = {
            "category": "pool",
            "camera_motion": "static",
            "dominant_motion_phase": "middle",
        }
        if scene_description is not None:
            cached["scene_description"] = scene_description
        key = clip_analyzer.clip_cache_key(
            str(clip),
            prompt=clip_analyzer._ANALYSIS_PROMPT,
            model=clip_analyzer.DEFAULT_MODEL,
        )
        clip_analyzer._save_cached_analysis(cache_dir, key, cached)
        return str(clip), cache_dir

    @pytest.mark.parametrize(
        "scene_value",
        ["", "   ", "\n\t", None],  # empty, spaces, newline+tab, missing key
        ids=["empty", "whitespace_spaces", "whitespace_newline_tab", "missing_key"],
    )
    def test_cache_hit_empty_or_whitespace_or_missing_raises(
        self, tmp_path, scene_value,
    ):
        """AC1 — parametrized over the four malformed shapes: empty string,
        whitespace, newline+tab, and key absent. All four must raise."""
        from promo.core.analyze import clip_analyzer
        from promo.core.errors import MimoAnalysisError

        clip_path, cache_dir = self._seed_cache(tmp_path, scene_value)
        with patch.object(clip_analyzer, "_call_openrouter") as mock_call:
            with pytest.raises(MimoAnalysisError) as exc_info:
                clip_analyzer.analyze_single_clip(
                    clip_path, "0042", cache_dir=cache_dir,
                )
        # The cache hit path must not fall through to the OpenRouter call.
        mock_call.assert_not_called()
        assert exc_info.value.clip_id == "0042"
        assert "scene_description" in str(exc_info.value)

    def test_cache_hit_with_valid_scene_description_still_returns(
        self, tmp_path,
    ):
        """AC2 — happy path regression: a cache entry with a valid
        scene_description and missing dominant_motion_phase still returns
        with `dominant_motion_phase="middle"` (default), unchanged from
        baseline behavior."""
        from promo.core.analyze import clip_analyzer

        clip_path, cache_dir = self._seed_cache(tmp_path, "a calm pool at sunset")
        # Re-save without dominant_motion_phase to confirm defaulting still applies.
        cached = {
            "scene_description": "a calm pool at sunset",
            "category": "pool",
            "camera_motion": "static",
        }
        key = clip_analyzer.clip_cache_key(
            clip_path,
            prompt=clip_analyzer._ANALYSIS_PROMPT,
            model=clip_analyzer.DEFAULT_MODEL,
        )
        clip_analyzer._save_cached_analysis(cache_dir, key, cached)

        with patch.object(clip_analyzer, "_call_openrouter") as mock_call:
            result = clip_analyzer.analyze_single_clip(
                clip_path, "0042", cache_dir=cache_dir,
            )
        mock_call.assert_not_called()
        assert result["scene_description"] == "a calm pool at sunset"
        assert result["dominant_motion_phase"] == "middle"  # default

    def test_cache_hit_raise_message_mirrors_fresh_path_shape(self, tmp_path):
        """E parity guard: the cache-hit raise carries the same `cause`
        RuntimeError text-shape as the fresh path's C8 raise — names
        scene_description and lists the cached dict keys for debug."""
        from promo.core.analyze import clip_analyzer
        from promo.core.errors import MimoAnalysisError

        clip_path, cache_dir = self._seed_cache(tmp_path, "")
        with pytest.raises(MimoAnalysisError) as exc_info:
            clip_analyzer.analyze_single_clip(
                clip_path, "0099", cache_dir=cache_dir,
            )
        msg = str(exc_info.value)
        assert "scene_description" in msg
        assert "0099" in msg
        # Cause carries the keys list (debug aid) — mirrors fresh path C8.
        cause_text = str(exc_info.value.cause)
        assert "category" in cause_text or "raw keys" in cause_text

    def test_cache_hit_raise_logs_error_with_bad_cache_path(self, tmp_path, caplog):
        """Audit-fix L-001 + L-002 + D-003: the cache-hit raise must
        emit a logger.error line naming the clip_id AND the on-disk
        path of the bad cache file. Without this, operators hit by a
        pre-C8 cache file get stuck without diagnostic info."""
        from promo.core.analyze import clip_analyzer
        from promo.core.errors import MimoAnalysisError

        clip_path, cache_dir = self._seed_cache(tmp_path, "")
        with caplog.at_level("ERROR", logger="promo.core.analyze.clip_analyzer"):
            with pytest.raises(MimoAnalysisError) as exc_info:
                clip_analyzer.analyze_single_clip(
                    clip_path, "0099", cache_dir=cache_dir,
                )
        error_records = [
            r for r in caplog.records
            if r.name == "promo.core.analyze.clip_analyzer" and r.levelname == "ERROR"
        ]
        assert error_records, "no ERROR record emitted at cache-hit raise"
        msg = error_records[0].message
        assert "0099" in msg
        # Path includes the .mimo_cache directory; we don't pin the
        # exact filename (cache key derivation is opaque) but the
        # directory must appear so the operator can locate the bad file.
        assert ".mimo_cache" in msg
        # Cause carries the same path (audit-fix: bad file must be
        # surfaceable from both the log AND the exception).
        assert ".mimo_cache" in str(exc_info.value.cause)


class TestSprintArsenalExternalizationMimoPrompt:
    """AC-9 + AC-10: the migrated MiMo prompt MUST preserve the
    ``_cache_version_suffix`` baseline. If this fails, every existing
    ``material/<slug>/.mimo_cache/<hash>-3c0efc35.json`` file across
    operator's POI set is silently invalidated on the next compile —
    100s of OpenRouter calls + significant wallclock + cost.

    AC-9 pins the suffix; AC-10 pins the byte-level "no trailing newline"
    rule that protects AC-9 from editor artifacts."""

    def test_ac9_mimo_cache_suffix_unchanged(self):
        """AC-9 — Sprint Arsenal Externalization invariant I-1.

        ``_cache_version_suffix(prompt, model)`` MUST equal the
        pre-sprint baseline ``3c0efc35`` for the v1 prompt + the
        canonical ``xiaomi/mimo-v2-omni`` model. The arsenal-loaded
        prompt must hash identically to the pre-sprint inline literal."""
        from promo.core.analyze.clip_analyzer import _cache_version_suffix
        from promo.core.arsenal_loader import load_system_prompt

        prompt = load_system_prompt("mimo_clip_analysis")
        suffix = _cache_version_suffix(prompt, "xiaomi/mimo-v2-omni")
        assert suffix == "3c0efc35", (
            f"MiMo cache version_suffix changed to {suffix!r} — "
            "this invalidates every existing .mimo_cache/ file across "
            "all POIs. Investigate before merging."
        )

    def test_ac10_mimo_md_file_no_trailing_newline(self):
        """AC-10 — the arsenal MD file's last byte MUST NOT be ``\\n``.

        Editor "save with trailing newline" is the most likely way the
        baseline-pinned suffix would change without anyone noticing,
        since most editors add a trailing newline by default. The
        arsenal_loader's ``.rstrip()`` is the runtime guard, but pinning
        the on-disk byte invariant catches drift at commit time."""
        from pathlib import Path

        path = Path(__file__).resolve().parents[3] / "arsenal" / "system_prompts" / "mimo_clip_analysis_v1.md"
        assert path.exists(), f"missing arsenal MD file: {path}"
        last_byte = path.read_bytes()[-1:]
        assert last_byte != b"\n", (
            f"{path} ends with a trailing newline byte — "
            "the loader's .rstrip() will mask this at runtime, "
            "but on-disk bytes are the durable invariant."
        )

    def test_ac9_loaded_prompt_matches_inline_literal(self):
        """Belt-and-suspenders: the arsenal-loaded prompt MUST be
        byte-identical (Python-string-equal) to the module-level
        ``_ANALYSIS_PROMPT`` symbol — both are now derived from the
        same MD file so this should always hold."""
        from promo.core.analyze.clip_analyzer import _ANALYSIS_PROMPT
        from promo.core.arsenal_loader import load_system_prompt

        loaded = load_system_prompt("mimo_clip_analysis")
        assert loaded == _ANALYSIS_PROMPT
        assert len(loaded) == 1391  # baseline char count, pre-sprint
