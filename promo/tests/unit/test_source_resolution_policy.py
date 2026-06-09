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


def test_invalid_source_policy_rejects_unknown_mode():
    from promo.core.source_resolution_policy import (
        SourceResolutionPolicyError,
        normalize_source_resolution_policy,
    )

    with pytest.raises(SourceResolutionPolicyError, match="mode"):
        normalize_source_resolution_policy({"mode": "forever_720p"})
