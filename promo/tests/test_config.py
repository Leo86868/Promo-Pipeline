"""Unit tests for promo.core.config and promo.core.llm.gemini_client."""

import json
import os
import re
import shutil
import sys
import tempfile
from unittest.mock import patch, MagicMock

import pytest

class TestSprint13ConfigResolvers:
    """AC15: gemini_api_key / openrouter_api_key / elevenlabs_api_key
    are typed resolvers in promo.core.config that raise ConfigError on
    missing values and return the whitespace-stripped string on success.
    """

    def test_gemini_api_key_raises_when_missing(self, monkeypatch):
        import pytest
        from promo.core.config import ConfigError, gemini_api_key

        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        with pytest.raises(ConfigError, match="GEMINI_API_KEY is required"):
            gemini_api_key()

    def test_gemini_api_key_returns_value(self, monkeypatch):
        from promo.core.config import gemini_api_key

        monkeypatch.setenv("GEMINI_API_KEY", "test-key-ABC")
        assert gemini_api_key() == "test-key-ABC"

    def test_openrouter_api_key_raises_when_missing(self, monkeypatch):
        import pytest
        from promo.core.config import ConfigError, openrouter_api_key

        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        with pytest.raises(ConfigError, match="OPENROUTER_API_KEY is required"):
            openrouter_api_key()

    def test_openrouter_api_key_strips_whitespace(self, monkeypatch):
        from promo.core.config import openrouter_api_key

        monkeypatch.setenv("OPENROUTER_API_KEY", "  sk-or-TRIMMED  ")
        assert openrouter_api_key() == "sk-or-TRIMMED"

    def test_elevenlabs_api_key_raises_when_missing(self, monkeypatch):
        import pytest
        from promo.core.config import ConfigError, elevenlabs_api_key

        monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
        with pytest.raises(ConfigError, match="ELEVENLABS_API_KEY is required"):
            elevenlabs_api_key()

    def test_elevenlabs_api_key_returns_value(self, monkeypatch):
        from promo.core.config import elevenlabs_api_key

        monkeypatch.setenv("ELEVENLABS_API_KEY", "eleven-XYZ")
        assert elevenlabs_api_key() == "eleven-XYZ"

    def test_migration_sites_no_direct_os_getenv(self):
        """No pre-S2 core module reads the three migrated keys via
        os.getenv directly — all calls route through config resolvers."""
        import os as _os

        core_dir = os.path.join(os.path.dirname(__file__), "..", "core")
        for name in (
            "analyze/clip_analyzer.py",
            "assign/beat_planner.py",
            "assign/packer.py",
            "assign/clip_assignment_sidecar.py",
            "assign/clip_assignment_validator.py",
            "assign/clip_embedder.py",
            "script/script_generator.py",
            "narrate/tts_engine.py",
            "narrate/tts_elevenlabs.py",
            "narrate/tts_gemini.py",
        ):
            with open(os.path.join(core_dir, name), encoding="utf-8") as f:
                src = f.read()
            for key in ("OPENROUTER_API_KEY", "GEMINI_API_KEY", "ELEVENLABS_API_KEY"):
                assert f'os.getenv("{key}"' not in src, (
                    f"{name} still reads {key} via os.getenv — should route "
                    "through promo.core.config.* resolver."
                )

# ---------------------------------------------------------------------------
#  promo.core.llm.gemini_client — the single google.generativeai quarantine
#  module (Pluggability Charter Rule 1).
# ---------------------------------------------------------------------------

