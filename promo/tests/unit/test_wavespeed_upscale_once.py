"""Unit tests for promo.cli.wavespeed_upscale_once.

2026-06-09 hardening: (1) the WaveSpeed source upload prefers a private
Supabase Storage bucket + signed URL over the legacy public temp hosts;
(2) a submitted prediction_id is persisted next to the output so that
run_batch's whole-command retries resume polling instead of paying for a
fresh prediction. All HTTP is mocked — no network in this module.

P4 test-health (2026-06-14): these tests stub the REAL external boundaries
(`requests.post/get/delete`, `subprocess.run` for ffprobe, `time`) and let
the real internal flow (_resolve_source_url / _submit_wavespeed /
_poll_wavespeed / _download / _probe_dimensions / _delete_supabase_object)
run, then assert the output contract. They no longer patch those private
steps by name, so refactoring the flow can't silently pass a broken test.
"""

import json
from types import SimpleNamespace

import pytest

from promo.cli import wavespeed_upscale_once as ws


FAKE_SHA = "a" * 64
OUTPUT_URL = "https://ws.example/out.mp4"


class _Resp:
    """Minimal stand-in for a ``requests`` Response.

    Supports both the plain-call sites (poll GET, all POSTs/DELETE) and the
    streaming context-manager site (``with requests.get(stream=True) as r``).
    """

    def __init__(self, *, status_code=200, json_data=None, text="",
                 content=b"mp4-bytes", ok=True):
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}
        self.text = text
        self._content = content
        self.ok = ok

    def raise_for_status(self):
        if self.status_code >= 400:
            err = ws.requests.HTTPError(f"HTTP {self.status_code}")
            err.response = self
            raise err

    def json(self):
        return self._json

    def iter_content(self, chunk_size=1):
        yield self._content

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _install_http(monkeypatch, *, post=None, get=None, delete=None):
    """Stub requests.{post,get,delete} and record every call's URL.

    Each handler is ``(url, **kwargs) -> _Resp``. A missing handler for a
    URL that gets hit raises AssertionError (so an unexpected call is loud,
    not a silent pass). Returns the ``calls`` dict for sequence assertions.
    """
    calls = {"POST": [], "GET": [], "DELETE": []}

    def _wrap(method, handler):
        def _f(url, **kwargs):
            calls[method].append(url)
            if handler is None:
                raise AssertionError(f"unexpected {method} {url}")
            return handler(url, **kwargs)
        return _f

    monkeypatch.setattr(ws.requests, "post", _wrap("POST", post))
    monkeypatch.setattr(ws.requests, "get", _wrap("GET", get))
    monkeypatch.setattr(
        ws.requests, "delete",
        _wrap("DELETE", delete or (lambda url, **kw: _Resp(ok=True))),
    )
    return calls


def _fake_ffprobe(monkeypatch, *, width=1080, height=1920):
    """Stub subprocess.run so the REAL _probe_dimensions parses our JSON."""
    def _run(cmd, **kwargs):
        out = json.dumps({"streams": [{"width": width, "height": height}]})
        return SimpleNamespace(returncode=0, stdout=out, stderr="")
    monkeypatch.setattr(ws.subprocess, "run", _run)


def _supabase_submit_post(pid="pred-1", *, expect_video=None):
    """Handler for the Supabase upload+sign POSTs and the WaveSpeed submit
    POST. Returns realistic response shapes the real code reads."""
    def _post(url, **kwargs):
        if "/storage/v1/object/sign/" in url:
            return _Resp(json_data={"signedURL": "/object/sign/b/p?token=t"})
        if url.endswith("/storage/v1/bucket") or "/storage/v1/object/" in url:
            return _Resp()
        if url.endswith("/wavespeed-ai/video-upscaler"):
            if expect_video is not None:
                assert kwargs["json"]["video"] == expect_video
            return _Resp(json_data={"data": {"id": pid}})
        raise AssertionError(f"unexpected POST {url}")
    return _post


