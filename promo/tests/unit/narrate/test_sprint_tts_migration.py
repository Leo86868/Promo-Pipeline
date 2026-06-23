"""Sprint TTS-Migration tests — Gemini TTS backend + MMS_FA forced aligner.

Separate file (not merged into test_promo_module.py) so this sprint's
commits stay cleanly isolated from Sprint 12a's parallel WIP which is
also modifying that file (contract N6 — no parallel-session sweep).

Coverage map:
    Phase 2 (ACs 3, 4, 5)
      - AC3 — VOICE_CATALOG backend discriminator + Gemini-first ordering
      - AC4 — generate_narration dispatch returns identical tuple shape
              for both backends; primary Gemini model + 404/403 fallback
      - AC5 — PCM → mp3 preconvert produces 44.1kHz mono; no torchcodec
    Phase 3 (ACs 6, 7, 8)
      - AC6 — align_words happy path, list[dict] with monotonic starts
      - AC7 — ForcedAlignmentError raised on OOV token, with the
              offending token surfaced in str(exc)
      - AC8 — subprocess.run guard grep (source-level check)
    Negative criteria
      - N1 — no Gemini pause tags in production modules
      - N3 — backend-branch centralization (grep guard)
      - N4 — generate_narration public signature stable
"""

from __future__ import annotations

import ast
import re
import subprocess
import wave
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
FIXTURE_WAV = REPO_ROOT / "promo" / "tests" / "fixtures" / "sprint-tts-migration" / "forced_aligner_sample.wav"


# ===========================================================================
#  Phase 3 — Forced aligner (ACs 6, 7, 8)
# ===========================================================================


class TestForcedAlignerHappyPath:
    """AC6 — align_words wraps torchaudio.pipelines.MMS_FA; returns
    list[dict] with monotonically increasing starts."""

    def test_returns_list_of_dicts_with_word_start_end_keys(self):
        from promo.core.narrate.forced_aligner import align_words

        tokens = ["Tucked", "along", "a", "private", "cove"]
        result = align_words(str(FIXTURE_WAV), tokens)

        assert isinstance(result, list)
        assert len(result) == len(tokens)
        for entry in result:
            assert isinstance(entry, dict)
            assert set(entry.keys()) == {"word", "start", "end"}
            assert isinstance(entry["word"], str)
            assert isinstance(entry["start"], float)
            assert isinstance(entry["end"], float)

    def test_preserves_caller_token_form_in_output(self):
        """Original casing / punctuation is round-tripped through ``word``."""
        from promo.core.narrate.forced_aligner import align_words

        tokens = ["Tucked", "along", "a", "private", "cove"]
        result = align_words(str(FIXTURE_WAV), tokens)

        assert [r["word"] for r in result] == tokens

    def test_monotonically_increasing_starts(self):
        from promo.core.narrate.forced_aligner import align_words

        result = align_words(
            str(FIXTURE_WAV),
            ["Tucked", "along", "a", "private", "cove"],
        )
        starts = [r["start"] for r in result]
        assert starts == sorted(starts), f"starts not monotonic: {starts}"

    def test_each_end_strictly_after_its_start(self):
        """Zero-width spans are inflated to 1ms in ``align_words`` (H-001
        fix) so downstream ``_validate_word_timestamps`` does not reject
        malformed end<=start entries."""
        from promo.core.narrate.forced_aligner import align_words

        result = align_words(
            str(FIXTURE_WAV),
            ["Tucked", "along", "a", "private", "cove"],
        )
        for entry in result:
            assert entry["end"] > entry["start"], (
                f"end<=start for {entry} — zero-width span should have "
                "been inflated"
            )

    def test_empty_tokens_raises_value_error(self):
        from promo.core.narrate.forced_aligner import align_words

        with pytest.raises(ValueError, match="empty"):
            align_words(str(FIXTURE_WAV), [])


