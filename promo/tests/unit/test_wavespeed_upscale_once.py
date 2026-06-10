"""Unit tests for promo.cli.wavespeed_upscale_once.

2026-06-09 hardening: (1) the WaveSpeed source upload prefers a private
Supabase Storage bucket + signed URL over the legacy public temp hosts;
(2) a submitted prediction_id is persisted next to the output so that
run_batch's whole-command retries resume polling instead of paying for a
fresh prediction. All HTTP is mocked — no network in this module.
"""

import json
from types import SimpleNamespace

import pytest

from promo.cli import wavespeed_upscale_once as ws


FAKE_SHA = "a" * 64


def _completed_poll(prediction_id="pred-1", output_url="https://ws.example/out.mp4"):
    def _poll(pid, *, max_wait, poll_interval):
        assert pid == prediction_id
        return output_url
    return _poll


def _stub_tail(monkeypatch, *, width=1080, height=1920):
    """Stub download + ffprobe so upscale_once can complete."""
    def _fake_download(url, output_path):
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"mp4")
        return 3
    monkeypatch.setattr(ws, "_download", _fake_download)
    monkeypatch.setattr(
        ws, "_probe_dimensions", lambda path: {"width": width, "height": height},
    )


class TestSupabaseSourceHost:
    def test_supabase_preferred_when_creds_exist(self, tmp_path, monkeypatch):
        """auto mode + creds → upload to private bucket, submit the signed
        URL, clean up the staged object on success."""
        monkeypatch.setenv("SUPABASE_URL", "https://proj.supabase.co")
        monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "svc-key")
        monkeypatch.setenv("WAVESPEED_API_KEY", "ws-key")

        input_path = tmp_path / "master.mp4"
        input_path.write_bytes(b"video-bytes")
        output_path = tmp_path / "out" / "upscaled.mp4"

        calls = {"posts": [], "deletes": []}

        def fake_post(url, **kwargs):
            calls["posts"].append(url)
            if url.endswith("/storage/v1/bucket"):
                return SimpleNamespace(
                    status_code=409, text="already exists", ok=False,
                    raise_for_status=lambda: None, json=lambda: {},
                )
            if "/storage/v1/object/sign/" in url:
                return SimpleNamespace(
                    status_code=200, text="", ok=True,
                    raise_for_status=lambda: None,
                    json=lambda: {"signedURL": "/object/sign/b/p?token=t"},
                )
            if "/storage/v1/object/" in url:
                return SimpleNamespace(
                    status_code=200, text="", ok=True,
                    raise_for_status=lambda: None, json=lambda: {},
                )
            if url.endswith("/wavespeed-ai/video-upscaler"):
                # The submitted video URL must be the signed Supabase URL.
                assert kwargs["json"]["video"] == (
                    "https://proj.supabase.co/storage/v1/object/sign/b/p?token=t"
                )
                return SimpleNamespace(
                    status_code=200, text="", ok=True,
                    raise_for_status=lambda: None,
                    json=lambda: {"data": {"id": "pred-1"}},
                )
            raise AssertionError(f"unexpected POST {url}")

        def fake_delete(url, **kwargs):
            calls["deletes"].append(url)
            return SimpleNamespace(ok=True)

        monkeypatch.setattr(ws.requests, "post", fake_post)
        monkeypatch.setattr(ws.requests, "delete", fake_delete)
        monkeypatch.setattr(ws, "_poll_wavespeed", _completed_poll())
        _stub_tail(monkeypatch)

        result = ws.upscale_once(
            input_path=input_path,
            output_path=output_path,
            target_resolution="1080p",
            min_width=1080,
            min_height=1920,
            max_wait=60,
            poll_interval=1,
            source_host="auto",
        )

        assert result["source_host"] == "supabase"
        assert result["temp_host"] is None
        assert result["resumed"] is False
        assert result["staging_object_deleted"] is True
        # Upload landed in the default staging bucket, keyed by input sha.
        upload_urls = [u for u in calls["posts"] if "/storage/v1/object/pgc-upscale-staging/" in u]
        assert len(upload_urls) == 1
        assert calls["deletes"] and "pgc-upscale-staging" in calls["deletes"][0]
        # Success removes the resume state file.
        assert not ws._state_path(output_path).exists()

    def test_auto_without_creds_falls_back_to_temp_with_warning(
        self, tmp_path, monkeypatch, capsys,
    ):
        monkeypatch.delenv("SUPABASE_URL", raising=False)
        monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
        monkeypatch.delenv("SUPABASE_KEY", raising=False)
        monkeypatch.delenv("SUPABASE_ANON_KEY", raising=False)

        input_path = tmp_path / "master.mp4"
        input_path.write_bytes(b"video-bytes")

        monkeypatch.setattr(
            ws, "_upload_to_temp",
            lambda path: ("uguu", "https://uguu.example/f.mp4", []),
        )
        info = ws._resolve_source_url(
            input_path, source_host="auto", input_sha256=FAKE_SHA, signed_ttl_sec=60,
        )
        assert info["host"] == "uguu"
        assert info["staging_object"] is None
        assert "PUBLIC" in capsys.readouterr().err

    def test_explicit_supabase_without_creds_raises(self, tmp_path, monkeypatch):
        monkeypatch.delenv("SUPABASE_URL", raising=False)
        input_path = tmp_path / "master.mp4"
        input_path.write_bytes(b"video-bytes")
        with pytest.raises(RuntimeError, match="SUPABASE_URL"):
            ws._resolve_source_url(
                input_path, source_host="supabase",
                input_sha256=FAKE_SHA, signed_ttl_sec=60,
            )

    def test_anon_key_is_rejected_as_storage_credential(self, monkeypatch):
        """Anon keys cannot create/upload/sign/delete Storage objects — they
        must NOT make auto mode believe Supabase is usable (the failure
        would otherwise surface after the render is already sunk)."""
        monkeypatch.setenv("SUPABASE_URL", "https://proj.supabase.co")
        monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
        monkeypatch.delenv("SUPABASE_KEY", raising=False)
        monkeypatch.setenv("SUPABASE_ANON_KEY", "anon-key")
        assert ws._supabase_creds() is None
        monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "svc-key")
        assert ws._supabase_creds() == ("https://proj.supabase.co", "svc-key")


