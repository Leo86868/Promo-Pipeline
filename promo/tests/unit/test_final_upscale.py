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


def test_min_width_mode_is_safe_by_default_no_upscale_rearm():
    """Flip safety (final_upscale.py:51, L-001 fix 2026-06-22): min_width sources
    are ALREADY native >=1080, so a missing/None final_upscale_policy must NOT
    re-arm upscale. Unlike the transition modes, min_width defaults to
    required=False even without an explicit `--final-upscale-provider disabled`
    — closing the silent-rearm trap (re-encoding 1080 / wasted WaveSpeed spend).
    The transition mode still defaults to required=True (unchanged)."""
    from promo.core.final_upscale import normalize_final_upscale_policy

    # Forgetting the explicit off under min_width is now SAFE: no rearm.
    safe = normalize_final_upscale_policy(None, source_policy_mode="min_width")
    assert safe.required is False
    assert safe.enabled is False

    # Transition mode is unchanged — it genuinely needs upscale and still
    # defaults to required=True (guards against regressing the 720 path).
    transition = normalize_final_upscale_policy(
        None, source_policy_mode="transition_low_res_only"
    )
    assert transition.required is True

    # Explicit off remains belt-and-suspenders and still wins under min_width.
    off = normalize_final_upscale_policy(
        {"required": False, "enabled": False, "provider": "disabled"},
        source_policy_mode="min_width",
    )
    assert off.required is False
    assert off.enabled is False
    assert off.provider == "disabled"


def test_verify_final_upscale_output_accepts_target_dimensions(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from promo.core import final_upscale

    output_path = tmp_path / "upscaled.mp4"
    output_path.write_bytes(b"mp4")

    def fake_run(command, check, capture_output, text):
        assert command[0] == "ffprobe"
        return SimpleNamespace(
            returncode=0,
            stdout=(
                '{"streams":[{"codec_type":"video","width":1080,"height":1920},'
                '{"codec_type":"audio"}],"format":{"duration":"65.04"}}'
            ),
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
        expected_duration_sec=65.0,
    )

    assert result["verified"] is True
    assert result["width"] == 1080
    assert result["height"] == 1920
    assert result["has_audio"] is True
    assert abs(result["duration_sec"] - 65.04) < 0.001


def test_verify_final_upscale_output_rejects_wrong_dimensions(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from promo.core import final_upscale

    output_path = tmp_path / "upscaled.mp4"
    output_path.write_bytes(b"mp4")

    def fake_run(command, check, capture_output, text):
        return SimpleNamespace(
            returncode=0,
            stdout=(
                '{"streams":[{"codec_type":"video","width":720,"height":1280},'
                '{"codec_type":"audio"}],"format":{"duration":"65.0"}}'
            ),
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


def _probe_payload(*, width=1080, height=1920, duration="65.0", audio=True):
    streams = [{"codec_type": "video", "width": width, "height": height}]
    if audio:
        streams.append({"codec_type": "audio"})
    import json as _json
    return _json.dumps({"streams": streams, "format": {"duration": duration}})


def _verify_with_payload(tmp_path, monkeypatch, payload, **kwargs):
    from types import SimpleNamespace

    from promo.core import final_upscale

    output_path = tmp_path / "upscaled.mp4"
    output_path.write_bytes(b"mp4")

    monkeypatch.setattr(
        final_upscale.subprocess,
        "run",
        lambda command, check, capture_output, text: SimpleNamespace(
            returncode=0, stdout=payload, stderr="",
        ),
    )
    return final_upscale.verify_final_upscale_output(
        output_path=str(output_path),
        policy=final_upscale.FinalUpscalePolicy(
            required=True, enabled=True, provider="wavespeed",
        ),
        **kwargs,
    )


def test_verify_final_upscale_output_rejects_missing_audio(tmp_path, monkeypatch):
    """2026-06-10 hardening: a silent MP4 must not pass — PGC masters
    always carry narration + BGM."""
    result = _verify_with_payload(tmp_path, monkeypatch, _probe_payload(audio=False))
    assert result["verified"] is False
    assert result["reason"] == "missing_audio_stream"


def test_verify_final_upscale_output_rejects_duration_mismatch(tmp_path, monkeypatch):
    """2026-06-10 hardening: a truncated file with correct dimensions must
    not be reused on resume."""
    result = _verify_with_payload(
        tmp_path, monkeypatch, _probe_payload(duration="12.3"),
        expected_duration_sec=65.0,
    )
    assert result["verified"] is False
    assert result["reason"] == "duration_mismatch"


def test_verify_final_upscale_output_rejects_zero_duration(tmp_path, monkeypatch):
    result = _verify_with_payload(tmp_path, monkeypatch, _probe_payload(duration="0"))
    assert result["verified"] is False
    assert result["reason"] == "missing_duration"


def test_command_upscaler_captures_cli_json_details(tmp_path, monkeypatch):
    """2026-06-10 review fix: the wavespeed CLI's JSON result line
    (prediction_id, source_host, resumed, ...) lands in the receipt."""
    from types import SimpleNamespace

    from promo.core import final_upscale

    def fake_run(command, check, capture_output, text):
        return SimpleNamespace(
            returncode=0,
            stdout='log line\n{"prediction_id": "pred-9", "source_host": "supabase"}\n',
            stderr="",
        )

    monkeypatch.setattr(final_upscale.subprocess, "run", fake_run)
    upscaler = final_upscale.CommandFinalVideoUpscaler(
        command_template="upscale --input {input_path} --output {output_path}",
    )
    result = upscaler.upscale(
        input_path=str(tmp_path / "in.mp4"),
        output_path=str(tmp_path / "out.mp4"),
    )
    assert result["status"] == "applied"
    assert result["details"] == {"prediction_id": "pred-9", "source_host": "supabase"}


def test_command_upscaler_preflight_appends_flag_and_fails_closed(
    tmp_path, monkeypatch,
):
    """2026-06-10 review fix: preflight() runs the command with --preflight
    and raises on a non-zero exit so run_batch fails before rendering."""
    from types import SimpleNamespace

    from promo.core import final_upscale

    seen = {}

    def fake_run(command, check, capture_output, text):
        seen["command"] = command
        return SimpleNamespace(
            returncode=1,
            stdout='{"preflight": "failed", "errors": ["WAVESPEED_API_KEY missing"]}',
            stderr="",
        )

    monkeypatch.setattr(final_upscale.subprocess, "run", fake_run)
    upscaler = final_upscale.CommandFinalVideoUpscaler(
        command_template="upscale --input {input_path} --output {output_path}",
    )
    import pytest as _pytest
    with _pytest.raises(final_upscale.FinalUpscaleError, match="WAVESPEED_API_KEY"):
        upscaler.preflight()
    assert seen["command"][-1] == "--preflight"