class TestForcedAlignerUnalignable:
    """AC7 — L-003 guard: unaligned tokens do not silently disappear.

    Post-E2E refinement (2026-04-20): MMS_FA never refuses to emit spans
    even for nonsense tokens — it returns a low-confidence best-fit. The
    guard's load-bearing job is preserving the output-list length (no
    silent drop) while surfacing issues to the operator. Raises fire
    ONLY on structural failures (empty span list, token normalizes to
    empty); score-based issues warn + return timestamps.
    """

    def test_punctuation_only_token_raises_with_empty_normalization_reason(self):
        """A token like ``...`` normalizes to empty and must raise — it
        literally has no MMS_FA character to align against."""
        from promo.core.errors import ForcedAlignmentError
        from promo.core.narrate.forced_aligner import align_words

        with pytest.raises(ForcedAlignmentError) as exc_info:
            align_words(str(FIXTURE_WAV), ["hello", "...", "world"])
        assert exc_info.value.token == "..."
        assert exc_info.value.position == 1
        assert "empty" in exc_info.value.reason.lower()

    def test_unicode_nonletter_token_raises(self):
        """A token like ``—`` (em-dash) or Chinese characters normalize
        to empty string and raise."""
        from promo.core.errors import ForcedAlignmentError
        from promo.core.narrate.forced_aligner import align_words

        with pytest.raises(ForcedAlignmentError) as exc_info:
            align_words(str(FIXTURE_WAV), ["hello", "世界", "world"])
        assert exc_info.value.token == "世界"

    def test_oov_token_does_not_raise_returns_all_timestamps(self):
        """Nonsense-but-alphabetic tokens (rare real-world case) return
        low-confidence timestamps; no silent drop (preserves output list
        length — the L-003 invariant). Operator gets a warning log."""
        from promo.core.narrate.forced_aligner import align_words

        tokens = ["tucked", "along", "a", "private", "cove", "xqzvvvt"]
        result = align_words(str(FIXTURE_WAV), tokens)
        assert len(result) == len(tokens)
        assert [r["word"] for r in result] == tokens

    def test_ampersand_token_normalizes_to_and_not_empty(self):
        """Regression — ``&`` is pronounced "and" by TTS engines, so the
        forced-aligner token normalizer must map ``&`` → ``and`` BEFORE
        the a-z strip. Surfaced during Sprint 12b's first Ocean Key E2E
        render (POI name: "Ocean Key Resort & Spa")."""
        from promo.core.narrate.forced_aligner import _preprocess_token

        assert _preprocess_token("&") == "and"
        # Compound tokens still survive.
        assert _preprocess_token("R&D") == "randd"
        # Non-spoken punctuation still rejected.
        assert _preprocess_token("...") == ""


class TestForcedAlignerSubprocessGuard:
    """AC8 — L-002 guard: every ``subprocess.run`` in the Gemini path
    and forced_aligner path must use ``check=True`` (or have an explicit
    returncode check within 5 lines)."""

    @staticmethod
    def _check_module_subprocess_calls(path: Path) -> list[str]:
        """Return a list of human-readable failures (empty list = pass).

        AC8 guard: each ``subprocess.run`` call in the Gemini path must
        use ``check=True`` OR its enclosing function body must inspect
        ``returncode`` explicitly. Enclosing-function scope (not a fixed
        line window) handles multi-line try/except blocks cleanly.
        """
        src = path.read_text()
        tree = ast.parse(src, filename=str(path))
        failures: list[str] = []

        # Map AST nodes to their enclosing FunctionDef (or None for module scope).
        parent_func: dict[int, ast.FunctionDef] = {}

        def _walk(node: ast.AST, enclosing: ast.FunctionDef | None) -> None:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.FunctionDef):
                    _walk(child, child)
                else:
                    parent_func[id(child)] = enclosing  # type: ignore[assignment]
                    _walk(child, enclosing)

        _walk(tree, None)

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            is_sp_run = (
                isinstance(func, ast.Attribute)
                and func.attr == "run"
                and isinstance(func.value, ast.Name)
                and func.value.id == "subprocess"
            )
            if not is_sp_run:
                continue
            keywords = {kw.arg: kw for kw in node.keywords if kw.arg}
            check_kw = keywords.get("check")
            check_true = (
                isinstance(check_kw, ast.keyword)
                and isinstance(check_kw.value, ast.Constant)
                and check_kw.value.value is True
            )
            if check_true:
                continue
            # Fallback: look for ``returncode`` anywhere in the enclosing
            # function body. This handles multi-line try/except blocks
            # where the returncode check lands >5 lines below the call.
            enclosing = parent_func.get(id(node))
            if enclosing is not None:
                enclosing_src = ast.unparse(enclosing)
                if "returncode" in enclosing_src:
                    continue
            failures.append(
                f"{path.name}:{node.lineno} subprocess.run without check=True "
                f"or returncode inspection"
            )
        return failures

    def test_tts_engine_subprocess_calls_are_guarded(self):
        path = REPO_ROOT / "promo" / "core" / "narrate" / "tts_engine.py"
        failures = self._check_module_subprocess_calls(path)
        assert failures == [], "\n".join(failures)

    def test_tts_assembly_subprocess_calls_are_guarded(self):
        path = REPO_ROOT / "promo" / "core" / "narrate" / "tts_assembly.py"
        failures = self._check_module_subprocess_calls(path)
        assert failures == [], "\n".join(failures)

    def test_tts_gemini_subprocess_calls_are_guarded(self):
        path = REPO_ROOT / "promo" / "core" / "narrate" / "tts_gemini.py"
        failures = self._check_module_subprocess_calls(path)
        assert failures == [], "\n".join(failures)

    def test_forced_aligner_subprocess_calls_are_guarded(self):
        path = REPO_ROOT / "promo" / "core" / "narrate" / "forced_aligner.py"
        failures = self._check_module_subprocess_calls(path)
        assert failures == [], "\n".join(failures)