def _poll_get(status_by_pid=None, *, default="completed", output_url=OUTPUT_URL):
    """Handler for the poll GET (``/predictions/<pid>/result``) and the
    download GET. ``status_by_pid`` maps prediction_id → status string."""
    status_by_pid = status_by_pid or {}

    def _get(url, **kwargs):
        if "/predictions/" in url:
            pid = url.split("/predictions/", 1)[1].split("/result", 1)[0]
            status = status_by_pid.get(pid, default)
            if status == "completed":
                return _Resp(json_data={"data": {
                    "status": "completed", "outputs": [output_url]}})
            if status == "failed":
                return _Resp(json_data={"data": {
                    "status": "failed", "error": "model error"}})
            return _Resp(json_data={"data": {"status": status}})
        # Anything else is the download of the finished output.
        return _Resp(content=b"upscaled-mp4")
    return _get


class TestSupabaseSourceHost:
    def test_supabase_preferred_when_creds_exist(self, tmp_path, monkeypatch):
        """auto mode + creds → upload to private bucket, submit the signed
        URL, clean up the staged object on success. Real upload/sign/submit/
        poll/download/probe/delete all run against stubbed HTTP."""
        monkeypatch.setenv("SUPABASE_URL", "https://proj.supabase.co")
        monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "svc-key")
        monkeypatch.setenv("WAVESPEED_API_KEY", "ws-key")

        input_path = tmp_path / "master.mp4"
        input_path.write_bytes(b"video-bytes")
        output_path = tmp_path / "out" / "upscaled.mp4"

        signed = "https://proj.supabase.co/storage/v1/object/sign/b/p?token=t"
        calls = _install_http(
            monkeypatch,
            post=_supabase_submit_post("pred-1", expect_video=signed),
            get=_poll_get(),
        )
        _fake_ffprobe(monkeypatch)

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
        assert result["prediction_id"] == "pred-1"
        assert result["staging_object_deleted"] is True
        assert result["width"] == 1080 and result["height"] == 1920
        # Upload landed in the default staging bucket, keyed by input sha.
        upload_urls = [u for u in calls["POST"]
                       if "/storage/v1/object/pgc-upscale-staging/" in u]
        assert len(upload_urls) == 1
        # Staged source deleted on success.
        assert calls["DELETE"] and "pgc-upscale-staging" in calls["DELETE"][0]
        # Real output file exists; resume state removed.
        assert output_path.exists()
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

        # Stub the temp-host upload boundary (uguu is first in the chain) and
        # let the REAL _upload_to_temp / _resolve_source_url run.
        def _post(url, **kwargs):
            if url == ws.UGUU_URL:
                return _Resp(json_data={
                    "success": True,
                    "files": [{"url": "https://uguu.example/f.mp4"}],
                })
            raise AssertionError(f"unexpected POST {url}")
        _install_http(monkeypatch, post=_post)

        info = ws._resolve_source_url(
            input_path, source_host="auto", input_sha256=FAKE_SHA, signed_ttl_sec=60,
        )
        assert info["host"] == "uguu"
        assert info["url"] == "https://uguu.example/f.mp4"
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
        new paid submission; poll the persisted prediction and download.
        Proven by the absence of any POST (upload/submit) at the boundary."""
        monkeypatch.setenv("SUPABASE_URL", "https://proj.supabase.co")
        monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "svc-key")
        monkeypatch.setenv("WAVESPEED_API_KEY", "ws-key")

        input_path = tmp_path / "master.mp4"
        input_path.write_bytes(b"video-bytes")
        output_path = tmp_path / "upscaled.mp4"
        sha = ws._sha256_file(input_path)
        self._write_state(output_path, sha)

        calls = _install_http(monkeypatch, post=None, get=_poll_get())
        _fake_ffprobe(monkeypatch)

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
        # The money contract: NOT a single POST (no upload, no paid submit).
        assert calls["POST"] == []
        assert not ws._state_path(output_path).exists()

    def test_stale_state_for_different_input_is_ignored(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SUPABASE_URL", "https://proj.supabase.co")
        monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "svc-key")
        monkeypatch.setenv("WAVESPEED_API_KEY", "ws-key")

        input_path = tmp_path / "master.mp4"
        input_path.write_bytes(b"new-render-bytes")
        output_path = tmp_path / "upscaled.mp4"
        # State written for a DIFFERENT input hash → must be ignored.
        self._write_state(output_path, "b" * 64, prediction_id="old-pred")

        calls = _install_http(
            monkeypatch,
            post=_supabase_submit_post("pred-2"),
            get=_poll_get(),
        )
        _fake_ffprobe(monkeypatch)

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
        # A fresh submit POST to WaveSpeed actually happened (stale state path).
        assert any(u.endswith("/wavespeed-ai/video-upscaler") for u in calls["POST"])
        # The fresh prediction was polled, not the stale one.
        assert any("/predictions/pred-2/result" in u for u in calls["GET"])
        assert not any("/predictions/old-pred/result" in u for u in calls["GET"])

    def test_failed_prediction_falls_back_to_fresh_submission(
        self, tmp_path, monkeypatch,
    ):
        monkeypatch.setenv("SUPABASE_URL", "https://proj.supabase.co")
        monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "svc-key")
        monkeypatch.setenv("WAVESPEED_API_KEY", "ws-key")

        input_path = tmp_path / "master.mp4"
        input_path.write_bytes(b"video-bytes")
        output_path = tmp_path / "upscaled.mp4"
        sha = ws._sha256_file(input_path)
        self._write_state(output_path, sha, prediction_id="dead-pred")

        # dead-pred polls as failed → real _try_resume_prediction returns None
        # → real fresh submit yields pred-fresh, which polls completed.
        calls = _install_http(
            monkeypatch,
            post=_supabase_submit_post("pred-fresh"),
            get=_poll_get({"dead-pred": "failed", "pred-fresh": "completed"}),
        )
        _fake_ffprobe(monkeypatch)

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
        assert result["prediction_id"] == "pred-fresh"
        # Polled the dead one first, then the fresh one (resume→fail→fresh).
        poll_preds = [u.split("/predictions/", 1)[1].split("/result", 1)[0]
                      for u in calls["GET"] if "/predictions/" in u]
        assert poll_preds == ["dead-pred", "pred-fresh"]

    def test_poll_timeout_leaves_state_for_next_retry(self, tmp_path, monkeypatch):
        """Timeout propagates (message keeps 'timeout' for run_batch's
        retryable classification) and the state file survives with the
        prediction_id so the retry resumes instead of re-paying."""
        monkeypatch.setenv("SUPABASE_URL", "https://proj.supabase.co")
        monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "svc-key")
        monkeypatch.setenv("WAVESPEED_API_KEY", "ws-key")

        input_path = tmp_path / "master.mp4"
        input_path.write_bytes(b"video-bytes")
        output_path = tmp_path / "upscaled.mp4"

        _install_http(
            monkeypatch,
            post=_supabase_submit_post("pred-slow"),
            get=_poll_get(default="processing"),  # never completes
        )
        # Drive time past the deadline on the first poll check so the REAL
        # _poll_wavespeed raises TimeoutError without real sleeping.
        clock = iter([1000.0, 1000.0, 9999.0])
        monkeypatch.setattr(ws.time, "time", lambda: next(clock, 9999.0))
        monkeypatch.setattr(ws.time, "sleep", lambda _s: None)

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
        monkeypatch.setenv("SUPABASE_URL", "https://proj.supabase.co")
        monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "svc-key")
        monkeypatch.setenv("WAVESPEED_API_KEY", "ws-key")

        input_path = tmp_path / "master.mp4"
        input_path.write_bytes(b"video-bytes")
        output_path = tmp_path / "upscaled.mp4"

        _install_http(
            monkeypatch,
            post=_supabase_submit_post("pred-1"),
            get=_poll_get(),
        )
        _fake_ffprobe(monkeypatch, width=720, height=1280)  # below minimum

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
        # State intentionally kept so a retry resumes the completed prediction.
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
