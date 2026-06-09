"""Source media width policy for shared POI assets.

The asset platform owns source metadata. PGC only consumes that metadata to
decide which assets are eligible for a given production mode.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


DEFAULT_SOURCE_RESOLUTION_MODE = "best_available"
DEFAULT_TRANSITION_TARGET_WIDTH = 720
DEFAULT_TRANSITION_WIDTH_TOLERANCE_PX = 40
DEFAULT_ASPECT_RATIO_MIN = 1.70
DEFAULT_ASPECT_RATIO_MAX = 1.86


class SourceResolutionPolicyError(ValueError):
    """Raised when a source resolution policy is invalid."""


@dataclass(frozen=True)
class SourceResolutionPolicy:
    mode: str = DEFAULT_SOURCE_RESOLUTION_MODE
    target_width: int | None = None
    tolerance_px: int = DEFAULT_TRANSITION_WIDTH_TOLERANCE_PX
    aspect_ratio_min: float = DEFAULT_ASPECT_RATIO_MIN
    aspect_ratio_max: float = DEFAULT_ASPECT_RATIO_MAX

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "target_width": self.target_width,
            "tolerance_px": self.tolerance_px,
            "aspect_ratio_min": self.aspect_ratio_min,
            "aspect_ratio_max": self.aspect_ratio_max,
        }


def normalize_source_resolution_policy(
    value: Mapping[str, Any] | SourceResolutionPolicy | None,
) -> SourceResolutionPolicy:
    if isinstance(value, SourceResolutionPolicy):
        policy = value
    else:
        raw = dict(value or {})
        mode = str(raw.get("mode") or DEFAULT_SOURCE_RESOLUTION_MODE).strip()
        if mode == "transition_low_res_only":
            target_width = int(raw.get("target_width") or DEFAULT_TRANSITION_TARGET_WIDTH)
        elif mode == "width_band":
            target_width = int(raw.get("target_width") or DEFAULT_TRANSITION_TARGET_WIDTH)
        elif mode == DEFAULT_SOURCE_RESOLUTION_MODE:
            target_width = (
                int(raw["target_width"])
                if raw.get("target_width") not in (None, "")
                else None
            )
        else:
            raise SourceResolutionPolicyError(
                "source_resolution_policy.mode must be one of: "
                "best_available, transition_low_res_only, width_band"
            )
        policy = SourceResolutionPolicy(
            mode=mode,
            target_width=target_width,
            tolerance_px=int(
                raw.get("tolerance_px", DEFAULT_TRANSITION_WIDTH_TOLERANCE_PX)
            ),
            aspect_ratio_min=float(
                raw.get("aspect_ratio_min", DEFAULT_ASPECT_RATIO_MIN)
            ),
            aspect_ratio_max=float(
                raw.get("aspect_ratio_max", DEFAULT_ASPECT_RATIO_MAX)
            ),
        )

    if policy.tolerance_px < 0:
        raise SourceResolutionPolicyError("tolerance_px must be >= 0")
    if policy.aspect_ratio_min <= 0 or policy.aspect_ratio_max <= 0:
        raise SourceResolutionPolicyError("aspect ratio bounds must be positive")
    if policy.aspect_ratio_min > policy.aspect_ratio_max:
        raise SourceResolutionPolicyError(
            "aspect_ratio_min must be <= aspect_ratio_max"
        )
    if policy.mode in {"transition_low_res_only", "width_band"}:
        if policy.target_width is None or policy.target_width <= 0:
            raise SourceResolutionPolicyError(
                "target_width must be positive for width-band policies"
            )
    return policy


def source_resolution_matches(
    row: Mapping[str, Any],
    policy: Mapping[str, Any] | SourceResolutionPolicy | None,
) -> bool:
    resolved = normalize_source_resolution_policy(policy)
    if resolved.mode == DEFAULT_SOURCE_RESOLUTION_MODE:
        return True

    try:
        width = int(row.get("width") or 0)
        height = int(row.get("height") or 0)
    except (TypeError, ValueError):
        return False
    if width <= 0 or height <= 0:
        return False

    assert resolved.target_width is not None
    min_width = resolved.target_width - resolved.tolerance_px
    max_width = resolved.target_width + resolved.tolerance_px
    if width < min_width or width > max_width:
        return False

    ratio = height / width
    return resolved.aspect_ratio_min <= ratio <= resolved.aspect_ratio_max


def source_resolution_summary(
    rows: list[Mapping[str, Any]],
) -> dict[str, int]:
    summary = {
        "total": len(rows),
        "width_720ish": 0,
        "width_1080ish": 0,
        "unknown_width": 0,
    }
    for row in rows:
        try:
            width = int(row.get("width") or 0)
        except (TypeError, ValueError):
            width = 0
        if width <= 0:
            summary["unknown_width"] += 1
        if 680 <= width <= 760:
            summary["width_720ish"] += 1
        if 1040 <= width <= 1120:
            summary["width_1080ish"] += 1
    return summary