# ===========================================================================
#  Phase 2 — Gemini TTS backend (ACs 3, 4, 5)
#  (Signature + dispatch + format tests are added once Phase 2 lands;
#  stubs declared here reserve the space so the test budget is visible.)
# ===========================================================================


class TestVoiceCatalogBackendDiscriminator:
    """AC3 — every VOICE_CATALOG entry has a ``backend`` key; Gemini
    entries appear BEFORE ElevenLabs in dict order so ``compile_promo``
    rotation defaults to Gemini when ``--voice`` unset."""

    def test_every_entry_has_backend_key(self):
        from promo.core.narrate.tts_engine import VOICE_CATALOG

        for key, voice in VOICE_CATALOG.items():
            assert "backend" in voice, f"VOICE_CATALOG[{key!r}] missing 'backend'"
            assert voice["backend"] in {"gemini", "elevenlabs"}

    def test_gemini_entries_ordered_before_elevenlabs(self):
        """First entry's backend determines rotation default. Must be Gemini."""
        from promo.core.narrate.tts_engine import VOICE_CATALOG

        first_backend = next(iter(VOICE_CATALOG.values()))["backend"]
        assert first_backend == "gemini", (
            f"VOICE_CATALOG rotation default is {first_backend!r}; expected "
            f"'gemini' (Gemini entries must be declared first)"
        )

    def test_existing_elevenlabs_voices_preserved(self):
        """Back-compat — jarnathan / hope / heather still in catalog."""
        from promo.core.narrate.tts_engine import VOICE_CATALOG

        for key in ("jarnathan", "hope", "heather"):
            assert key in VOICE_CATALOG
            assert VOICE_CATALOG[key]["backend"] == "elevenlabs"


