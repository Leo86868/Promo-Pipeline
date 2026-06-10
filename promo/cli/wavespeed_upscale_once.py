#!/usr/bin/env python3
"""Upscale one local MP4 with WaveSpeed and write a local MP4 output.

Source-URL policy (2026-06-09): WaveSpeed fetches the input by URL. The
preferred source host is Supabase Storage (private bucket + time-limited
signed URL) — production masters must not transit public anonymous file
hosts. The legacy temp-host chain (uguu/tmpfiles/litterbox) remains only
as an explicit/auto fallback for environments without Supabase
credentials; ``--source-host`` controls the choice.

Resume policy (2026-06-09): a submitted prediction_id is persisted to
``<output>.wavespeed_state.json`` keyed by the input's sha256. If this
CLI is re-invoked for the same input/output (run_batch retries the whole
command on timeout), it resumes polling the existing prediction instead
of paying for a fresh submission. The state file is removed on success;
a failed/expired prediction falls back to a fresh submission.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

import requests

WAVESPEED_BASE_URL = "https://api.wavespeed.ai/api/v3"
UGUU_URL = "https://uguu.se/upload.php"
TMPFILES_URL = "https://tmpfiles.org/api/v1/upload"
LITTERBOX_URL = "https://litterbox.catbox.moe/resources.php"

STAGING_BUCKET_ENV = "PGC_WAVESPEED_STAGING_BUCKET"
DEFAULT_STAGING_BUCKET = "pgc-upscale-staging"
# Signed URL must outlive the whole submit+poll window so WaveSpeed can
# fetch the source at any point before the poll deadline.
SIGNED_URL_EXTRA_TTL_SEC = 7200
STATE_SCHEMA_VERSION = 1


def _load_env(path: str | None) -> None:
    if not path:
        return
    env_path = Path(path)
    if not env_path.exists():
        raise FileNotFoundError(f"env file not found: {env_path}")
    for line in env_path.read_text(errors="ignore").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _upload_uguu(input_path: Path) -> str:
    with input_path.open("rb") as fh:
        response = requests.post(
            UGUU_URL,
            files={"files[]": (input_path.name, fh)},
            timeout=300,
        )
    response.raise_for_status()
    payload = response.json()
    files = payload.get("files") or []
    url = files[0].get("url") if files else None
    if not payload.get("success") or not url:
        raise RuntimeError(f"uguu returned unexpected response: {response.text[:200]}")
    return str(url)


def _upload_tmpfiles(input_path: Path) -> str:
    with input_path.open("rb") as fh:
        response = requests.post(
            TMPFILES_URL,
            files={"file": (input_path.name, fh)},
            timeout=300,
        )
    response.raise_for_status()
    payload = response.json()
    url = payload.get("data", {}).get("url")
    if payload.get("status") != "success" or not url:
        raise RuntimeError(f"tmpfiles returned unexpected response: {response.text[:200]}")
    return str(url).replace("https://tmpfiles.org/", "https://tmpfiles.org/dl/", 1)


def _upload_litterbox(input_path: Path) -> str:
    with input_path.open("rb") as fh:
        response = requests.post(
            LITTERBOX_URL,
            data={"reqtype": "fileupload", "time": "24h"},
            files={"fileToUpload": (input_path.name, fh)},
            timeout=300,
        )
    response.raise_for_status()
    url = response.text.strip()
    if not url.startswith("http"):
        raise RuntimeError(f"litterbox returned unexpected response: {url[:200]}")
    return url


def _upload_to_temp(input_path: Path) -> tuple[str, str, list[dict[str, str]]]:
    errors: list[dict[str, str]] = []
    hosts: list[tuple[str, Callable[[Path], str]]] = [
        ("uguu", _upload_uguu),
        ("tmpfiles", _upload_tmpfiles),
        ("litterbox", _upload_litterbox),
    ]
    for host, upload in hosts:
        try:
            return host, upload(input_path), errors
        except Exception as exc:
            errors.append({"host": host, "error": str(exc)})
    raise RuntimeError(f"all temp hosts failed: {errors}")


# ---------------------------------------------------------------------------
#  Supabase Storage staging (preferred source host)
# ---------------------------------------------------------------------------

def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _supabase_creds() -> tuple[str, str] | None:
    """Resolve credentials able to WRITE to Storage.

    Deliberately narrower than ``poi_asset_backend.from_env`` (which also
    accepts ``SUPABASE_ANON_KEY``): this path creates/uploads/signs/deletes
    private objects, and an anon key would pass the credential check only
    to fail at upload time — after the render is already sunk. Anon-only
    environments are treated as "no Supabase" so ``auto`` falls back
    loudly instead of failing late.
    """
    url = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
    key = (
        os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
        or os.environ.get("SUPABASE_KEY")
        or ""
    ).strip()
    if not url or not key:
        return None
    return url, key


def _supabase_headers(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}", "apikey": key}


def _ensure_staging_bucket(url: str, key: str, bucket: str) -> None:
    """Best-effort create. A pre-existing bucket (409/400) is fine; any
    real problem surfaces as a clear error on the upload that follows."""
    try:
        requests.post(
            f"{url}/storage/v1/bucket",
            headers={**_supabase_headers(key), "Content-Type": "application/json"},
            json={"id": bucket, "name": bucket, "public": False},
            timeout=30,
        )
    except Exception:
        pass


def _upload_supabase(
    input_path: Path, *, input_sha256: str, signed_ttl_sec: int,
) -> tuple[str, str]:
    """Upload to the private staging bucket and return
    ``(signed_url, "<bucket>/<object_path>")``."""
    creds = _supabase_creds()
    if creds is None:
        raise RuntimeError(
            "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY (or SUPABASE_KEY) are "
            "required for --source-host supabase; anon keys cannot write Storage"
        )
    url, key = creds
    bucket = os.environ.get(STAGING_BUCKET_ENV, DEFAULT_STAGING_BUCKET).strip()
    _ensure_staging_bucket(url, key, bucket)
    object_path = f"{input_sha256[:16]}/{input_path.name}"
    with input_path.open("rb") as fh:
        response = requests.post(
            f"{url}/storage/v1/object/{bucket}/{object_path}",
            headers={
                **_supabase_headers(key),
                "Content-Type": "video/mp4",
                "x-upsert": "true",
            },
            data=fh,
            timeout=600,
        )
    response.raise_for_status()
    sign = requests.post(
        f"{url}/storage/v1/object/sign/{bucket}/{object_path}",
        headers={**_supabase_headers(key), "Content-Type": "application/json"},
        json={"expiresIn": signed_ttl_sec},
        timeout=30,
    )
    sign.raise_for_status()
    payload = sign.json()
    signed_path = payload.get("signedURL") or payload.get("signedUrl")
    if not signed_path:
        raise RuntimeError(
            f"supabase sign returned unexpected response: {sign.text[:200]}"
        )
    return f"{url}/storage/v1{signed_path}", f"{bucket}/{object_path}"


def _delete_supabase_object(object_ref: str | None) -> bool:
    """Best-effort cleanup of the staged source after a successful run."""
    creds = _supabase_creds()
    if creds is None or not object_ref:
        return False
    url, key = creds
    try:
        response = requests.delete(
            f"{url}/storage/v1/object/{object_ref}",
            headers=_supabase_headers(key),
            timeout=30,
        )
        return bool(response.ok)
    except Exception:
        return False


def _resolve_source_url(
    input_path: Path, *, source_host: str, input_sha256: str, signed_ttl_sec: int,
) -> dict[str, Any]:
    """Return ``{host, url, staging_object, temp_host_errors}`` for the
    requested source-host mode (``auto`` prefers supabase when creds exist)."""
    if source_host == "auto":
        source_host = "supabase" if _supabase_creds() else "temp"
        if source_host == "temp":
            print(
                "warning: SUPABASE_URL/key not set - falling back to PUBLIC "
                "temp hosts for the WaveSpeed source upload",
                file=sys.stderr,
            )
    if source_host == "supabase":
        signed_url, object_ref = _upload_supabase(
            input_path, input_sha256=input_sha256, signed_ttl_sec=signed_ttl_sec,
        )
        return {
            "host": "supabase",
            "url": signed_url,
            "staging_object": object_ref,
            "temp_host_errors": [],
        }
    host, temp_url, errors = _upload_to_temp(input_path)
    return {
        "host": host,
        "url": temp_url,
        "staging_object": None,
        "temp_host_errors": errors,
    }


# ---------------------------------------------------------------------------
#  Resume state (survive run_batch's whole-command retries without re-paying)
# ---------------------------------------------------------------------------

def _state_path(output_path: Path) -> Path:
    return output_path.with_suffix(output_path.suffix + ".wavespeed_state.json")


def _load_resume_state(state_path: Path, input_sha256: str) -> dict[str, Any] | None:
    if not state_path.exists():
        return None
    try:
        state = json.loads(state_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if (
        state.get("schema_version") != STATE_SCHEMA_VERSION
        or state.get("input_sha256") != input_sha256
        or not state.get("prediction_id")
    ):
        return None
    return state


def _write_resume_state(
    state_path: Path,
    *,
    input_sha256: str,
    prediction_id: str,
    source_host: str,
    staging_object: str | None,
) -> None:
    payload = {
        "schema_version": STATE_SCHEMA_VERSION,
        "input_sha256": input_sha256,
        "prediction_id": prediction_id,
        "source_host": source_host,
        "staging_object": staging_object,
        "submitted_at_epoch": int(time.time()),
    }
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_path.with_suffix(state_path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, sort_keys=True))
    tmp.replace(state_path)


def _try_resume_prediction(
    state: dict[str, Any], *, max_wait: int, poll_interval: int,
) -> str | None:
    """Poll the persisted prediction. Returns the output URL on completion,
    ``None`` when the prediction is unusable (gone/failed → caller submits
    fresh). Timeouts and transient HTTP errors re-raise so run_batch's
    retry classification keeps working."""
    try:
        return _poll_wavespeed(
            state["prediction_id"], max_wait=max_wait, poll_interval=poll_interval,
        )
    except TimeoutError:
        raise
    except requests.HTTPError as exc:
        status_code = exc.response.status_code if exc.response is not None else None
        if status_code in (400, 404, 410):
            return None
        raise
    except RuntimeError:
        # "WaveSpeed failed: ..." / "completed with no outputs" — the paid
        # prediction is a write-off either way; a fresh submission is the
        # only path forward.
        return None


def _wavespeed_headers() -> dict[str, str]:
    key = os.environ.get("WAVESPEED_API_KEY", "").strip()
    if not key:
        raise RuntimeError("WAVESPEED_API_KEY missing")
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def _submit_wavespeed(video_url: str, *, target_resolution: str) -> str:
    base_url = os.environ.get("WAVESPEED_BASE_URL", WAVESPEED_BASE_URL).rstrip("/")
    response = requests.post(
        f"{base_url}/wavespeed-ai/video-upscaler",
        headers=_wavespeed_headers(),
        json={"video": video_url, "target_resolution": target_resolution},
        timeout=int(os.environ.get("WAVESPEED_SUBMIT_TIMEOUT", "60")),
    )
    response.raise_for_status()
    return str(response.json()["data"]["id"])


def _poll_wavespeed(
    prediction_id: str,
    *,
    max_wait: int,
    poll_interval: int,
) -> str:
    base_url = os.environ.get("WAVESPEED_BASE_URL", WAVESPEED_BASE_URL).rstrip("/")
    deadline = time.time() + max_wait
    while True:
        response = requests.get(
            f"{base_url}/predictions/{prediction_id}/result",
            headers=_wavespeed_headers(),
            timeout=int(os.environ.get("WAVESPEED_SUBMIT_TIMEOUT", "60")),
        )
        response.raise_for_status()
        data = response.json().get("data", {})
        status = data.get("status")
        if status == "completed":
            outputs = data.get("outputs") or []
            if not outputs:
                raise RuntimeError(f"WaveSpeed completed with no outputs: {prediction_id}")
            return str(outputs[0])
        if status == "failed":
            raise RuntimeError(f"WaveSpeed failed: {data.get('error')}")
        if time.time() > deadline:
            raise TimeoutError(
                f"WaveSpeed timeout after {max_wait}s: {prediction_id} status={status}"
            )
        time.sleep(poll_interval)


def _download(url: str, output_path: Path) -> int:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = output_path.with_suffix(output_path.suffix + ".download")
    total = 0
    with requests.get(url, stream=True, timeout=300) as response:
        response.raise_for_status()
        with tmp.open("wb") as fh:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    total += len(chunk)
                    fh.write(chunk)
    tmp.replace(output_path)
    return total


def _probe_dimensions(path: Path) -> dict[str, Any]:
    completed = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "json",
            str(path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"ffprobe failed for {path}: {completed.stderr.strip() or completed.stdout.strip()}"
        )
    payload = json.loads(completed.stdout)
    stream = (payload.get("streams") or [{}])[0]
    return {"width": int(stream["width"]), "height": int(stream["height"])}


def upscale_once(
    *,
    input_path: Path,
    output_path: Path,
    target_resolution: str,
    min_width: int,
    min_height: int,
    max_wait: int,
    poll_interval: int,
    source_host: str = "auto",
) -> dict[str, Any]:
    if not input_path.exists():
        raise FileNotFoundError(input_path)
    input_sha256 = _sha256_file(input_path)
    state_path = _state_path(output_path)

    resumed = False
    output_url: str | None = None
    state = _load_resume_state(state_path, input_sha256)
    if state is not None:
        output_url = _try_resume_prediction(
            state, max_wait=max_wait, poll_interval=poll_interval,
        )
        if output_url is not None:
            resumed = True
            prediction_id = str(state["prediction_id"])
            source_info: dict[str, Any] = {
                "host": state.get("source_host", "unknown"),
                "staging_object": state.get("staging_object"),
                "temp_host_errors": [],
            }

    if not resumed:
        source_info = _resolve_source_url(
            input_path,
            source_host=source_host,
            input_sha256=input_sha256,
            signed_ttl_sec=max_wait + SIGNED_URL_EXTRA_TTL_SEC,
        )
        prediction_id = _submit_wavespeed(
            source_info["url"], target_resolution=target_resolution,
        )
        # Persist BEFORE polling: a poll timeout (run_batch retries the
        # whole command) must find the prediction_id on disk.
        _write_resume_state(
            state_path,
            input_sha256=input_sha256,
            prediction_id=prediction_id,
            source_host=source_info["host"],
            staging_object=source_info["staging_object"],
        )
        output_url = _poll_wavespeed(
            prediction_id,
            max_wait=max_wait,
            poll_interval=poll_interval,
        )

    downloaded_bytes = _download(output_url, output_path)
    dimensions = _probe_dimensions(output_path)
    if dimensions["width"] < min_width or dimensions["height"] < min_height:
        # State is intentionally kept: a retry resumes the completed
        # prediction and re-downloads without a fresh paid submission.
        raise RuntimeError(
            "upscaled output below minimum dimensions: "
            f"{dimensions['width']}x{dimensions['height']} < {min_width}x{min_height}"
        )
    staging_deleted = _delete_supabase_object(source_info.get("staging_object"))
    state_path.unlink(missing_ok=True)
    return {
        "status": "completed",
        "provider": "wavespeed",
        "source_host": source_info["host"],
        "resumed": resumed,
        "staging_object_deleted": staging_deleted,
        "temp_host": source_info["host"] if source_info["host"] not in {"supabase", "unknown"} else None,
        "temp_host_errors": source_info["temp_host_errors"],
        "prediction_id": prediction_id,
        "output_path": str(output_path),
        "downloaded_bytes": downloaded_bytes,
        **dimensions,
    }


def run_preflight(source_host: str) -> dict[str, Any]:
    """Config-only readiness check (2026-06-10 review fix).

    run_batch's autopilot preflight previously only proved this command
    EXISTS; a missing WAVESPEED_API_KEY or missing Supabase storage
    credentials still surfaced after the first render. This validates the
    actual runtime configuration — including anything loaded via --env —
    without paid calls or network traffic.
    """
    errors: list[str] = []
    if not os.environ.get("WAVESPEED_API_KEY", "").strip():
        errors.append("WAVESPEED_API_KEY missing")
    resolved = source_host
    if source_host == "auto":
        resolved = "supabase" if _supabase_creds() else "temp"
    if source_host == "supabase" and _supabase_creds() is None:
        errors.append(
            "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY (or SUPABASE_KEY) "
            "missing for --source-host supabase"
        )
    if resolved == "temp" and not errors:
        print(
            "warning: preflight resolved source host to PUBLIC temp hosts — "
            "production must use --source-host supabase",
            file=sys.stderr,
        )
    return {
        "preflight": "failed" if errors else "passed",
        "source_host": resolved,
        "errors": errors,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, help="Local source MP4 path.")
    parser.add_argument("--output", required=True, help="Local upscaled MP4 path.")
    parser.add_argument("--env", help="Optional env file containing WAVESPEED_API_KEY.")
    parser.add_argument("--target-resolution", default="1080p")
    parser.add_argument("--min-width", type=int, default=1080)
    parser.add_argument("--min-height", type=int, default=1920)
    parser.add_argument(
        "--max-wait",
        type=int,
        default=int(os.environ.get("WAVESPEED_MAX_WAIT", "3600")),
    )
    parser.add_argument(
        "--poll-interval",
        type=int,
        default=int(os.environ.get("WAVESPEED_POLL_INTERVAL", "10")),
    )
    parser.add_argument(
        "--source-host",
        choices=("auto", "supabase", "temp"),
        default=os.environ.get("PGC_WAVESPEED_SOURCE_HOST", "auto"),
        help=(
            "Where WaveSpeed fetches the input from: supabase = private "
            "staging bucket + signed URL (preferred), temp = legacy public "
            "temp-host chain, auto = supabase when credentials exist."
        ),
    )
    parser.add_argument(
        "--preflight",
        action="store_true",
        help=(
            "Validate runtime configuration (API key + source-host "
            "credentials, including --env contents) and exit without "
            "upscaling anything. Used by run_batch's autopilot preflight."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        _load_env(args.env)
        if args.preflight:
            result = run_preflight(args.source_host)
            print(json.dumps(result, sort_keys=True))
            return 0 if result["preflight"] == "passed" else 1
        result = upscale_once(
            input_path=Path(args.input),
            output_path=Path(args.output),
            target_resolution=args.target_resolution,
            min_width=args.min_width,
            min_height=args.min_height,
            max_wait=args.max_wait,
            poll_interval=args.poll_interval,
            source_host=args.source_host,
        )
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as exc:
        print(f"wavespeed upscale failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
