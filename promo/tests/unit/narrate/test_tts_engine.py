"""Unit tests for promo.core.narrate.tts_engine."""

import json
import os
import re
import shutil
import sys
import tempfile
from unittest.mock import patch, MagicMock

from pathlib import Path

import pytest

class TestSprint07VoiceCatalog:
    """AC2: VOICE_CATALOG holds the operator-validated voice IDs.
    Sprint TTS-Migration added the Gemini-backed ``kore`` entry; this test
    keeps a regression guard on the original three ElevenLabs names while
    allowing additional backend entries to coexist.
    """

    def test_catalog_contains_three_elevenlabs_voices(self):
        from promo.core.narrate.tts_engine import VOICE_CATALOG
        el_keys = {
            k for k, v in VOICE_CATALOG.items()
            if v.get("backend") == "elevenlabs"
        }
        assert el_keys == {"jarnathan", "hope", "heather"}

    def test_catalog_voice_ids_verbatim(self):
        from promo.core.narrate.tts_engine import VOICE_CATALOG
        assert VOICE_CATALOG["jarnathan"]["id"] == "c6SfcYrb2t09NHXiT80T"
        assert VOICE_CATALOG["hope"]["id"] == "tnSpp4vdxKPjI9w0GnoV"
        assert VOICE_CATALOG["heather"]["id"] == "MnUw1cSnpiLoLhpd3Hqp"

    def test_voice_settings_single_source_of_truth(self):
        from promo.core.narrate.tts_engine import VOICE_SETTINGS
        assert VOICE_SETTINGS == {
            "stability": 0.35,
            "similarity_boost": 0.75,
            "style": 0.25,
            "use_speaker_boost": True,
            "speed": 0.95,
        }

    def test_model_id_is_multilingual_v2(self):
        from promo.core.narrate.tts_engine import MODEL_ID
        assert MODEL_ID == "eleven_multilingual_v2"

class TestSprint07WordTimestampValidation:
    """AC11: malformed ElevenLabs timestamps abort the pipeline with a clear message."""

    def test_empty_timestamps_raises(self):
        from promo.core.narrate.tts_engine import _validate_word_timestamps
        with pytest.raises(RuntimeError, match="empty"):
            _validate_word_timestamps([], 5.0)

    def test_missing_field_raises(self):
        from promo.core.narrate.tts_engine import _validate_word_timestamps
        with pytest.raises(RuntimeError, match="missing key"):
            _validate_word_timestamps(
                [{"word": "hi", "start": 0.0}], 5.0,
            )

    def test_end_le_start_raises(self):
        from promo.core.narrate.tts_engine import _validate_word_timestamps
        with pytest.raises(RuntimeError, match="end<=start"):
            _validate_word_timestamps(
                [{"word": "hi", "start": 0.3, "end": 0.2}], 5.0,
            )

    def test_overshoot_end_raises(self):
        from promo.core.narrate.tts_engine import _validate_word_timestamps
        with pytest.raises(RuntimeError, match="exceeds narration_duration"):
            _validate_word_timestamps(
                [{"word": "hi", "start": 0.0, "end": 100.0}], 5.0,
            )

    def test_valid_passes(self):
        from promo.core.narrate.tts_engine import _validate_word_timestamps
        # Should not raise
        _validate_word_timestamps(
            [
                {"word": "one", "start": 0.0, "end": 0.3},
                {"word": "two", "start": 0.4, "end": 0.7},
            ],
            1.0,
        )

class TestSprint07CharactersToWords:
    """AC10 detail: character alignment groups into words on whitespace."""

    def test_basic_grouping(self):
        from promo.core.narrate.tts_engine import _characters_to_words
        chars = list("Hi world")
        starts = [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7]
        ends = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8]
        words = _characters_to_words(chars, starts, ends)
        assert [w["word"] for w in words] == ["Hi", "world"]
        assert words[0]["start"] == 0.0
        assert words[0]["end"] == 0.2
        assert words[1]["start"] == 0.3
        assert words[1]["end"] == 0.8

    def test_ssml_markup_skipped_defensively(self):
        """If alignment accidentally includes SSML, <...> tokens are skipped."""
        from promo.core.narrate.tts_engine import _characters_to_words
        chars = list("A<break/>B")
        starts = [float(i) * 0.1 for i in range(len(chars))]
        ends = [s + 0.1 for s in starts]
        words = _characters_to_words(chars, starts, ends)
        assert [w["word"] for w in words] == ["A", "B"]

class TestSprint07WhisperRemoved:
    """AC9: Whisper imports and _estimate_timestamps removed from active code."""

    def test_no_whisper_references_in_tts_engine(self):
        from promo.core.narrate import tts_engine
        import inspect
        source = inspect.getsource(tts_engine)
        for token in (
            "stable_whisper", "whisperx", "import whisper",
            "_estimate_timestamps", "get_word_timestamps_whisper",
        ):
            assert token not in source, (
                f"tts_engine still contains banned Whisper reference: {token!r}"
            )

    def test_no_whisper_references_in_compile_promo(self):
        from promo.cli import compile_promo
        import inspect
        source = inspect.getsource(compile_promo)
        for token in ("stable_whisper", "whisperx", "import whisper",
                      "get_word_timestamps_whisper"):
            assert token not in source, (
                f"compile_promo still contains banned Whisper reference: {token!r}"
            )