class TestResumeState:
    def _write_state(self, output_path, input_sha256, prediction_id="pred-1"):
        ws._write_resume_state(
            ws._state_path(output_path),
            input_sha256=input_sha256,
            prediction_id=prediction_id,
            source_host="supabase",
            staging_object="pgc-upscale-staging/abc/master.mp4",
        )

    def test_retry_resumes_existing_prediction_without_resubmitting(
        self, tmp_path, monkeypatch,
    ):
        """The money test: state file + matching input → NO new upload, NO
        new paid submission; poll the persisted prediction and download."""
        input_path = tmp_path / "master.mp4"
        input_path.write_bytes(b"video-bytes")
        output_path = tmp_path / "upscaled.mp4"
        sha = ws._sha256_file(input_path)
        self._write_state(output_path, sha)

        def _boom(*args, **kwargs):
            raise AssertionError("must not upload or submit on resume")
        monkeypatch.setattr(ws, "_resolve_source_url", _boom)
        monkeypatch.setattr(ws, "_submit_wavespeed", _boom)
        monkeypatch.setattr(ws, "_poll_wavespeed", _completed_poll())
        monkeypatch.setattr(ws, "_delete_supabase_object", lambda ref: True)
        _stub_tail(monkeypatch)

        result = ws.upscale_once(
            input_path=input_path,
            output_path=output_path,
            target_resolution="1080p",
            min_width=1080,
            min_height=1920,
            max_wait=60,
            poll_interval=1,
        )
        assert result["resumed"] is True
        assert result["prediction_id"] == "pred-1"
        assert not ws._state_path(output_path).exists()

    def test_stale_state_for_different_input_is_ignored(self, tmp_path, monkeypatch):
        input_path = tmp_path / "master.mp4"
        input_path.write_bytes(b"new-render-bytes")
        output_path = tmp_path / "upscaled.mp4"
        # State written for a DIFFERENT input hash.
        self._write_state(output_path, "b" * 64, prediction_id="old-pred")

        submitted = {}

        def fake_submit(url, *, target_resolution):
            submitted["url"] = url
            return "pred-2"
        monkeypatch.setattr(
            ws, "_resolve_source_url",
            lambda *a, **k: {
                "host": "supabase", "url": "https://signed.example/x",
                "staging_object": "pgc-upscale-staging/x/master.mp4",
                "temp_host_errors": [],
            },
        )
        monkeypatch.setattr(ws, "_submit_wavespeed", fake_submit)
        monkeypatch.setattr(ws, "_poll_wavespeed", _completed_poll("pred-2"))
        monkeypatch.setattr(ws, "_delete_supabase_object", lambda ref: True)
        _stub_tail(monkeypatch)

        result = ws.upscale_once(
            input_path=input_path,
            output_path=output_path,
            target_resolution="1080p",
            min_width=1080,
            min_height=1920,
            max_wait=60,
            poll_interval=1,
        )
        assert result["resumed"] is False
        assert result["prediction_id"] == "pred-2"
        assert submitted["url"] == "https://signed.example/x"

    def test_failed_prediction_falls_back_to_fresh_submission(
        self, tmp_path, monkeypatch,
    ):
        input_path = tmp_path / "master.mp4"
        input_path.write_bytes(b"video-bytes")
        output_path = tmp_path / "upscaled.mp4"
        sha = ws._sha256_file(input_path)
        self._write_state(output_path, sha, prediction_id="dead-pred")

        poll_calls = []

        def fake_poll(pid, *, max_wait, poll_interval):
            poll_calls.append(pid)
            if pid == "dead-pred":
                raise RuntimeError("WaveSpeed failed: model error")
            return "https://ws.example/out.mp4"
        monkeypatch.setattr(ws, "_poll_wavespeed", fake_poll)
        monkeypatch.setattr(
            ws, "_resolve_source_url",
            lambda *a, **k: {
                "host": "supabase", "url": "https://signed.example/x",
                "staging_object": None, "temp_host_errors": [],
            },
        )
        monkeypatch.setattr(ws, "_submit_wavespeed", lambda url, *, target_resolution: "pred-fresh")
        _stub_tail(monkeypatch)

        result = ws.upscale_once(
            input_path=input_path,
            output_path=output_path,
            target_resolution="1080p",
            min_width=1080,
            min_height=1920,
            max_wait=60,
            poll_interval=1,
        )
        assert poll_calls == ["dead-pred", "pred-fresh"]
        assert result["resumed"] is False
        assert result["prediction_id"] == "pred-fresh"

    def test_poll_timeout_leaves_state_for_next_retry(self, tmp_path, monkeypatch):
        """Timeout propagates (message keeps 'timeout' for run_batch's
        retryable classification) and the state file survives with the
        prediction_id so the retry resumes instead of re-paying."""
        input_path = tmp_path / "master.mp4"
        input_path.write_bytes(b"video-bytes")
        output_path = tmp_path / "upscaled.mp4"

        monkeypatch.setattr(
            ws, "_resolve_source_url",
            lambda *a, **k: {
                "host": "supabase", "url": "https://signed.example/x",
                "staging_object": "pgc-upscale-staging/x/master.mp4",
                "temp_host_errors": [],
            },
        )
        monkeypatch.setattr(ws, "_submit_wavespeed", lambda url, *, target_resolution: "pred-slow")

        def fake_poll(pid, *, max_wait, poll_interval):
            raise TimeoutError(f"WaveSpeed timeout after {max_wait}s: {pid} status=processing")
        monkeypatch.setattr(ws, "_poll_wavespeed", fake_poll)

        with pytest.raises(TimeoutError, match="timeout"):
            ws.upscale_once(
                input_path=input_path,
                output_path=output_path,
                target_resolution="1080p",
                min_width=1080,
                min_height=1920,
                max_wait=60,
                poll_interval=1,
            )
        state = json.loads(ws._state_path(output_path).read_text())
        assert state["prediction_id"] == "pred-slow"
        assert state["input_sha256"] == ws._sha256_file(input_path)

    def test_dimension_failure_keeps_state_for_repay_free_retry(
        self, tmp_path, monkeypatch,
    ):
        input_path = tmp_path / "master.mp4"
        input_path.write_bytes(b"video-bytes")
        output_path = tmp_path / "upscaled.mp4"

        monkeypatch.setattr(
            ws, "_resolve_source_url",
            lambda *a, **k: {
                "host": "supabase", "url": "https://signed.example/x",
                "staging_object": None, "temp_host_errors": [],
            },
        )
        monkeypatch.setattr(ws, "_submit_wavespeed", lambda url, *, target_resolution: "pred-1")
        monkeypatch.setattr(ws, "_poll_wavespeed", _completed_poll())
        _stub_tail(monkeypatch, width=720, height=1280)  # below minimum

        with pytest.raises(RuntimeError, match="below minimum dimensions"):
            ws.upscale_once(
                input_path=input_path,
                output_path=output_path,
                target_resolution="1080p",
                min_width=1080,
                min_height=1920,
                max_wait=60,
                poll_interval=1,
            )
        assert ws._state_path(output_path).exists()


