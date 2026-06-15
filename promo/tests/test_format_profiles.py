"""Unit tests for promo.core.format_profiles."""

import json
import os
import re
import shutil
import sys
import tempfile
from unittest.mock import patch, MagicMock

from pathlib import Path

import pytest

class TestPromoFormatProfiles:
    """Duration profiles should map short and long promo modes consistently."""

    def test_short_and_long_profiles(self):
        from promo.core.format_profiles import get_promo_format_profile

        short_profile = get_promo_format_profile(30)
        long_profile = get_promo_format_profile(65)

        assert short_profile.segment_count == 4
        assert short_profile.mode == "short"
        assert long_profile.segment_count == 5
        assert long_profile.mode == "long"
        assert long_profile.total_words_min == 145
        assert long_profile.total_words_max == 170
        assert long_profile.min_clip_pool_size == 14
        assert long_profile.recommended_clip_pool_size == 18
        # Sprint 08: total_clips_* are derived from segment_plans.
        assert long_profile.total_clips_min == sum(
            sp.min_clips for sp in long_profile.segment_plans
        )
        assert long_profile.total_clips_max == sum(
            sp.max_clips for sp in long_profile.segment_plans
        )
        # SHORT profile parity check.
        assert short_profile.total_clips_min == sum(
            sp.min_clips for sp in short_profile.segment_plans
        )
        assert short_profile.total_clips_max == sum(
            sp.max_clips for sp in short_profile.segment_plans
        )

    def test_long_clip_pool_messages(self):
        from promo.core.format_profiles import get_clip_pool_messages, get_promo_format_profile

        long_profile = get_promo_format_profile(65)

        errors, warnings = get_clip_pool_messages(12, long_profile)
        assert errors == ["long format requires at least 14 unique clips; found 12"]
        assert warnings == []

        errors, warnings = get_clip_pool_messages(16, long_profile)
        assert errors == []
        assert warnings == ["long format works best with 18+ unique clips; found 16"]

class TestSprint08FormatProfilesDerived:
    """PromoFormatProfile.total_clips_* derive from segment_plans (Item 4)."""

    def test_total_clips_match_sum_of_segment_plans(self):
        from promo.core.format_profiles import SHORT_PROFILE, LONG_PROFILE
        for p in (SHORT_PROFILE, LONG_PROFILE):
            assert p.total_clips_min == sum(sp.min_clips for sp in p.segment_plans)
            assert p.total_clips_max == sum(sp.max_clips for sp in p.segment_plans)

    def test_segmentplan_clip_range_display(self):
        from promo.core.format_profiles import SegmentPlan
        sp = SegmentPlan("HOOK", 20, 2, 3, "x")
        assert sp.clip_range_display == "2-3 clips"
        sp_one = SegmentPlan("CLOSE", 10, 1, 1, "x")
        assert sp_one.clip_range_display == "1 clip"


class TestP2DurationRouting:
    """P2 step 2: routing is an exact duration → card lookup. Pins the
    full real input domain (None / 30 / 65, int and float forms) to the
    pre-P2 behavior, and pins the intentional change: an unknown
    duration now fails loudly instead of silently falling into a
    threshold bucket."""

    def test_real_input_domain_routes_unchanged(self):
        from promo.core.format_profiles import get_promo_format_profile

        assert get_promo_format_profile(None).mode == "short"
        assert get_promo_format_profile(30).mode == "short"
        assert get_promo_format_profile(30.0).mode == "short"
        assert get_promo_format_profile(65).mode == "long"
        assert get_promo_format_profile(65.0).mode == "long"

    def test_unknown_duration_fails_loud_listing_deck(self):
        from promo.core.format_profiles import get_promo_format_profile

        with pytest.raises(ValueError, match=r"known durations: \[30, 65\]"):
            get_promo_format_profile(50)


class TestP2CardIsSoleSource:
    """P2 step 3: the personality constants are deleted — the card is
    the only source. Guard the consumer signatures so a module default
    can't silently reappear and become a second source of truth."""

    def test_consumers_have_no_personality_defaults(self):
        import inspect

        from promo.core.assign.beat_planner import plan_beats
        from promo.core.assign.packer import pack_clips
        from promo.core.run_receipt import required_active_assets
        from promo.core.script.pause_budget import compute_pause_budget

        required = {
            plan_beats: ("max_beat_sec", "min_beat_sec"),
            pack_clips: ("max_beat_sec",),
            compute_pause_budget: ("pause_cap_ms",),
            required_active_assets: (
                "base_min_assets_for_format", "extra_variation_asset_buffer",
            ),
        }
        for fn, params in required.items():
            sig = inspect.signature(fn)
            for name in params:
                assert sig.parameters[name].default is inspect.Parameter.empty, (
                    f"{fn.__name__}({name}=...) grew a default — the card "
                    "must stay the only source"
                )

    def test_deleted_constants_stay_deleted(self):
        from promo.core import run_receipt
        from promo.core.assign import beat_planner
        from promo.core.script import pause_budget

        for mod, name in (
            (beat_planner, "DEFAULT_MAX_BEAT_SEC"),
            (beat_planner, "DEFAULT_MIN_BEAT_SEC"),
            (pause_budget, "PER_GAP_CAP_MS"),
            (run_receipt, "DEFAULT_BASE_MIN_ASSETS_FOR_FORMAT"),
            (run_receipt, "DEFAULT_EXTRA_VARIATION_ASSET_BUFFER"),
        ):
            assert not hasattr(mod, name), f"{mod.__name__}.{name} resurrected"

    def test_card_descriptions_are_distinct_nonempty(self):
        from promo.core.format_profiles import SHORT_PROFILE, LONG_PROFILE

        assert SHORT_PROFILE.description
        assert LONG_PROFILE.description
        assert SHORT_PROFILE.description != LONG_PROFILE.description