class TestSprint13ResolveGeminiModel:
    """``resolve_gemini_model`` replaces the 3× duplicated
    ``configure_gemini`` + ``GenerativeModel`` + ``logger.info`` blocks
    that used to live in the retired Gemini #2 caller and
    ``script_generator``'s ``generate_script_variants`` +
    ``regenerate_single_variant_with_hint``.
    """

    def test_resolver_configures_gemini_with_env_key(self, monkeypatch):
        from unittest.mock import patch
        from promo.core.llm import gemini_client

        gemini_client.reset_for_tests()
        monkeypatch.setenv("GEMINI_API_KEY", "key-ABC-test")
        monkeypatch.delenv("GEMINI_MODEL", raising=False)

        with patch.object(gemini_client.genai, "configure") as mock_configure, \
             patch.object(gemini_client.genai, "GenerativeModel") as mock_model_cls:
            gemini_client.resolve_gemini_model(log_context="unit-test")

        mock_configure.assert_called_once_with(api_key="key-ABC-test")
        mock_model_cls.assert_called_once_with("gemini-2.5-pro")

    def test_resolver_honors_gemini_model_override(self, monkeypatch):
        from unittest.mock import patch
        from promo.core.llm import gemini_client

        gemini_client.reset_for_tests()
        monkeypatch.setenv("GEMINI_API_KEY", "key-XYZ")
        monkeypatch.setenv("GEMINI_MODEL", "gemini-flash-experimental")

        with patch.object(gemini_client.genai, "configure"), \
             patch.object(gemini_client.genai, "GenerativeModel") as mock_model_cls:
            gemini_client.resolve_gemini_model(log_context="unit-test")

        mock_model_cls.assert_called_once_with("gemini-flash-experimental")

    def test_configure_gemini_raises_on_empty_key(self):
        """Sprint 13 post-audit F-8: configure_gemini must refuse to silently
        no-op on an empty api_key (pre-fix it fell back to os.getenv and
        skipped configure silently if neither was set — contradicting the
        AC15 raise-on-missing contract)."""
        import pytest
        from promo.core.llm import gemini_client

        gemini_client.reset_for_tests()
        with pytest.raises(ValueError, match="non-empty api_key"):
            gemini_client.configure_gemini("")
        with pytest.raises(ValueError, match="non-empty api_key"):
            gemini_client.configure_gemini(None)

    def test_reset_for_tests_clears_configured_flag(self, monkeypatch):
        """Sprint 13 post-audit L-003 / F-8: reset_for_tests is the
        sanctioned way for tests to un-set the module-global _configured
        flag, replacing the private-name monkeypatch idiom."""
        from unittest.mock import patch
        from promo.core.llm import gemini_client

        gemini_client.reset_for_tests()
        monkeypatch.setenv("GEMINI_API_KEY", "first-key")
        with patch.object(gemini_client.genai, "configure") as mock_configure:
            gemini_client.configure_gemini("first-key")
            # Second call while _configured=True skips re-configure.
            gemini_client.configure_gemini("second-key")
            assert mock_configure.call_count == 1

        # After reset_for_tests(), the next configure_gemini fires again.
        gemini_client.reset_for_tests()
        with patch.object(gemini_client.genai, "configure") as mock_configure:
            gemini_client.configure_gemini("third-key")
            mock_configure.assert_called_once_with(api_key="third-key")

    def test_configure_gemini_safe_under_concurrent_callers(self):
        """promo-handoff-readiness Sprint 2 AC-L001: ``configure_gemini``
        no longer has an unlocked fast-path read of ``_configured`` — all
        state inspection happens inside ``with _lock:``. Four threads that
        enter ``configure_gemini`` simultaneously must observe exactly one
        ``genai.configure`` call across the group.
        """
        import threading
        from unittest.mock import patch
        from promo.core.llm import gemini_client

        gemini_client.reset_for_tests()

        barrier = threading.Barrier(4)

        with patch.object(gemini_client.genai, "configure") as mock_configure:
            def _call() -> None:
                barrier.wait(timeout=5.0)
                gemini_client.configure_gemini("race-key")

            threads = [threading.Thread(target=_call) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5.0)
                assert not t.is_alive(), "configure_gemini thread hung"

            assert mock_configure.call_count == 1, (
                f"configure_gemini fired {mock_configure.call_count}× under "
                "4 concurrent callers; expected exactly 1 (AC-L001)."
            )

        gemini_client.reset_for_tests()

    def test_configure_gemini_acquires_lock_on_every_call_including_reset_race(self):
        """promo-handoff-readiness Sprint 3 AC-9 (F-13 strengthen): the
        Sprint 2 liveness test (``test_configure_gemini_safe_under_concurrent_callers``)
        does not discriminate the OLD unlocked-fast-path DCL from the NEW
        locked-only path — under OLD code, 4 concurrent callers also
        serialize to ``call_count == 1`` because the inner DCL inside
        ``with _lock:`` catches duplicates.

        The discriminating invariant: **``configure_gemini`` acquires
        ``_lock`` on every invocation, never short-circuiting on an
        unlocked outer read of ``_configured``**. If a revert reintroduced
        the ``if _configured: return`` outer fast-path, calls made while
        ``_configured`` is already ``True`` would bypass the lock — and
        the acquisition count below would drop below the call count.

        This test also exercises a concurrent ``reset_for_tests`` thread
        flipping ``_configured`` mid-race, per the F-13 recommendation,
        so the invariant holds under the exact scenario the audit flagged.
        """
        import threading
        from unittest.mock import patch
        from promo.core.llm import gemini_client

        gemini_client.reset_for_tests()

        acquire_count = [0]
        real_lock = gemini_client._lock

        class _CountingLock:
            # Proxy that delegates to the real module lock but increments
            # a counter on every enter/acquire. Acts as a drop-in
            # replacement for the module-global ``_lock``.
            def __enter__(self):
                acquire_count[0] += 1
                real_lock.acquire()
                return real_lock

            def __exit__(self, exc_type, exc, tb):
                real_lock.release()
                return False

            def acquire(self, *args, **kwargs):
                acquire_count[0] += 1
                return real_lock.acquire(*args, **kwargs)

            def release(self):
                return real_lock.release()

            def locked(self):
                return real_lock.locked()

        # --- Part 1 — serial discriminator. Under OLD unlocked DCL,
        # calls 2 + 3 would short-circuit on the outer unlocked read
        # (because ``_configured`` is True after call 1) and never
        # acquire the lock. Under NEW locked-only code, every call
        # enters the lock block.
        with patch.object(gemini_client, "_lock", _CountingLock()), \
             patch.object(gemini_client.genai, "configure"):
            gemini_client.configure_gemini("key-1")
            gemini_client.configure_gemini("key-2")  # _configured=True on entry
            gemini_client.configure_gemini("key-3")  # _configured=True on entry

        assert acquire_count[0] == 3, (
            f"expected 3 _lock acquisitions for 3 configure_gemini calls; "
            f"got {acquire_count[0]}. Under the OLD unlocked-fast-path DCL, "
            f"calls 2+3 would short-circuit outside the lock and this count "
            f"would drop to 1."
        )

        gemini_client.reset_for_tests()

        # --- Part 2 — concurrent discriminator: 4 callers race with
        # _configured=True on entry. Under OLD unlocked-fast-path DCL, all
        # 4 see _configured=True at the outer check and return WITHOUT
        # acquiring the lock → 0 acquisitions. Under NEW locked-only, all
        # 4 acquire the lock, inner check sees True, return → exactly 4
        # acquisitions. Strict equality.
        #
        # Sprint 3 post-audit L-003 retraction: the earlier Part 2 used a
        # tight-loop reset_spammer thread whose own lock acquisitions
        # trivially satisfied ``acquire_count >= 4`` regardless of
        # configure short-circuit behavior — the test did not discriminate.
        # This revision removes the spammer and primes _configured=True
        # before the race so the discrimination is direct.
        with patch.object(gemini_client.genai, "configure"):
            gemini_client.configure_gemini("priming-key")
        assert gemini_client._configured is True, (
            "priming step must leave _configured=True"
        )

        acquire_count[0] = 0
        barrier = threading.Barrier(4)

        def _configure_caller() -> None:
            barrier.wait(timeout=5.0)
            gemini_client.configure_gemini("race-key")

        with patch.object(gemini_client, "_lock", _CountingLock()), \
             patch.object(gemini_client.genai, "configure"):
            threads = [threading.Thread(target=_configure_caller) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5.0)
                assert not t.is_alive(), "configure_gemini thread hung"

        assert acquire_count[0] == 4, (
            f"expected exactly 4 _lock acquisitions for 4 concurrent "
            f"configure_gemini calls with _configured=True on entry; got "
            f"{acquire_count[0]}. Under the OLD unlocked-fast-path DCL, "
            f"all 4 calls short-circuit at the outer unlocked read and "
            f"acquire_count would be 0 — this strict-equality assertion "
            f"discriminates OLD vs NEW."
        )

        # --- Part 3 — liveness under concurrent reset race (audit-suggested
        # shape, kept as a liveness-only smoke since it doesn't discriminate
        # OLD from NEW by lock-count alone). The invariant here is that
        # the API stays deadlock-free when reset and configure interleave.
        gemini_client.reset_for_tests()
        stop_reset = threading.Event()
        barrier = threading.Barrier(5)

        def _race_configure() -> None:
            barrier.wait(timeout=5.0)
            gemini_client.configure_gemini("race-key")

        def _race_reset() -> None:
            barrier.wait(timeout=5.0)
            while not stop_reset.is_set():
                gemini_client.reset_for_tests()

        with patch.object(gemini_client.genai, "configure"):
            config_threads = [threading.Thread(target=_race_configure) for _ in range(4)]
            reset_thread = threading.Thread(target=_race_reset)
            reset_thread.start()
            for t in config_threads:
                t.start()
            for t in config_threads:
                t.join(timeout=5.0)
                assert not t.is_alive(), "configure_gemini thread hung under reset race"
            stop_reset.set()
            reset_thread.join(timeout=5.0)
            assert not reset_thread.is_alive(), "reset_for_tests thread hung"

        gemini_client.reset_for_tests()