class TestGenerateNarrationDispatch:
    """AC4 — ``generate_narration`` routes to the right backend generator
    by VOICE_CATALOG[voice_key]['backend']. Both backends return the
    same tuple shape consumed by ``_back_allocate_timestamps``."""

    @staticmethod
    def _one_segment_script():
        return [{
            "segment": 1,
            "text": "Hello world.",
            "word_count": 2,
            "pause_weight": 1,
            "pause_after_ms": 0,
        }]

    def test_dispatches_to_elevenlabs_when_backend_is_elevenlabs(self, tmp_path):
        from promo.core.narrate import tts_engine

        fake_path = tmp_path / "seg_01.mp3"
        # write a minimal valid mp3 so ffprobe/ffmpeg don't explode
        fake_path.write_bytes(b"\xff\xfb\x90\x64" + b"\x00" * 4096)

        def _fake_el(text, voice_id, output_path, *, speed=None):
            Path(output_path).write_bytes(fake_path.read_bytes())
            return 1.0, [
                {"word": "Hello", "start": 0.0, "end": 0.5},
                {"word": "world.", "start": 0.5, "end": 1.0},
            ]

        def _fake_gem(*args, **kwargs):
            raise AssertionError("Gemini generator called for ElevenLabs voice")

        def _fake_ffprobe(path):
            return 1.0

        def _fake_concat(inputs, output_path, **kwargs):
            Path(output_path).write_bytes(fake_path.read_bytes())

        with mock.patch.object(tts_engine, "_generate_segment_audio_elevenlabs", _fake_el), \
             mock.patch.object(tts_engine, "_generate_segment_audio_gemini", _fake_gem), \
             mock.patch.object(tts_engine, "_ffprobe_duration", _fake_ffprobe), \
             mock.patch.object(tts_engine, "_ffmpeg_concat_mp3s", _fake_concat):
            result = tts_engine.generate_narration(
                self._one_segment_script(),
                voice_key="jarnathan",
                output_dir=str(tmp_path),
            )
        assert "word_timestamps" in result
        assert len(result["word_timestamps"]) == 2

    def test_dispatches_to_gemini_when_backend_is_gemini(self, tmp_path):
        from promo.core.narrate import tts_engine

        fake_path = tmp_path / "seg_01.mp3"
        fake_path.write_bytes(b"\xff\xfb\x90\x64" + b"\x00" * 4096)

        def _fake_el(*args, **kwargs):
            raise AssertionError("ElevenLabs generator called for Gemini voice")

        def _fake_gem(text, voice_name, output_path, *, style_prompt=""):
            Path(output_path).write_bytes(fake_path.read_bytes())
            return 1.0, [
                {"word": "Hello", "start": 0.0, "end": 0.5},
                {"word": "world.", "start": 0.5, "end": 1.0},
            ]

        def _fake_ffprobe(path):
            return 1.0

        def _fake_concat(inputs, output_path, **kwargs):
            Path(output_path).write_bytes(fake_path.read_bytes())

        # Pick the first gemini-backed voice key in catalog.
        gemini_voice_key = next(
            k for k, v in tts_engine.VOICE_CATALOG.items() if v["backend"] == "gemini"
        )
        with mock.patch.object(tts_engine, "_generate_segment_audio_elevenlabs", _fake_el), \
             mock.patch.object(tts_engine, "_generate_segment_audio_gemini", _fake_gem), \
             mock.patch.object(tts_engine, "_ffprobe_duration", _fake_ffprobe), \
             mock.patch.object(tts_engine, "_ffmpeg_concat_mp3s", _fake_concat):
            result = tts_engine.generate_narration(
                self._one_segment_script(),
                voice_key=gemini_voice_key,
                output_dir=str(tmp_path),
            )
        assert "word_timestamps" in result
        assert len(result["word_timestamps"]) == 2


class TestGeminiModelFallback:
    """AC4 — primary Gemini model fallback to 2.5-flash on HTTP 404/403."""

    def test_fallback_invoked_exactly_once_on_404(self):
        """When primary raises HTTPError(404), fallback is called with the
        secondary model ID exactly once."""
        import requests

        from promo.core.narrate import tts_engine

        calls: list[str] = []

        def _fake_rest(text, model, voice):
            calls.append(model)
            if model == tts_engine.GEMINI_PRIMARY_MODEL:
                response = mock.Mock()
                response.status_code = 404
                raise requests.HTTPError(response=response)
            # fallback path — return 24kHz PCM silence (bytes)
            return b"\x00\x00" * 24000  # 1s of silence

        with mock.patch("promo.core.narrate.tts_gemini._gemini_tts_rest", _fake_rest):
            pcm, model_used = tts_engine._gemini_tts_with_fallback(
                "hello world", voice="Kore",
            )
        assert calls == [
            tts_engine.GEMINI_PRIMARY_MODEL,
            tts_engine.GEMINI_FALLBACK_MODEL,
        ], f"expected primary-then-fallback, got {calls}"
        assert model_used == tts_engine.GEMINI_FALLBACK_MODEL
        assert pcm

    def test_non_404_errors_propagate_without_fallback(self):
        """HTTP 500 must propagate (no fallback); operator sees the actual error."""
        import requests

        from promo.core.narrate import tts_engine

        def _fake_rest(text, model, voice):
            response = mock.Mock()
            response.status_code = 500
            raise requests.HTTPError(response=response)

        with mock.patch("promo.core.narrate.tts_gemini._gemini_tts_rest", _fake_rest):
            with pytest.raises(requests.HTTPError):
                tts_engine._gemini_tts_with_fallback("hello world", voice="Kore")