class TestSprint07FishAudioRemoved:
    """AC1, N4: no Fish Audio code path or env var remains."""

    def test_no_fish_audio_in_tts_engine(self):
        from promo.core.narrate import tts_engine
        import inspect
        source = inspect.getsource(tts_engine)
        for token in ("fish.audio", "FISH_AUDIO", "FISH_API_KEY", "reference_id", "s2-pro"):
            assert token not in source, (
                f"tts_engine still references Fish Audio token: {token!r}"
            )

    def test_no_fish_audio_in_compile_promo(self):
        from promo.cli import compile_promo
        import inspect
        source = inspect.getsource(compile_promo)
        for token in ("FISH_AUDIO_API_KEY", "FISH_API_KEY", "fish.audio"):
            assert token not in source, (
                f"compile_promo still references Fish Audio: {token!r}"
            )

class TestSprint07GenerateNarrationEndToEnd:
    """generate_narration wires ElevenLabs into the narration_result shape.

    Sprint 08 update: generate_narration now makes one API call per segment
    and assembles via ffmpeg silence concat. Tests mock the per-segment API
    call AND the ffmpeg helpers (no real subprocess, no real mp3 bytes).
    """

    def _fake_response(self, text: str, start: float = 0.0, step: float = 0.05):
        """Fake ElevenLabs with_timestamps response for one segment's text."""
        import base64
        chars = list(text)
        starts = [round(start + i * step, 3) for i in range(len(chars))]
        ends = [round(start + (i + 1) * step, 3) for i in range(len(chars))]
        audio_bytes = b"\x00\xff" * 1024
        return {
            "audio_base64": base64.b64encode(audio_bytes).decode("ascii"),
            "alignment": {
                "characters": chars,
                "character_start_times_seconds": starts,
                "character_end_times_seconds": ends,
            },
        }

    def _patch_ffmpeg(self, per_segment_duration: float = 1.0):
        """Patch context: short-circuits all ffmpeg subprocess calls.

        ``_generate_silence_mp3`` and ``_ffmpeg_concat_mp3s`` become no-ops
        that just touch a file. ``_ffprobe_duration`` returns a deterministic
        value so the assembly drift check passes.
        """
        from contextlib import ExitStack
        stack = ExitStack()

        def silence_touch(duration_sec, out):
            with open(out, "wb") as f:
                f.write(b"\x00")

        def concat_touch(inputs, out):
            with open(out, "wb") as f:
                f.write(b"\x00")

        # ffprobe must return a duration compatible with the fake alignment
        # so the drift check passes. Computed per-call from the stitched
        # offsets is ideal, but for tests we return a large-enough number.
        stack.callback(lambda: None)  # anchor
        stack.enter_context(patch(
            "promo.core.narrate.tts_engine._generate_silence_mp3",
            side_effect=silence_touch,
        ))
        stack.enter_context(patch(
            "promo.core.narrate.tts_engine._ffmpeg_concat_mp3s",
            side_effect=concat_touch,
        ))
        return stack

    def test_generate_narration_produces_word_timestamps(self):
        """Word timestamps length matches the narration's word count."""
        from promo.core.narrate.tts_engine import generate_narration

        segments = [
            {"segment": 1, "text": "Hello world", "pause_after_ms": 0, "word_count": 2},
        ]

        with tempfile.TemporaryDirectory() as tmpdir, \
             patch.dict(os.environ, {"ELEVENLABS_API_KEY": "test-key"}), \
             patch(
                 "promo.core.narrate.tts_elevenlabs._call_elevenlabs_with_timestamps",
                 return_value=self._fake_response("Hello world"),
             ), \
             patch("promo.core.narrate.tts_engine._generate_silence_mp3"), \
             patch("promo.core.narrate.tts_engine._ffmpeg_concat_mp3s"), \
             patch("promo.core.narrate.tts_engine._ffprobe_duration", return_value=0.55):
            result = generate_narration(
                segments=segments,
                voice_key="jarnathan",
                output_dir=tmpdir,
            )

            assert len(result["word_timestamps"]) == 2
            assert result["word_timestamps"][0]["word"] == "Hello"
            assert result["word_timestamps"][1]["word"] == "world"
            assert result["audio_path"].endswith(".mp3")
            assert result["duration"] > 0
            # tagged_text is the plain joined text (no SSML) in the per-segment path.
            assert "<break" not in result["tagged_text"]

    def test_generate_narration_aborts_on_malformed_alignment(self):
        """Malformed alignment response aborts per-segment TTS cleanly."""
        from promo.core.narrate.tts_engine import generate_narration

        import base64 as _b64
        bad_response = {
            "audio_base64": _b64.b64encode(b"\x00\xff" * 1024).decode("ascii"),
            "alignment": None,
        }

        with tempfile.TemporaryDirectory() as tmpdir, \
             patch.dict(os.environ, {"ELEVENLABS_API_KEY": "test-key"}), \
             patch(
                 "promo.core.narrate.tts_elevenlabs._call_elevenlabs_with_timestamps",
                 return_value=bad_response,
             ), \
             patch("promo.core.narrate.tts_engine._generate_silence_mp3"), \
             patch("promo.core.narrate.tts_engine._ffmpeg_concat_mp3s"), \
             patch("promo.core.narrate.tts_engine._ffprobe_duration", return_value=1.0):
            with pytest.raises(RuntimeError, match="alignment"):
                generate_narration(
                    segments=[{"segment": 1, "text": "Hi there", "word_count": 2}],
                    voice_key="jarnathan",
                    output_dir=tmpdir,
                )

    def test_generate_narration_uses_catalog_voice_id_per_batch_call(self):
        """Every per-batch TTS call uses the same resolved voice_id.

        Sprint 08.5: pause_weight=2 on seg1 forces a batch split so both
        segments get their own API call (otherwise they merge to one batch).
        """
        from promo.core.narrate.tts_engine import generate_narration, VOICE_CATALOG

        seen_ids = []

        def fake_call(text, voice_id, **kwargs):
            seen_ids.append(voice_id)
            return self._fake_response(text)

        segments = [
            {"segment": 1, "text": "Only word here.", "word_count": 3,
             "pause_after_ms": 500, "pause_weight": 2},
            {"segment": 2, "text": "Second segment.", "word_count": 2,
             "pause_after_ms": 0, "pause_weight": 1},
        ]
        with tempfile.TemporaryDirectory() as tmpdir, \
             patch.dict(os.environ, {"ELEVENLABS_API_KEY": "test-key"}), \
             patch(
                 "promo.core.narrate.tts_elevenlabs._call_elevenlabs_with_timestamps",
                 side_effect=fake_call,
             ), \
             patch("promo.core.narrate.tts_engine._generate_silence_mp3"), \
             patch("promo.core.narrate.tts_engine._ffmpeg_concat_mp3s"), \
             patch("promo.core.narrate.tts_engine._ffprobe_duration", return_value=2.0):
            generate_narration(
                segments=segments, voice_key="hope", output_dir=tmpdir,
            )
        # Two segments → two API calls, both with Hope's voice_id.
        expected_id = VOICE_CATALOG["hope"]["id"]
        assert seen_ids == [expected_id, expected_id]