class TestCliSurface:
    def test_parser_default_source_host_is_auto(self, monkeypatch):
        monkeypatch.delenv("PGC_WAVESPEED_SOURCE_HOST", raising=False)
        args = ws._parser().parse_args(["--input", "a.mp4", "--output", "b.mp4"])
        assert args.source_host == "auto"

    def test_parser_source_host_env_override(self, monkeypatch):
        monkeypatch.setenv("PGC_WAVESPEED_SOURCE_HOST", "temp")
        args = ws._parser().parse_args(["--input", "a.mp4", "--output", "b.mp4"])
        assert args.source_host == "temp"


class TestPreflightMode:
    """2026-06-10 review fix: --preflight validates runtime config (API key
    + source-host credentials, incl. --env contents) with no paid calls."""

    def test_preflight_passes_with_full_supabase_config(self, monkeypatch, capsys):
        monkeypatch.setenv("WAVESPEED_API_KEY", "ws-key")
        monkeypatch.setenv("SUPABASE_URL", "https://proj.supabase.co")
        monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "svc-key")
        exit_code = ws.main([
            "--input", "a.mp4", "--output", "b.mp4",
            "--source-host", "supabase", "--preflight",
        ])
        assert exit_code == 0
        result = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert result == {
            "preflight": "passed", "source_host": "supabase", "errors": [],
        }

    def test_preflight_fails_without_wavespeed_key_or_storage_creds(
        self, monkeypatch, capsys,
    ):
        monkeypatch.delenv("WAVESPEED_API_KEY", raising=False)
        monkeypatch.delenv("SUPABASE_URL", raising=False)
        monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
        monkeypatch.delenv("SUPABASE_KEY", raising=False)
        exit_code = ws.main([
            "--input", "a.mp4", "--output", "b.mp4",
            "--source-host", "supabase", "--preflight",
        ])
        assert exit_code == 1
        result = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert result["preflight"] == "failed"
        assert any("WAVESPEED_API_KEY" in e for e in result["errors"])
        assert any("SUPABASE_URL" in e for e in result["errors"])
