import pytest


def test_transition_low_res_policy_uses_width_and_vertical_ratio():
    from promo.core.source_resolution_policy import source_resolution_matches

    policy = {
        "mode": "transition_low_res_only",
        "target_width": 720,
        "tolerance_px": 40,
    }

    assert source_resolution_matches({"width": 720, "height": 1280}, policy)
    assert source_resolution_matches({"width": 704, "height": 1248}, policy)
    assert not source_resolution_matches({"width": 1080, "height": 1920}, policy)
    assert not source_resolution_matches({"width": 720, "height": 720}, policy)


def test_min_width_defaults_target_width_to_1080_not_720():
    # Latent nit: min_width without an explicit target_width must default to the
    # native-1080 floor, NOT the 720 transition target (opposite intents). A
    # missing target_width that silently became 720 would let 720p clips pass.
    from promo.core.source_resolution_policy import (
        normalize_source_resolution_policy,
    )

    policy = normalize_source_resolution_policy({"mode": "min_width"})
    assert policy.target_width == 1080
    # And it really gates as a 1080 floor: 720 rejected, 1080 accepted.
    from promo.core.source_resolution_policy import source_resolution_matches

    assert not source_resolution_matches({"width": 720, "height": 1280}, policy)
    assert source_resolution_matches({"width": 1080, "height": 1920}, policy)


def test_min_width_is_a_floor_not_a_band():
    from promo.core.source_resolution_policy import source_resolution_matches

    policy = {"mode": "min_width", "target_width": 1080}

    # At/above the 1080 floor, 9:16 sources pass...
    assert source_resolution_matches({"width": 1080, "height": 1920}, policy)
    assert source_resolution_matches({"width": 1088, "height": 1920}, policy)
    # ...including widths far ABOVE 1080 — proving it is a floor with no upper
    # bound, NOT a symmetric width_band (which would reject these).
    assert source_resolution_matches({"width": 1440, "height": 2560}, policy)
    assert source_resolution_matches({"width": 2160, "height": 3840}, policy)
    # Below the floor is rejected (the 720-tier sources).
    assert not source_resolution_matches({"width": 720, "height": 1280}, policy)
    assert not source_resolution_matches({"width": 704, "height": 1248}, policy)
    # The 9:16 sub-gate still applies above the floor (a square 1080 fails).
    assert not source_resolution_matches({"width": 1080, "height": 1080}, policy)


def test_width_band_rejects_above_band_unlike_min_width():
    """Guards the semantic difference the flip depends on: today width_band@1080
    'happens to work' only because 0 assets are >1088; min_width is the correct
    floor that stays right if native 1280/1440 ever lands."""
    from promo.core.source_resolution_policy import source_resolution_matches

    band = {"mode": "width_band", "target_width": 1080, "tolerance_px": 40}
    assert source_resolution_matches({"width": 1088, "height": 1920}, band)
    assert not source_resolution_matches({"width": 1440, "height": 2560}, band)


def test_invalid_source_policy_rejects_unknown_mode():
    from promo.core.source_resolution_policy import (
        SourceResolutionPolicyError,
        normalize_source_resolution_policy,
    )

    with pytest.raises(SourceResolutionPolicyError, match="mode"):
        normalize_source_resolution_policy({"mode": "forever_720p"})