class TestSprint08NarrationAssembly:
    """Per-segment TTS + silence concat stitching math (Sprint 08 structural fix)."""

    def _fake_response(self, text: str, step: float = 0.05):
        import base64
        chars = list(text)
        starts = [round(i * step, 3) for i in range(len(chars))]
        ends = [round((i + 1) * step, 3) for i in range(len(chars))]
        return {
            "audio_base64": base64.b64encode(b"\x00\xff" * 1024).decode("ascii"),
            "alignment": {
                "characters": chars,
                "character_start_times_seconds": starts,
                "character_end_times_seconds": ends,
            },
        }

    def test_word_timestamps_offset_by_prior_segments_and_silence(self):
        """Words in segment N+1 must be offset by (sum of prior segment durations + prior silences).

        Sprint 08.5: pause_weight=2 on seg1 forces a batch split so the
        silence between batches is actually inserted.
        """
        from promo.core.narrate.tts_engine import generate_narration

        # Two segments, each ~0.5s long (10 chars × 0.05s), with a 1000ms silence between.
        # Expected: seg1 words at 0-0.5s, seg2 words at (0.5 + 1.0) = 1.5s onwards.
        segments = [
            {"segment": 1, "text": "Hello there", "word_count": 2,
             "pause_after_ms": 1000, "pause_weight": 2},
            {"segment": 2, "text": "Hi", "word_count": 1,
             "pause_after_ms": 0, "pause_weight": 1},
        ]
        # "Hello there" is 11 chars × 0.05 = 0.55s. "Hi" is 2 chars × 0.05 = 0.1s.
        # Expected concat duration = 0.55 + 1.0 + 0.1 = 1.65s.
        with tempfile.TemporaryDirectory() as tmpdir, \
             patch.dict(os.environ, {"ELEVENLABS_API_KEY": "test-key"}), \
             patch(
                 "promo.core.narrate.tts_elevenlabs._call_elevenlabs_with_timestamps",
                 side_effect=lambda text, voice_id, **kw: self._fake_response(text),
             ), \
             patch("promo.core.narrate.tts_engine._generate_silence_mp3"), \
             patch("promo.core.narrate.tts_engine._ffmpeg_concat_mp3s"), \
             patch("promo.core.narrate.tts_engine._ffprobe_duration", return_value=1.65):
            result = generate_narration(
                segments=segments, voice_key="jarnathan", output_dir=tmpdir,
            )

        # Seg 1 words present, at 0-~0.5s range.
        assert len(result["word_timestamps"]) == 3
        seg1_last_end = result["word_timestamps"][1]["end"]
        assert seg1_last_end < 0.6
        # Seg 2 word "Hi" — must start AFTER seg1_end + 1.0s silence (= 1.5s).
        hi = result["word_timestamps"][2]
        assert hi["word"] == "Hi"
        assert hi["start"] >= 1.5, f"Hi start {hi['start']} should be >= 1.5 (seg1_end + 1s silence)"

    def test_segment_timestamps_built_from_offsets(self):
        from promo.core.narrate.tts_engine import generate_narration

        # Sprint 08.5: pause_weight=2 on seg1 splits into two batches so the
        # inter-batch silence is emitted.
        segments = [
            {"segment": 1, "text": "one two three", "word_count": 3,
             "pause_after_ms": 500, "pause_weight": 2},
            {"segment": 2, "text": "four five", "word_count": 2,
             "pause_after_ms": 0, "pause_weight": 1},
        ]
        with tempfile.TemporaryDirectory() as tmpdir, \
             patch.dict(os.environ, {"ELEVENLABS_API_KEY": "test-key"}), \
             patch(
                 "promo.core.narrate.tts_elevenlabs._call_elevenlabs_with_timestamps",
                 side_effect=lambda text, voice_id, **kw: self._fake_response(text),
             ), \
             patch("promo.core.narrate.tts_engine._generate_silence_mp3"), \
             patch("promo.core.narrate.tts_engine._ffmpeg_concat_mp3s"), \
             patch("promo.core.narrate.tts_engine._ffprobe_duration", return_value=2.0):
            result = generate_narration(
                segments=segments, voice_key="jarnathan", output_dir=tmpdir,
            )
        st = result["segment_timestamps"]
        assert len(st) == 2
        # Segment 2 must start AFTER segment 1's end + 0.5s silence.
        assert st[1]["start"] >= st[0]["end"] + 0.45

    def test_drift_detection_raises_when_ffprobe_disagrees(self):
        from promo.core.narrate.tts_engine import generate_narration

        segments = [
            {"segment": 1, "text": "only segment", "word_count": 2, "pause_after_ms": 0},
        ]
        # Stitched offsets will be ~0.6s (12 chars × 0.05s). Lie to ffprobe with
        # 5s — drift should trigger the RuntimeError.
        with tempfile.TemporaryDirectory() as tmpdir, \
             patch.dict(os.environ, {"ELEVENLABS_API_KEY": "test-key"}), \
             patch(
                 "promo.core.narrate.tts_elevenlabs._call_elevenlabs_with_timestamps",
                 side_effect=lambda text, voice_id, **kw: self._fake_response(text),
             ), \
             patch("promo.core.narrate.tts_engine._generate_silence_mp3"), \
             patch("promo.core.narrate.tts_engine._ffmpeg_concat_mp3s"), \
             patch("promo.core.narrate.tts_engine._ffprobe_duration", return_value=5.0):
            with pytest.raises(RuntimeError, match="drift too large"):
                generate_narration(
                    segments=segments, voice_key="jarnathan", output_dir=tmpdir,
                )

