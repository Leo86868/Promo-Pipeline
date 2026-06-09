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
    default_required = source_policy_mode != "best_available"
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
        return {
            "status": "applied",
            "provider": self._provider,
            "input_path": input_path,
            "output_path": output_path,
        }


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


def _probe_video_dimensions(path: Path) -> tuple[int, int]:
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
        raise FinalUpscaleError(
            "ffprobe failed for final upscale output: "
            f"{completed.stderr.strip() or completed.stdout.strip()}"
        )
    try:
        payload = json.loads(completed.stdout)
        stream = (payload.get("streams") or [])[0]
        return int(stream["width"]), int(stream["height"])
    except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise FinalUpscaleError(
            f"ffprobe returned invalid dimension payload for {path}"
        ) from exc


def verify_final_upscale_output(
    *,
    output_path: str,
    policy: FinalUpscalePolicy,
) -> dict[str, Any]:
    path = Path(output_path)
    if not path.exists():
        return {"verified": False, "reason": "missing_output", "path": output_path}
    size = path.stat().st_size
    if size <= 0:
        return {"verified": False, "reason": "empty_output", "path": output_path}
    try:
        width, height = _probe_video_dimensions(path)
    except FinalUpscaleError as exc:
        return {
            "verified": False,
            "reason": "dimension_probe_failed",
            "path": output_path,
            "file_size_bytes": size,
            "error": str(exc),
            "target_width": policy.target_width,
            "target_height": policy.target_height,
        }
    if width != policy.target_width or height != policy.target_height:
        return {
            "verified": False,
            "reason": "dimension_mismatch",
            "path": output_path,
            "file_size_bytes": size,
            "width": width,
            "height": height,
            "target_width": policy.target_width,
            "target_height": policy.target_height,
        }
    return {
        "verified": True,
        "path": output_path,
        "file_size_bytes": size,
        "width": width,
        "height": height,
        "target_width": policy.target_width,
        "target_height": policy.target_height,
    }
