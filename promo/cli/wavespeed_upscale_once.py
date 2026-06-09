#!/usr/bin/env python3
"""Upscale one local MP4 with WaveSpeed and write a local MP4 output."""

from __future__ import annotations

import argparse
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
) -> dict[str, Any]:
    if not input_path.exists():
        raise FileNotFoundError(input_path)
    host, temp_url, host_errors = _upload_to_temp(input_path)
    prediction_id = _submit_wavespeed(temp_url, target_resolution=target_resolution)
    output_url = _poll_wavespeed(
        prediction_id,
        max_wait=max_wait,
        poll_interval=poll_interval,
    )
    downloaded_bytes = _download(output_url, output_path)
    dimensions = _probe_dimensions(output_path)
    if dimensions["width"] < min_width or dimensions["height"] < min_height:
        raise RuntimeError(
            "upscaled output below minimum dimensions: "
            f"{dimensions['width']}x{dimensions['height']} < {min_width}x{min_height}"
        )
    return {
        "status": "completed",
        "provider": "wavespeed",
        "temp_host": host,
        "temp_host_errors": host_errors,
        "prediction_id": prediction_id,
        "output_path": str(output_path),
        "downloaded_bytes": downloaded_bytes,
        **dimensions,
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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        _load_env(args.env)
        result = upscale_once(
            input_path=Path(args.input),
            output_path=Path(args.output),
            target_resolution=args.target_resolution,
            min_width=args.min_width,
            min_height=args.min_height,
            max_wait=args.max_wait,
            poll_interval=args.poll_interval,
        )
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as exc:
        print(f"wavespeed upscale failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