class TestScriptLlmProvider:
    """PROMO_SCRIPT_LLM_PROVIDER resolver — default gemini, switch to
    openrouter, fail-loud on unknown (billing-failover lane)."""

    def test_default_is_gemini(self, monkeypatch):
        from promo.core.config import script_llm_provider

        monkeypatch.delenv("PROMO_SCRIPT_LLM_PROVIDER", raising=False)
        assert script_llm_provider() == "gemini"

    def test_openrouter_selected(self, monkeypatch):
        from promo.core.config import script_llm_provider

        for raw in ("openrouter", "OpenRouter", "  OPENROUTER  "):
            monkeypatch.setenv("PROMO_SCRIPT_LLM_PROVIDER", raw)
            assert script_llm_provider() == "openrouter"

    def test_gemini_selected_explicitly(self, monkeypatch):
        from promo.core.config import script_llm_provider

        monkeypatch.setenv("PROMO_SCRIPT_LLM_PROVIDER", "gemini")
        assert script_llm_provider() == "gemini"

    def test_unknown_provider_raises(self, monkeypatch):
        from promo.core.config import ConfigError, script_llm_provider

        monkeypatch.setenv("PROMO_SCRIPT_LLM_PROVIDER", "anthropic")
        with pytest.raises(ConfigError, match="PROMO_SCRIPT_LLM_PROVIDER must be one of"):
            script_llm_provider()