class TestGeminiPcmToMp3Format:
    """AC5 — PCM 24kHz → mp3 44.1kHz mono preconvert matches concat
    sample rate. No torchcodec dep."""

    def test_pcm_to_mp3_produces_44100_mono(self, tmp_path):
        from promo.core.narrate import tts_engine

        # Synthetic 0.5s of silence PCM 24kHz 16-bit mono
        pcm_bytes = b"\x00\x00" * 12000
        out_mp3 = tmp_path / "probe.mp3"
        tts_engine._gemini_pcm_to_mp3(pcm_bytes, str(out_mp3))

        assert out_mp3.exists()
        # ffprobe the output
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "stream=sample_rate,channels",
             "-of", "default=noprint_wrappers=1", str(out_mp3)],
            check=True, capture_output=True, text=True,
        )
        assert "sample_rate=44100" in proc.stdout
        assert "channels=1" in proc.stdout

    def test_no_torchcodec_in_requirements(self):
        req_path = REPO_ROOT / "requirements.txt"
        if not req_path.exists():
            pytest.skip("no requirements.txt")
        text = req_path.read_text()
        for line in text.splitlines():
            stripped = line.strip().lower()
            if stripped.startswith("#") or not stripped:
                continue
            # Pin prefix check — reject "torchcodec==..." but allow comments.
            pkg_name = re.split(r"[<>=!~\s]", stripped, maxsplit=1)[0]
            assert pkg_name != "torchcodec", (
                f"torchcodec found in requirements.txt ({line!r}) — AC5 forbids it"
            )


class TestNumericNormalization:
    """Post-E2E bug fix (2026-04-20) — numeric tokens like "900" or
    "$1,900" cannot be MMS_FA-aligned (letter-only vocab). The Gemini
    path spells them out before both the TTS call and the aligner.

    Tests cover the realistic promo-script range: counts, prices, years,
    decimals. Edge cases (zero, commas, negatives) round-tripped."""

    def test_simple_integer(self):
        from promo.core.narrate.tts_engine import _int_to_words

        assert _int_to_words(0) == "zero"
        assert _int_to_words(7) == "seven"
        assert _int_to_words(19) == "nineteen"

    def test_two_digit(self):
        from promo.core.narrate.tts_engine import _int_to_words

        assert _int_to_words(33) == "thirty three"
        assert _int_to_words(40) == "forty"
        assert _int_to_words(90) == "ninety"

    def test_three_digit(self):
        from promo.core.narrate.tts_engine import _int_to_words

        assert _int_to_words(900) == "nine hundred"
        assert _int_to_words(301) == "three hundred one"
        assert _int_to_words(125) == "one hundred twenty five"

    def test_four_digit_and_thousands(self):
        from promo.core.narrate.tts_engine import _int_to_words

        assert _int_to_words(1900) == "one thousand nine hundred"
        assert _int_to_words(2026) == "two thousand twenty six"

    def test_million_range(self):
        from promo.core.narrate.tts_engine import _int_to_words

        assert _int_to_words(1_250_000) == "one million two hundred fifty thousand"

    def test_normalize_promo_script_line(self):
        """The exact pattern that failed on the Hotel Xcaret E2E run."""
        from promo.core.narrate.tts_engine import _normalize_digits_to_words

        before = "This hotel has 900 suites."
        after = _normalize_digits_to_words(before)
        assert "900" not in after
        assert "nine hundred suites" in after

    def test_normalize_price_with_comma(self):
        from promo.core.narrate.tts_engine import _normalize_digits_to_words

        before = "$1,900 a night"
        after = _normalize_digits_to_words(before)
        assert "1,900" not in after
        assert "one thousand nine hundred" in after

    def test_normalize_decimal(self):
        from promo.core.narrate.tts_engine import _normalize_digits_to_words

        before = "2.5 metres of snow"
        after = _normalize_digits_to_words(before)
        assert "2.5" not in after
        assert "two point five metres" in after

    def test_normalize_leaves_non_numeric_untouched(self):
        from promo.core.narrate.tts_engine import _normalize_digits_to_words

        before = "A quiet stretch of coast."
        assert _normalize_digits_to_words(before) == before

    def test_normalized_output_is_mms_fa_alignable(self):
        """Integration-ish: normalized tokens all pass MMS_FA's
        letter-only preprocessor. The regex would strip digits to
        empty, raising ForcedAlignmentError; spelled-out tokens
        survive."""
        from promo.core.narrate.forced_aligner import _preprocess_token
        from promo.core.narrate.tts_engine import _normalize_digits_to_words

        normalized = _normalize_digits_to_words(
            "This hotel has 900 suites and cost $1,900."
        )
        for tok in normalized.split():
            # Strip trailing punctuation to emulate align_words input.
            assert _preprocess_token(tok), (
                f"Normalized token {tok!r} strips to empty — "
                "number-to-words converter missed it"
            )