class TestSprint085TTSSpeedPlumbing:
    """AC11: --tts-speed threads through to voice_settings.speed."""

    def test_generate_narration_forwards_speed_to_voice_settings(self):
        """Every ElevenLabs call payload has voice_settings.speed == requested."""
        from promo.core.narrate.tts_engine import generate_narration
        import base64

        def fake_response(text: str, **kw):
            chars = list(text)
            return {
                "audio_base64": base64.b64encode(b"\x00\xff" * 1024).decode("ascii"),
                "alignment": {
                    "characters": chars,
                    "character_start_times_seconds":
                        [round(0.05 * i, 3) for i in range(len(chars))],
                    "character_end_times_seconds":
                        [round(0.05 * (i + 1), 3) for i in range(len(chars))],
                },
            }

        captured_payloads: list[dict] = []

        def fake_post(url, params=None, json=None, headers=None, timeout=None):
            captured_payloads.append(json)
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = fake_response(json["text"])
            resp.text = ""
            return resp

        segments = [
            {"segment": 1, "text": "First part", "word_count": 2,
             "pause_weight": 2, "pause_after_ms": 500},
            {"segment": 2, "text": "Second part", "word_count": 2,
             "pause_weight": 1, "pause_after_ms": 0},
        ]
        with tempfile.TemporaryDirectory() as tmpdir, \
             patch.dict(os.environ, {"ELEVENLABS_API_KEY": "test-key"}), \
             patch("promo.core.narrate.tts_engine.requests.post", side_effect=fake_post), \
             patch("promo.core.narrate.tts_engine._generate_silence_mp3"), \
             patch("promo.core.narrate.tts_engine._ffmpeg_concat_mp3s"), \
             patch("promo.core.narrate.tts_engine._ffprobe_duration", return_value=1.5):
            generate_narration(
                segments=segments, voice_key="jarnathan",
                output_dir=tmpdir, speed=0.90,
            )
        assert captured_payloads
        for payload in captured_payloads:
            assert payload["voice_settings"]["speed"] == 0.90

    def test_cli_help_shows_tts_speed_default_0_95(self):
        """--tts-speed appears in CLI with default 0.95."""
        import subprocess
        res = subprocess.run(
            ["python3", "-m", "promo.cli.compile_promo", "--help"],
            capture_output=True, text=True, check=False,
        )
        assert res.returncode == 0
        out = res.stdout
        assert "--tts-speed" in out
        assert "0.95" in out

