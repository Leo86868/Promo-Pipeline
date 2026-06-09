def test_final_upscale_policy_derives_requirement_from_source_policy():
    from promo.core.final_upscale import normalize_final_upscale_policy

    transition = normalize_final_upscale_policy(
        None,
        source_policy_mode="transition_low_res_only",
    )
    best = normalize_final_upscale_policy(None, source_policy_mode="best_available")

    assert transition.required is True
    assert transition.enabled is True
    assert transition.provider == "wavespeed"
    assert transition.reason == "low_res_source_transition"
    assert best.required is False
    assert best.enabled is False
    assert best.provider == "disabled"


def test_final_upscale_policy_can_be_explicitly_required_but_disabled():
    from promo.core.final_upscale import normalize_final_upscale_policy

    policy = normalize_final_upscale_policy({
        "required": True,
        "enabled": False,
        "provider": "disabled",
    })

    assert policy.required is True
    assert policy.enabled is False
    assert policy.provider == "disabled"


def test_verify_final_upscale_output_accepts_target_dimensions(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from promo.core import final_upscale

    output_path = tmp_path / "upscaled.mp4"
    output_path.write_bytes(b"mp4")

    def fake_run(command, check, capture_output, text):
        assert command[0] == "ffprobe"
        return SimpleNamespace(
            returncode=0,
            stdout='{"streams":[{"width":1080,"height":1920}]}',
            stderr="",
        )

    monkeypatch.setattr(final_upscale.subprocess, "run", fake_run)

    result = final_upscale.verify_final_upscale_output(
        output_path=str(output_path),
        policy=final_upscale.FinalUpscalePolicy(
            required=True,
            enabled=True,
            provider="wavespeed",
        ),
    )

    assert result["verified"] is True
    assert result["width"] == 1080
    assert result["height"] == 1920


def test_verify_final_upscale_output_rejects_wrong_dimensions(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from promo.core import final_upscale

    output_path = tmp_path / "upscaled.mp4"
    output_path.write_bytes(b"mp4")

    def fake_run(command, check, capture_output, text):
        return SimpleNamespace(
            returncode=0,
            stdout='{"streams":[{"width":720,"height":1280}]}',
            stderr="",
        )

    monkeypatch.setattr(final_upscale.subprocess, "run", fake_run)

    result = final_upscale.verify_final_upscale_output(
        output_path=str(output_path),
        policy=final_upscale.FinalUpscalePolicy(
            required=True,
            enabled=True,
            provider="wavespeed",
        ),
    )

    assert result["verified"] is False
    assert result["reason"] == "dimension_mismatch"
    assert result["width"] == 720
    assert result["height"] == 1280


def test_verify_final_upscale_output_fails_closed_when_probe_fails(
    tmp_path,
    monkeypatch,
):
    from types import SimpleNamespace

    from promo.core import final_upscale

    output_path = tmp_path / "upscaled.mp4"
    output_path.write_bytes(b"not a valid video")

    def fake_run(command, check, capture_output, text):
        return SimpleNamespace(returncode=1, stdout="", stderr="invalid data")

    monkeypatch.setattr(final_upscale.subprocess, "run", fake_run)

    result = final_upscale.verify_final_upscale_output(
        output_path=str(output_path),
        policy=final_upscale.FinalUpscalePolicy(
            required=True,
            enabled=True,
            provider="wavespeed",
        ),
    )

    assert result["verified"] is False
    assert result["reason"] == "dimension_probe_failed"