# ===========================================================================
#  Negative criteria
# ===========================================================================


class TestN1NoGeminiPauseTags:
    """N1 — Gemini pause tags never sent to the API in production modules.
    Spike measured σ up to 719ms variance on ``[medium pause]`` tags."""

    def test_no_pause_tags_in_production_modules(self):
        prod_paths = [
            REPO_ROOT / "promo" / "core" / "narrate" / "tts_engine.py",
            REPO_ROOT / "promo" / "core" / "narrate" / "tts_elevenlabs.py",
            REPO_ROOT / "promo" / "core" / "narrate" / "tts_gemini.py",
            REPO_ROOT / "promo" / "core" / "narrate" / "forced_aligner.py",
            REPO_ROOT / "promo" / "core" / "script" / "pause_budget.py",
            REPO_ROOT / "promo" / "cli" / "compile_promo.py",
        ]
        pause_pattern = re.compile(
            r"\[\s*(?:short|medium|long)\s+pause\s*\]", re.IGNORECASE
        )
        for path in prod_paths:
            if not path.exists():
                continue
            text = path.read_text()
            match = pause_pattern.search(text)
            assert match is None, (
                f"{path.name} contains Gemini pause tag {match.group()!r} — N1 forbids it"
            )


class TestN3BackendBranchCentralization:
    """N3 — ``if backend == 'gemini'`` / ``== 'elevenlabs'`` branches
    live in exactly three places (tts_engine, pause_budget, compile_promo).
    No scattering into the assign stage, remotion_renderer, captions, tests."""

    ALLOWED = {
        REPO_ROOT / "promo" / "core" / "narrate" / "tts_engine.py",
        REPO_ROOT / "promo" / "core" / "script" / "pause_budget.py",
        REPO_ROOT / "promo" / "cli" / "compile_promo.py",
    }

    def test_backend_equality_only_in_allowed_modules(self):
        pattern = re.compile(
            r"""backend\s*==\s*['"](?:gemini|elevenlabs)['"]""", re.VERBOSE
        )
        scan_dirs = [
            REPO_ROOT / "promo" / "core",
            REPO_ROOT / "promo" / "cli",
        ]
        offenders: list[str] = []
        for d in scan_dirs:
            for py_path in d.rglob("*.py"):
                if py_path in self.ALLOWED:
                    continue
                text = py_path.read_text()
                if pattern.search(text):
                    offenders.append(str(py_path.relative_to(REPO_ROOT)))
        assert not offenders, (
            f"backend equality leaked outside allowed dispatch sites: {offenders}"
        )


# ===========================================================================
#  Phase 4 — Persona + WPM wiring (ACs 9, 10)
# ===========================================================================


