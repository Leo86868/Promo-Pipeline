"""Detachable final-video upscale gate for production handoff."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol


class FinalUpscaleError(RuntimeError):
    """Raised when final-video upscale is required but not safely available."""


@dataclass(frozen=True)
class FinalUpscalePolicy:
    required: bool = False
    enabled: bool = False
    provider: str = "disabled"
    reason: str | None = None
    target_width: int = 1080
    target_height: int = 1920

    def to_dict(self) -> dict[str, Any]:
        return {
            "required": self.required,
            "enabled": self.enabled,
            "provider": self.provider,
            "reason": self.reason,
            "target_width": self.target_width,
            "target_height": self.target_height,
        }


class FinalVideoUpscaler(Protocol):
    def upscale(self, *, input_path: str, output_path: str) -> dict[str, Any]:
        """Upscale ``input_path`` into ``output_path`` and return metadata."""


def normalize_final_upscale_policy(
    value: Mapping[str, Any] | FinalUpscalePolicy | None,
    *,
    source_policy_mode: str = "best_available",
) -> FinalUpscalePolicy:
    if isinstance(value, FinalUpscalePolicy):
        return value
    raw = dict(value or {})
    # "has a width policy" must NOT be conflated with "needs upscale". Only the
    # transition modes (720-era low-res sources) default to requiring upscale.
    # min_width is the 1080-endgame policy — sources are ALREADY native >=1080,
    # so it must default to required=False; otherwise a min_width batch that
    # forgets `--final-upscale-provider disabled` would silently re-arm upscale
    # (re-encoding already-1080 masters / burning WaveSpeed spend during the
    # transition window when the env command is still present). Explicit off is
    # now belt-and-suspenders, not the only line of defence (2026-06-22 flip).
    default_required = source_policy_mode not in {"best_available", "min_width"}
    required = bool(raw.get("required", default_required))
    enabled = bool(raw.get("enabled", required))
    provider = str(raw.get("provider") or ("wavespeed" if enabled else "disabled"))
    if not enabled:
        provider = "disabled"
    if provider not in {"disabled", "wavespeed"}:
        raise FinalUpscaleError("final_upscale_policy.provider must be disabled or wavespeed")
    target_width = int(raw.get("target_width") or 1080)
    target_height = int(raw.get("target_height") or 1920)
    if target_width <= 0 or target_height <= 0:
        raise FinalUpscaleError("final upscale target dimensions must be positive")
    return FinalUpscalePolicy(
        required=required,
        enabled=enabled,
        provider=provider,
        reason=raw.get("reason") or (
            "low_res_source_transition" if required else None
        ),
        target_width=target_width,
        target_height=target_height,
    )


def _parse_last_json_line(stdout: str) -> dict[str, Any] | None:
    for line in reversed((stdout or "").strip().splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return None


class CommandFinalVideoUpscaler:
    """Run an operator-provided command that performs the actual upscale.

    The command template must write an MP4 to ``{output_path}``. This keeps PGC
    decoupled from the temporary hosting choice needed by WaveSpeed.
    """

    def __init__(self, *, command_template: str, provider: str = "wavespeed") -> None:
        if not command_template.strip():
            raise FinalUpscaleError("final upscale command template is required")
        self._command_template = command_template
        self._provider = provider

    def upscale(self, *, input_path: str, output_path: str) -> dict[str, Any]:
        command = self._command_template.format(
            input_path=input_path,
            output_path=output_path,
        )
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        completed = subprocess.run(
            shlex.split(command),
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            raise FinalUpscaleError(
                f"final upscale command failed with exit {completed.returncode}: "
                f"{completed.stderr.strip() or completed.stdout.strip()}"
            )
        result = {
            "status": "applied",
            "provider": self._provider,
            "input_path": input_path,
            "output_path": output_path,
        }
        # Auditability (2026-06-10 review fix): the wavespeed CLI prints a
        # JSON result line (prediction_id, source_host, resumed,
        # staging_object_deleted). Keep it in the receipt instead of
        # discarding stdout.
        details = _parse_last_json_line(completed.stdout)
        if details is not None:
            result["details"] = details
        return result

    def preflight(self) -> dict[str, Any]:
        """Run the command in --preflight mode (2026-06-10 review fix).

        Validates the command's actual runtime configuration — API key and
        source-host credentials, including anything its --env file loads —
        before any render spend. Raises :class:`FinalUpscaleError` on
        failure so run_batch's autopilot preflight can fail the batch.
        """
        command = self._command_template.format(
            input_path="__preflight__",
            output_path="__preflight__",
        ) + " --preflight"
        completed = subprocess.run(
            shlex.split(command),
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            raise FinalUpscaleError(
                "final upscale preflight failed: "
                f"{completed.stdout.strip() or completed.stderr.strip()}"
            )
        return _parse_last_json_line(completed.stdout) or {"preflight": "passed"}


def create_final_video_upscaler_from_env(
    policy: FinalUpscalePolicy,
) -> FinalVideoUpscaler | None:
    if not policy.enabled:
        return None
    if policy.provider != "wavespeed":
        raise FinalUpscaleError(f"unsupported final upscale provider: {policy.provider}")
    command = os.getenv("PGC_WAVESPEED_UPSCALE_COMMAND", "").strip()
    if not command:
        return None
    return CommandFinalVideoUpscaler(command_template=command, provider=policy.provider)


# Upscaled output must match the pre-upscale master's duration within
# this tolerance — container remux/encode jitter only, never content loss.
DURATION_TOLERANCE_SEC = 1.0


def probe_video_properties(path: Path) -> dict[str, Any]:
    """ffprobe width/height/duration/audio in one call.

    2026-06-10 hardening (resume-reuse review): dimensions alone are NOT
    proof of a usable master — a truncated or silent MP4 can still report
    1080x1920 from its header. Duration and audio-stream presence are
    probed alongside so verification can fail closed on broken outputs.
    """
    completed = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type,width,height",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise FinalUpscaleError(
            "ffprobe failed for final upscale output: "
            f"{completed.stderr.strip() or completed.stdout.strip()}"
        )
    try:
        payload = json.loads(completed.stdout)
        streams = payload.get("streams") or []
        video = next(
            stream for stream in streams if stream.get("codec_type") == "video"
        )
        return {
            "width": int(video["width"]),
            "height": int(video["height"]),
            "has_audio": any(
                stream.get("codec_type") == "audio" for stream in streams
            ),
            "duration_sec": float((payload.get("format") or {}).get("duration") or 0.0),
        }
    except (StopIteration, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise FinalUpscaleError(
            f"ffprobe returned invalid stream payload for {path}"
        ) from exc


def verify_final_upscale_output(
    *,
    output_path: str,
    policy: FinalUpscalePolicy,
    expected_duration_sec: float | None = None,
) -> dict[str, Any]:
    """Fail-closed check that an upscaled MP4 is a usable master.

    Beyond dimensions, requires a positive duration (within
    ``DURATION_TOLERANCE_SEC`` of ``expected_duration_sec`` when the
    caller knows the pre-upscale master's length) and an audio stream —
    PGC masters always carry narration + BGM. 2026-06-10 hardening: this
    verdict now also gates the resume-path reuse of an existing upscale
    output, where "looks 1080x1920" alone must not be treated as "is a
    complete 65s video with sound".
    """
    path = Path(output_path)
    if not path.exists():
        return {"verified": False, "reason": "missing_output", "path": output_path}
    size = path.stat().st_size
    if size <= 0:
        return {"verified": False, "reason": "empty_output", "path": output_path}
    base = {
        "path": output_path,
        "file_size_bytes": size,
        "target_width": policy.target_width,
        "target_height": policy.target_height,
        "expected_duration_sec": expected_duration_sec,
    }
    try:
        props = probe_video_properties(path)
    except FinalUpscaleError as exc:
        return {
            **base,
            "verified": False,
            "reason": "dimension_probe_failed",
            "error": str(exc),
        }
    base.update(props)
    if (
        props["width"] != policy.target_width
        or props["height"] != policy.target_height
    ):
        return {**base, "verified": False, "reason": "dimension_mismatch"}
    if props["duration_sec"] <= 0:
        return {**base, "verified": False, "reason": "missing_duration"}
    if (
        expected_duration_sec is not None
        and abs(props["duration_sec"] - float(expected_duration_sec))
        > DURATION_TOLERANCE_SEC
    ):
        return {**base, "verified": False, "reason": "duration_mismatch"}
    if not props["has_audio"]:
        return {**base, "verified": False, "reason": "missing_audio_stream"}
    return {**base, "verified": True}
