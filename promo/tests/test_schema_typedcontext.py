"""Sprint 14 item (f) — TypedDict schema + N1 runtime-parity regression guards.

Covers:
  - AC13: all 7 TypedDicts importable with the declared key sets
  - N1:   public functions on the 6 refactored core modules keep their
          __name__ (runtime-parity guardrail)
  - AC14: signature-only grep residual count stays at 0 (source-grep
          guardrail against future regression)
"""

from __future__ import annotations

import re
import subprocess
import typing
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def test_seven_typeddicts_importable_and_named():
    """AC13: the 7 TypedDicts exist with the expected names."""
    from promo.core.schema import (
        ClipAssignment,
        ClipMetadata,
        Narration,
        Script,
        ScriptSegment,
        SegmentTimestamp,
        WordTimestamp,
    )

    names = {
        WordTimestamp.__name__,
        SegmentTimestamp.__name__,
        ClipMetadata.__name__,
        ClipAssignment.__name__,
        ScriptSegment.__name__,
        Script.__name__,
        Narration.__name__,
    }
    assert names == {
        "WordTimestamp",
        "SegmentTimestamp",
        "ClipMetadata",
        "ClipAssignment",
        "ScriptSegment",
        "Script",
        "Narration",
    }


def test_narration_has_seven_tts_migration_contract_keys():
    """AC13: Narration carries all 7 keys from tts_engine.generate_narration."""
    from promo.core.schema import Narration

    contract_keys = {
        "audio_path",
        "word_timestamps",
        "segment_timestamps",
        "duration",
        "voice_id",
        "voice_key",
        "tagged_text",
    }
    all_keys = Narration.__required_keys__ | Narration.__optional_keys__
    assert all_keys >= contract_keys


def test_script_segments_is_list_of_scriptsegment():
    """AC13 pre-planner amendment: Script.segments is list[ScriptSegment], not list[dict]."""
    from promo.core.schema import Script, ScriptSegment

    hints = typing.get_type_hints(Script)
    inner = hints["segments"].__args__[0]
    assert inner is ScriptSegment


def test_public_functions_on_six_modules_keep_their_names():
    """N1: runtime-parity guardrail — public entry points exist with unchanged names."""
    # Minimum set exercised downstream of the refactor. If any of these
    # symbols moves/renames, compile_promo's orchestrator breaks.
    from promo.core.assign.clip_assigner import assign_clips, assign_clips_with_f3_retry
    from promo.core.assign.clip_embedder import attach_embeddings_to_metadata, embed_clips_for_poi
    from promo.core.assign.clip_retriever import top_k, union_of_top_k
    from promo.core.render.remotion_renderer import build_props_from_script
    from promo.core.script.script_generator import generate_script_variants
    from promo.core.narrate.tts_engine import generate_narration

    assert assign_clips.__name__ == "assign_clips"
    assert assign_clips_with_f3_retry.__name__ == "assign_clips_with_f3_retry"
    assert attach_embeddings_to_metadata.__name__ == "attach_embeddings_to_metadata"
    assert embed_clips_for_poi.__name__ == "embed_clips_for_poi"
    assert top_k.__name__ == "top_k"
    assert union_of_top_k.__name__ == "union_of_top_k"
    assert build_props_from_script.__name__ == "build_props_from_script"
    assert generate_script_variants.__name__ == "generate_script_variants"
    assert generate_narration.__name__ == "generate_narration"


def test_ac14_signature_grep_residual_at_zero():
    """AC14 source-grep guardrail — no bare list[dict] / -> dict: in the
    6 target modules plus the downstream helpers that propagate TypedDicts
    through the pipeline (forced_aligner, tts_batch_planner). The contract
    pins the 6; the two downstream helpers are included here as a
    defense-in-depth guardrail against L-004 annotation drift.
    """
    targets = [
        "promo/core/assign/clip_assigner.py",
        "promo/core/assign/clip_assignment_sidecar.py",
        "promo/core/assign/clip_assignment_validator.py",
        "promo/core/script/script_generator.py",
        "promo/core/narrate/tts_engine.py",
        "promo/core/render/remotion_renderer.py",
        "promo/core/assign/clip_embedder.py",
        "promo/core/assign/clip_retriever.py",
        "promo/core/narrate/forced_aligner.py",
        "promo/core/narrate/tts_batch_planner.py",
    ]
    pattern = re.compile(
        r"list\[dict\]|-> dict\[str, *Any\]|-> dict:$|-> dict$"
    )
    offender_re = re.compile(r"^\s*def |\) ->")
    hits: list[str] = []
    for rel in targets:
        path = REPO_ROOT / rel
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            if not offender_re.search(line):
                continue
            if pattern.search(line):
                hits.append(f"{rel}:{lineno}:{line.strip()}")
    assert len(hits) == 0, (
        "AC14 signature-only residual regression:\n" + "\n".join(hits)
    )


def test_logging_config_module_exists_and_exports_configure_logging():
    """Sprint 14 item (b) source-structure guardrail — module + API live at expected path."""
    import promo.core.logging_config as mod

    assert hasattr(mod, "configure_logging")
    assert callable(mod.configure_logging)