class TestPersonaGeminiBlock:
    """AC9 — persona yaml gets a ``gemini:`` block populated from the
    Phase 1 voice A/B decision memo."""

    @staticmethod
    def _load():
        import yaml

        # Sprint Arsenal Externalization (Commit 7) relocated personas
        # to promo/arsenal/personas/.
        with (REPO_ROOT / "promo" / "arsenal" / "personas" / "third_person_promo.yaml").open() as f:
            return yaml.safe_load(f)

    def test_gemini_block_present_with_required_keys(self):
        persona = self._load()
        assert "gemini" in persona, "persona yaml missing gemini: block"
        g = persona["gemini"]
        assert "voice" in g and isinstance(g["voice"], str) and g["voice"]
        assert "style_prompt" in g and isinstance(g["style_prompt"], str)
        assert "default_tags" in g and isinstance(g["default_tags"], list)

    def test_gemini_voice_resolves_in_catalog(self):
        """gemini.voice must match a VOICE_CATALOG entry with backend=gemini."""
        from promo.core.narrate.tts_engine import VOICE_CATALOG

        persona = self._load()
        gemini_voice_name = persona["gemini"]["voice"]
        matches = [
            k for k, v in VOICE_CATALOG.items()
            if v.get("backend") == "gemini" and v.get("id") == gemini_voice_name
        ]
        assert matches, (
            f"persona gemini.voice={gemini_voice_name!r} has no matching "
            f"VOICE_CATALOG entry with backend=gemini"
        )

    def test_legacy_voice_id_field_retained(self):
        """Back-compat — the old top-level voice_id field stays present
        (even if empty) so callers reading it don't KeyError."""
        persona = self._load()
        assert "voice_id" in persona


class TestBootstrapWpmDispatch:
    """AC10 — ``OBSERVED_GEMINI_WPM_BOOTSTRAP`` added, ElevenLabs constant
    unchanged, and the dispatch lives in a single named helper so
    downstream modules don't re-branch."""

    def test_elevenlabs_bootstrap_unchanged(self):
        from promo.core.script.pause_budget import OBSERVED_ELEVENLABS_WPM

        assert OBSERVED_ELEVENLABS_WPM == 195

    def test_gemini_bootstrap_in_plausible_range(self):
        """Contract AC10 verify: 120 <= OBSERVED_GEMINI_WPM_BOOTSTRAP <= 170.
        The Sprint TTS-Migration Phase 1 measurement landed at 148 WPM."""
        from promo.core.script.pause_budget import OBSERVED_GEMINI_WPM_BOOTSTRAP

        assert 120 <= OBSERVED_GEMINI_WPM_BOOTSTRAP <= 170

    def test_bootstrap_dispatch_helper_routes_per_backend(self):
        from promo.core.script.pause_budget import (
            OBSERVED_ELEVENLABS_WPM,
            OBSERVED_GEMINI_WPM_BOOTSTRAP,
            bootstrap_wpm_for_backend,
        )

        assert bootstrap_wpm_for_backend("gemini") == OBSERVED_GEMINI_WPM_BOOTSTRAP
        assert bootstrap_wpm_for_backend("elevenlabs") == OBSERVED_ELEVENLABS_WPM

    def test_unknown_backend_falls_back_to_elevenlabs(self):
        """Defensive default so new persona YAMLs don't crash before
        their backend lands in code."""
        from promo.core.script.pause_budget import (
            OBSERVED_ELEVENLABS_WPM,
            bootstrap_wpm_for_backend,
        )

        assert bootstrap_wpm_for_backend("azure") == OBSERVED_ELEVENLABS_WPM
        assert bootstrap_wpm_for_backend("") == OBSERVED_ELEVENLABS_WPM