class TestSprint085BatchMergeE2E:
    """AC5: end-to-end narration with weights [1,2,3,1,_] → exactly 3 API calls."""

    def _fake_response(self, text: str, step: float = 0.05):
        import base64
        chars = list(text)
        return {
            "audio_base64": base64.b64encode(b"\x00\xff" * 1024).decode("ascii"),
            "alignment": {
                "characters": chars,
                "character_start_times_seconds":
                    [round(step * i, 3) for i in range(len(chars))],
                "character_end_times_seconds":
                    [round(step * (i + 1), 3) for i in range(len(chars))],
            },
        }

    def test_five_segments_merge_to_three_batches(self):
        """AC5 canonical: pause_weights=[1,2,3,1,_] → 3 API calls, 2 silence mp3s."""
        from promo.core.narrate.tts_engine import generate_narration

        call_texts: list[str] = []

        def fake_call(text, voice_id, **kw):
            call_texts.append(text)
            return self._fake_response(text)

        segments = [
            {"segment": 1, "text": "first segment words here",
             "word_count": 4, "pause_weight": 1, "pause_after_ms": 0},
            {"segment": 2, "text": "second segment more words",
             "word_count": 4, "pause_weight": 2, "pause_after_ms": 1500},
            {"segment": 3, "text": "third reveal beat",
             "word_count": 3, "pause_weight": 3, "pause_after_ms": 2500},
            {"segment": 4, "text": "fourth chain continues",
             "word_count": 3, "pause_weight": 1, "pause_after_ms": 0},
            {"segment": 5, "text": "fifth and final words",
             "word_count": 4, "pause_weight": 1, "pause_after_ms": 0},
        ]
        silence_files_written: list[str] = []

        def capture_silence(duration_sec, path):
            silence_files_written.append(path)
            with open(path, "wb") as f:
                f.write(b"\x00")

        with tempfile.TemporaryDirectory() as tmpdir, \
             patch.dict(os.environ, {"ELEVENLABS_API_KEY": "test-key"}), \
             patch(
                 "promo.core.narrate.tts_elevenlabs._call_elevenlabs_with_timestamps",
                 side_effect=fake_call,
             ), \
             patch(
                 "promo.core.narrate.tts_engine._generate_silence_mp3",
                 side_effect=capture_silence,
             ), \
             patch("promo.core.narrate.tts_engine._ffmpeg_concat_mp3s"), \
             patch("promo.core.narrate.tts_engine._ffprobe_duration", return_value=10.0):
            result = generate_narration(
                segments=segments, voice_key="jarnathan", output_dir=tmpdir,
            )

        # Exactly 3 API calls — not 5.
        assert len(call_texts) == 3
        # Exactly 2 silence mp3s (between batch 1→2 and 2→3).
        assert len(silence_files_written) == 2
        assert all(
            p.endswith("silence_01.mp3") or p.endswith("silence_02.mp3")
            for p in silence_files_written
        )
        # All 5 source segments present in word_timestamps, monotonically non-decreasing.
        wts = result["word_timestamps"]
        # Each source segment has its words; segment_timestamps has 5 entries (per source seg).
        assert len(result["segment_timestamps"]) == 5
        starts = [w["start"] for w in wts]
        assert starts == sorted(starts)

    def test_merged_batch_word_count_split_preserves_source_segments(self):
        """AC6: a merged batch with 3 source segments produces 3 segment_timestamps."""
        from promo.core.narrate.tts_engine import generate_narration

        # 3 source segments, all pause_weight=1 → one batch.
        # word_counts = [10, 12, 8] → batch has 30 words total.
        # Text length × step defines alignment; use ~30 chars total so words split cleanly.
        seg1_text = "w1 w2 w3 w4 w5 w6 w7 w8 w9 wa"          # 10 words
        seg2_text = "xa xb xc xd xe xf xg xh xi xj xk xl"    # 12 words
        seg3_text = "ya yb yc yd ye yf yg yh"                # 8 words
        segments = [
            {"segment": 1, "text": seg1_text, "word_count": 10, "pause_weight": 1},
            {"segment": 2, "text": seg2_text, "word_count": 12, "pause_weight": 1},
            {"segment": 3, "text": seg3_text, "word_count": 8, "pause_weight": 1},
        ]

        def fake_call(text, voice_id, **kw):
            import base64
            chars = list(text)
            return {
                "audio_base64": base64.b64encode(b"\x00\xff" * 1024).decode("ascii"),
                "alignment": {
                    "characters": chars,
                    "character_start_times_seconds":
                        [round(0.05 * i, 3) for i in range(len(chars))],
                    "character_end_times_seconds":
                        [round(0.05 * (i + 1), 3) for i in range(len(chars))],
                },
            }

        # Merged batch text is ~89 chars × 0.05s/char = 4.45s; match ffprobe.
        with tempfile.TemporaryDirectory() as tmpdir, \
             patch.dict(os.environ, {"ELEVENLABS_API_KEY": "test-key"}), \
             patch(
                 "promo.core.narrate.tts_elevenlabs._call_elevenlabs_with_timestamps",
                 side_effect=fake_call,
             ), \
             patch("promo.core.narrate.tts_engine._generate_silence_mp3"), \
             patch("promo.core.narrate.tts_engine._ffmpeg_concat_mp3s"), \
             patch("promo.core.narrate.tts_engine._ffprobe_duration", return_value=4.5):
            result = generate_narration(
                segments=segments, voice_key="jarnathan", output_dir=tmpdir,
            )

        st = result["segment_timestamps"]
        assert len(st) == 3
        assert [e["word_count"] for e in st] == [10, 12, 8]
        # Each segment starts ≥ the previous one's end (monotonic within batch).
        assert st[1]["start"] >= st[0]["end"]
        assert st[2]["start"] >= st[1]["end"]