class TestDbFirstAssignmentFlags:
    """DB-first assignment + bridge-reserve config resolvers (default OFF)."""

    def test_db_first_default_off(self, monkeypatch):
        from promo.core.config import db_first_assignment_enabled

        monkeypatch.delenv("PROMO_DB_FIRST_ASSIGNMENT", raising=False)
        assert db_first_assignment_enabled() is False

    def test_db_first_armed_truthy_values(self, monkeypatch):
        from promo.core.config import db_first_assignment_enabled

        for raw in ("1", "true", "YES", "On"):
            monkeypatch.setenv("PROMO_DB_FIRST_ASSIGNMENT", raw)
            assert db_first_assignment_enabled() is True

    def test_bridge_reserve_count_default_none(self, monkeypatch):
        from promo.core.config import bridge_reserve_count

        monkeypatch.delenv("PROMO_BRIDGE_RESERVE_COUNT", raising=False)
        assert bridge_reserve_count() is None

    def test_bridge_reserve_count_parses_int(self, monkeypatch):
        from promo.core.config import bridge_reserve_count

        monkeypatch.setenv("PROMO_BRIDGE_RESERVE_COUNT", "12")
        assert bridge_reserve_count() == 12

    def test_bridge_reserve_count_rejects_non_int(self, monkeypatch):
        from promo.core.config import ConfigError, bridge_reserve_count

        monkeypatch.setenv("PROMO_BRIDGE_RESERVE_COUNT", "abc")
        with pytest.raises(ConfigError, match="must be an integer"):
            bridge_reserve_count()

    def test_bridge_reserve_count_rejects_negative(self, monkeypatch):
        from promo.core.config import ConfigError, bridge_reserve_count

        monkeypatch.setenv("PROMO_BRIDGE_RESERVE_COUNT", "-1")
        with pytest.raises(ConfigError, match="must be >= 0"):
            bridge_reserve_count()