class TestLoadCalibratedWpmBackendScoped:
    """Phase 5 regression — Gemini-backed prior run was polluting the
    WPM calibration for a subsequent ElevenLabs run (137 WPM applied to
    an ElevenLabs-rendered script that actually runs at ~166 WPM),
    dropping coverage below 85%. Calibration must filter by backend."""

    def test_calibrated_wpm_filters_by_backend(self, tmp_path):
        import json

        from promo.core.script.pause_budget import load_calibrated_wpm

        # Sidecar with mixed Gemini (137) and ElevenLabs (195) entries.
        path = tmp_path / "tts_metrics_hotel_xcaret_arte_30s.json"
        path.write_text(json.dumps([
            {"variant_index": 1, "backend": "gemini", "measured_wpm": 137.0},
            {"variant_index": 2, "backend": "elevenlabs", "measured_wpm": 195.0},
        ]))

        gemini_wpm = load_calibrated_wpm(
            "hotel_xcaret_arte", 30, [str(tmp_path)], backend="gemini",
        )
        el_wpm = load_calibrated_wpm(
            "hotel_xcaret_arte", 30, [str(tmp_path)], backend="elevenlabs",
        )
        assert gemini_wpm == 137
        assert el_wpm == 195

    def test_calibrated_wpm_none_backend_accepts_all_entries(self, tmp_path):
        """Back-compat — pre-migration sidecars had no backend field;
        callers that don't pass backend get the old behavior."""
        import json

        from promo.core.script.pause_budget import load_calibrated_wpm

        path = tmp_path / "tts_metrics_hotel_xcaret_arte_30s.json"
        path.write_text(json.dumps([
            {"variant_index": 1, "measured_wpm": 160.0},
            {"variant_index": 2, "measured_wpm": 200.0},
        ]))
        wpm = load_calibrated_wpm(
            "hotel_xcaret_arte", 30, [str(tmp_path)],
        )
        assert wpm == 180  # mean of 160 and 200

    def test_calibrated_wpm_skips_mismatched_backend_entries(self, tmp_path):
        """When the requested backend has zero matching entries, return
        None (falls through to bootstrap), not the mean of the other
        backend's entries."""
        import json

        from promo.core.script.pause_budget import load_calibrated_wpm

        path = tmp_path / "tts_metrics_hotel_xcaret_arte_30s.json"
        path.write_text(json.dumps([
            {"variant_index": 1, "backend": "gemini", "measured_wpm": 137.0},
        ]))
        wpm = load_calibrated_wpm(
            "hotel_xcaret_arte", 30, [str(tmp_path)], backend="elevenlabs",
        )
        assert wpm is None


class TestCompilePromoRoutingBackend:
    """AC10 continuation — compile_promo routes per-backend WPM via the
    dispatch helper at one site (N3). Source-level check."""

    def test_compile_promo_imports_bootstrap_helper(self):
        """promo-handoff-readiness Sprint 4 A-001 narrow — the per-backend
        WPM bootstrap moved into ``promo.core.pipeline.steps`` alongside
        ``_step_generate_script`` and ``_build_variant_tts_metrics``, which
        were the two consumer sites. The N3 "one module owns the dispatch
        helper" invariant is still enforced here."""
        from promo.core.pipeline import steps as pipeline_steps

        assert hasattr(pipeline_steps, "bootstrap_wpm_for_backend")

    def test_compile_promo_step_generate_script_accepts_resolved_voice_keys(self):
        # S0.5 supersedes the Phase 4 single-backend invariant: WPM
        # bootstrap is now resolved per-variant from the voice rotation,
        # so _step_generate_script consumes the full ``resolved_voice_keys``
        # list instead of a single ``primary_backend`` string.
        import inspect

        from promo.cli.compile_promo import _step_generate_script

        sig = inspect.signature(_step_generate_script)
        assert "resolved_voice_keys" in sig.parameters, (
            "_step_generate_script must accept resolved_voice_keys for "
            "S0.5 per-variant WPM dispatch"
        )
        assert "primary_backend" not in sig.parameters, (
            "primary_backend kwarg removed by S0.5; per-variant rotation "
            "replaces the single run-level backend"
        )


class TestN4GenerateNarrationSignature:
    """N4 — public ``generate_narration`` signature unchanged. Only the
    argument names present at sprint-open: ``segments``, ``voice_id``,
    ``voice_key``, ``output_dir``, ``speed``. The vestigial ``mode`` kwarg
    was dropped in Sprint 13 AC10 / D-004 once the zero-production-caller
    audit confirmed it had never driven prosody."""

    def test_signature_argument_names_stable(self):
        import inspect

        from promo.core.narrate.tts_engine import generate_narration

        sig = inspect.signature(generate_narration)
        expected = {"segments", "voice_id", "voice_key", "output_dir", "speed"}
        actual = set(sig.parameters.keys())
        assert actual == expected, (
            f"generate_narration signature drift: expected {expected}, got {actual}"
        )