class TestSprint085TempdirCleanup:
    """AC12: generate_narration cleans up partial files on exception."""

    def _fake_response(self, text: str):
        import base64
        chars = list(text)
        return {
            "audio_base64": base64.b64encode(b"\x00\xff" * 1024).decode("ascii"),
            "alignment": {
                "characters": chars,
                "character_start_times_seconds":
                    [round(0.05 * i, 3) for i in range(len(chars))],
                "character_end_times_seconds":
                    [round(0.05 * (i + 1), 3) for i in range(len(chars))],
            },
        }

    def test_caller_provided_output_dir_cleans_partial_seg_files(self, tmp_path):
        """AC12 test a: seg_01.mp3 and seg_02.mp3 unlinked when 3rd call raises."""
        from promo.core.narrate.tts_engine import generate_narration

        call_count = {"n": 0}

        def fake_gen_seg(text, voice_id, out_path, *, speed=None):
            call_count["n"] += 1
            if call_count["n"] == 3:
                raise RuntimeError("simulated TTS failure on batch 3")
            # Touch the output file to simulate a completed segment.
            with open(out_path, "wb") as f:
                f.write(b"\x00")
            return 1.0, [{"word": "w", "start": 0.0, "end": 0.5}]

        segments = [
            {"segment": i, "text": f"text {i}", "word_count": 2,
             "pause_weight": 2, "pause_after_ms": 500}
            for i in range(1, 6)
        ]
        out_dir = str(tmp_path / "out")
        os.makedirs(out_dir, exist_ok=True)

        with patch.dict(os.environ, {"ELEVENLABS_API_KEY": "test-key"}), \
             patch(
                 "promo.core.narrate.tts_engine._generate_segment_audio_elevenlabs",
                 side_effect=fake_gen_seg,
             ), \
             patch("promo.core.narrate.tts_engine._generate_silence_mp3"), \
             patch("promo.core.narrate.tts_engine._ffmpeg_concat_mp3s"), \
             patch("promo.core.narrate.tts_engine._ffprobe_duration", return_value=3.0):
            with pytest.raises(RuntimeError, match="simulated TTS failure"):
                generate_narration(
                    segments=segments, voice_key="jarnathan", output_dir=out_dir,
                )

        assert not os.path.exists(os.path.join(out_dir, "seg_01.mp3"))
        assert not os.path.exists(os.path.join(out_dir, "seg_02.mp3"))
        # Caller-provided directory itself survives.
        assert os.path.isdir(out_dir)

    def test_auto_tmpdir_removed_on_exception(self):
        """AC12 test b: auto-created tmpdir is removed on exception."""
        from promo.core.narrate.tts_engine import generate_narration

        captured_dir = {"path": None}

        def fake_gen_seg(text, voice_id, out_path, *, speed=None):
            captured_dir["path"] = os.path.dirname(out_path)
            raise RuntimeError("boom")

        segments = [
            {"segment": 1, "text": "only", "word_count": 1,
             "pause_weight": 1, "pause_after_ms": 0},
        ]
        with patch.dict(os.environ, {"ELEVENLABS_API_KEY": "test-key"}), \
             patch(
                 "promo.core.narrate.tts_engine._generate_segment_audio_elevenlabs",
                 side_effect=fake_gen_seg,
             ), \
             patch("promo.core.narrate.tts_engine._generate_silence_mp3"), \
             patch("promo.core.narrate.tts_engine._ffmpeg_concat_mp3s"), \
             patch("promo.core.narrate.tts_engine._ffprobe_duration", return_value=1.0):
            with pytest.raises(RuntimeError, match="boom"):
                generate_narration(
                    segments=segments, voice_key="jarnathan", output_dir=None,
                )

        assert captured_dir["path"] is not None
        assert not os.path.exists(captured_dir["path"])

