def test_required_active_assets_adds_buffer_per_extra_variation():
    from promo.core.run_receipt import required_active_assets

    assert required_active_assets(1) == 50
    assert required_active_assets(2) == 60
    assert required_active_assets(3) == 70
    assert required_active_assets(4) == 80


def test_paradigm_for_duration_uses_rounded_seconds():
    from promo.core.run_receipt import paradigm_for_duration

    assert paradigm_for_duration(65.0) == "pgc_65s"
    assert paradigm_for_duration(119.6) == "pgc_120s"
