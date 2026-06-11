def test_required_active_assets_adds_buffer_per_extra_variation():
    # P2 step 3: the floor knobs come from the format card; the long
    # card carries the production values (base 50 + 10 per extra).
    from promo.core.format_profiles import LONG_PROFILE
    from promo.core.run_receipt import required_active_assets

    def gate(videos_per_poi):
        return required_active_assets(
            videos_per_poi,
            base_min_assets_for_format=LONG_PROFILE.assets_base_min,
            extra_variation_asset_buffer=LONG_PROFILE.assets_per_extra,
        )

    assert gate(1) == 50
    assert gate(2) == 60
    assert gate(3) == 70
    assert gate(4) == 80


def test_paradigm_for_duration_uses_rounded_seconds():
    from promo.core.run_receipt import paradigm_for_duration

    assert paradigm_for_duration(65.0) == "pgc_65s"
    assert paradigm_for_duration(119.6) == "pgc_120s"