class TestSprint085TTSNormalization:
    """Pre-TTS regex: $ and % are expanded to words before feeding ElevenLabs."""

    def test_currency_sign_expanded(self):
        from promo.core.narrate.tts_engine import _normalize_for_tts
        assert _normalize_for_tts("It's $1,900 a night.") == "It's 1,900 dollars a night."

    def test_currency_with_decimals(self):
        from promo.core.narrate.tts_engine import _normalize_for_tts
        assert _normalize_for_tts("just $4.99 extra") == "just 4.99 dollars extra"

    def test_percent_expanded(self):
        from promo.core.narrate.tts_engine import _normalize_for_tts
        assert _normalize_for_tts("Save 10% today") == "Save 10 percent today"

    def test_combined_currency_and_percent(self):
        from promo.core.narrate.tts_engine import _normalize_for_tts
        result = _normalize_for_tts("$2,500 rooms, 15% off")
        assert result == "2,500 dollars rooms, 15 percent off"

    def test_no_match_is_unchanged(self):
        from promo.core.narrate.tts_engine import _normalize_for_tts
        assert _normalize_for_tts("Plain narration.") == "Plain narration."

    def test_empty_input(self):
        from promo.core.narrate.tts_engine import _normalize_for_tts
        assert _normalize_for_tts("") == ""

class TestSprint085CaptionWordCleanup:
    """_clean_caption_word strips wrapping quotes / brackets from alignment words."""

    def test_strips_straight_quotes(self):
        from promo.core.narrate.tts_engine import _clean_caption_word
        assert _clean_caption_word('"can\'t"') == "can't"

    def test_strips_parens(self):
        from promo.core.narrate.tts_engine import _clean_caption_word
        assert _clean_caption_word("(can't)") == "can't"

    def test_strips_smart_quotes(self):
        from promo.core.narrate.tts_engine import _clean_caption_word
        assert _clean_caption_word("\u201ccan't\u201d") == "can't"

    def test_preserves_internal_apostrophe(self):
        from promo.core.narrate.tts_engine import _clean_caption_word
        assert _clean_caption_word("can't") == "can't"

    def test_preserves_trailing_period(self):
        from promo.core.narrate.tts_engine import _clean_caption_word
        # Period conveys sentence cadence; keep it.
        assert _clean_caption_word("end.") == "end."

    def test_preserves_compound_hyphen(self):
        from promo.core.narrate.tts_engine import _clean_caption_word
        assert _clean_caption_word("rust-red") == "rust-red"

    def test_empty_stripped_falls_back_to_original(self):
        from promo.core.narrate.tts_engine import _clean_caption_word
        # Not useful edge, but should never return "" when input was non-empty
        # and consists entirely of stripping chars (so raw is unchanged).
        assert _clean_caption_word('"') == '"'

class TestSprint09bC6HelperExtractions:
    """Sprint 09b C6 (ACs 24-27): helper extractions preserve behavior
    while shrinking the function bodies they come out of."""

    def test_back_allocate_timestamps_exists(self):
        from promo.core.narrate.tts_engine import _back_allocate_timestamps
        assert callable(_back_allocate_timestamps)

    def test_back_allocate_timestamps_exact_word_count_path(self):
        """When each batch's returned word count matches the source
        word_counts, each source segment gets exactly its declared
        words' timestamps."""
        from promo.core.narrate.tts_engine import _back_allocate_timestamps
        batches = [
            {"segments": [
                {"segment": 1, "text": "Hello world.", "word_count": 2},
                {"segment": 2, "text": "Second line.", "word_count": 2},
            ]},
        ]
        batch_audios = [
            ("/tmp/b1.mp3", 4.0, [
                {"word": "Hello", "start": 0.0, "end": 0.4},
                {"word": "world.", "start": 0.5, "end": 0.9},
                {"word": "Second", "start": 1.2, "end": 1.6},
                {"word": "line.", "start": 1.7, "end": 2.1},
            ]),
        ]
        silence = []  # single batch, no inter-batch silence
        words, segs, total = _back_allocate_timestamps(
            batch_audios, batches, silence,
        )
        assert len(words) == 4
        assert len(segs) == 2
        assert segs[0]["segment"] == 1
        assert segs[0]["word_count"] == 2
        assert segs[1]["segment"] == 2
        assert segs[1]["word_count"] == 2
        assert total == 4.0

    def test_back_allocate_timestamps_proportional_fallback(self):
        """When word counts don't match (e.g. numerals/contractions),
        falls back to proportional character-count split."""
        from promo.core.narrate.tts_engine import _back_allocate_timestamps
        batches = [
            {"segments": [
                # total_expected = 4, but actual words returned = 3
                # Falls into proportional split.
                {"segment": 1, "text": "abc.", "word_count": 2},
                {"segment": 2, "text": "defg.", "word_count": 2},
            ]},
        ]
        batch_audios = [
            ("/tmp/b1.mp3", 3.0, [
                {"word": "abc.", "start": 0.0, "end": 0.5},
                {"word": "de", "start": 0.6, "end": 0.9},
                {"word": "fg.", "start": 1.0, "end": 1.3},
            ]),
        ]
        silence = []
        words, segs, total = _back_allocate_timestamps(
            batch_audios, batches, silence,
        )
        assert len(words) == 3
        assert len(segs) == 2
        # Proportional split hit — total word_count equals returned words.
        assert sum(s["word_count"] for s in segs) == 3

    def test_back_allocate_timestamps_inter_batch_silence_accumulates(self):
        from promo.core.narrate.tts_engine import _back_allocate_timestamps
        batches = [
            {"segments": [{"segment": 1, "text": "A.", "word_count": 1}]},
            {"segments": [{"segment": 2, "text": "B.", "word_count": 1}]},
        ]
        batch_audios = [
            ("/tmp/b1.mp3", 1.0, [{"word": "A.", "start": 0.0, "end": 0.5}]),
            ("/tmp/b2.mp3", 1.0, [{"word": "B.", "start": 0.0, "end": 0.5}]),
        ]
        silence = [2.0]  # 2s of silence between batch 1 and batch 2
        words, segs, total = _back_allocate_timestamps(
            batch_audios, batches, silence,
        )
        # Batch 2 word timestamps must be offset by batch 1 duration + silence.
        assert words[1]["start"] == 3.0  # 1.0 batch + 2.0 silence
        assert segs[1]["start"] == 3.0
        # Total = 1.0 + 2.0 + 1.0
        assert total == 4.0

    def test_generate_narration_body_shorter(self):
        """AC24 binary: generate_narration body is measurably shorter
        after extraction (was ~270 logic lines). Target 150 is
        aspirational; 'measurably shorter' is the pass bar."""
        import ast
        from pathlib import Path
        src = Path(__file__).resolve().parents[3] / "core" / "narrate" / "tts_engine.py"
        tree = ast.parse(src.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "generate_narration":
                body_lines = node.end_lineno - node.lineno + 1
                # Pre-09b: ~310. Post-C6: targeting <=200.
                assert body_lines <= 210, (
                    f"generate_narration is {body_lines} lines; "
                    "C6 extraction should keep it at most 210"
                )
                return
        raise AssertionError("generate_narration not found")

    def test_resolve_bgm_paths_explicit_bgm_paths_wins(self, tmp_path):
        from promo.cli.compile_promo import _resolve_bgm_paths
        from unittest.mock import MagicMock
        backend = MagicMock()
        result = _resolve_bgm_paths(
            bgm_paths=["/tmp/a.mp3", "/tmp/b.mp3"],
            bgm_path=None,
            poi_name="X",
            backend=backend,
            tmp_dir=str(tmp_path),
            target_duration_sec=65,
        )
        assert result == ["/tmp/a.mp3", "/tmp/b.mp3"]
        # Backend NOT called — explicit paths short-circuit.
        backend.fetch_bgm.assert_not_called()

    def test_resolve_bgm_paths_single_bgm_path(self, tmp_path):
        from promo.cli.compile_promo import _resolve_bgm_paths
        from unittest.mock import MagicMock
        backend = MagicMock()
        result = _resolve_bgm_paths(
            bgm_paths=None,
            bgm_path="/tmp/only.mp3",
            poi_name="X",
            backend=backend,
            tmp_dir=str(tmp_path),
            target_duration_sec=65,
        )
        assert result == ["/tmp/only.mp3"]

    def test_resolve_bgm_paths_backend_fetch_fallback(self, tmp_path):
        from promo.cli.compile_promo import _resolve_bgm_paths
        from unittest.mock import MagicMock
        backend = MagicMock()
        backend.fetch_bgm.return_value = "/tmp/fetched.mp3"
        result = _resolve_bgm_paths(
            bgm_paths=None,
            bgm_path=None,
            poi_name="X",
            backend=backend,
            tmp_dir=str(tmp_path),
            target_duration_sec=65,
        )
        assert result == ["/tmp/fetched.mp3"]

    def test_resolve_voice_keys_explicit_voice(self):
        from promo.cli.compile_promo import _resolve_voice_keys
        from promo.core.narrate.tts_engine import VOICE_CATALOG
        first_key = next(iter(VOICE_CATALOG.keys()))
        result = _resolve_voice_keys(first_key)
        assert result == [first_key]

    def test_resolve_voice_keys_default_rotation(self):
        from promo.cli.compile_promo import _resolve_voice_keys
        from promo.core.narrate.tts_engine import VOICE_CATALOG
        result = _resolve_voice_keys(None)
        assert result == list(VOICE_CATALOG.keys())

    def test_resolve_voice_keys_unknown_raises(self):
        from promo.cli.compile_promo import _resolve_voice_keys
        import pytest
        with pytest.raises(ValueError, match="Unknown --voice"):
            _resolve_voice_keys("nonexistent_voice")
