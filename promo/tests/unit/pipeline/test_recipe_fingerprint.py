"""Parity + behavior tests for the vendored recipe_fingerprint module.

The golden-vector test is the drift guard for the VERBATIM VENDORED COPY at
``promo/core/recipe_fingerprint.py`` — if it diverges from the AIGC platform's
canonicalization, the fingerprints stop matching the DB trigger and these tests
go red. The remaining tests pin PGC's recipe_input wiring (occurrence order,
music excluded, trim ignored, repeats kept) and the fail-loud contract.
"""

import json
from pathlib import Path

import pytest

GOLDEN_PATH = Path(__file__).resolve().parents[2] / "fixtures" / "recipe_fingerprint_golden.json"


def _load_golden():
    return json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))


# --- Golden-vector parity (drift guard for the vendored copy) ----------------

def test_golden_vectors_match_vendored_copy():
    from promo.core.recipe_fingerprint import recipe_fingerprint

    vectors = _load_golden()
    assert vectors, "golden fixture must contain vectors"
    for case in vectors:
        got = recipe_fingerprint(case["recipe_input"])
        assert got == case["expected"], (
            f"recipe_fingerprint drift on {case['name']!r}: "
            f"got {got} expected {case['expected']}"
        )


def test_fingerprint_version_pinned():
    from promo.core.recipe_fingerprint import FINGERPRINT_VERSION

    assert FINGERPRINT_VERSION == "rfp2"


def test_recipe_fingerprint_rejects_empty_list():
    from promo.core.recipe_fingerprint import recipe_fingerprint

    with pytest.raises(ValueError):
        recipe_fingerprint([])


def test_recipe_fingerprint_rejects_empty_hash():
    from promo.core.recipe_fingerprint import recipe_fingerprint

    with pytest.raises(ValueError):
        recipe_fingerprint(["sha256:aaaa", ""])


# --- recipe_input_from_render_manifest (PGC run_manifest shape) ---------------

def _manifest(timeline_entries, asset_snapshot):
    return {
        "asset_snapshot": asset_snapshot,
        "timeline_entries": timeline_entries,
    }


def test_recipe_input_in_occurrence_order():
    from promo.core.recipe_fingerprint import recipe_input_from_render_manifest

    # Deliberately out of order in the list; occurrence_index drives ordering.
    manifest = _manifest(
        timeline_entries=[
            {"asset_id": "b", "occurrence_index": 1, "variant_index": 1},
            {"asset_id": "a", "occurrence_index": 0, "variant_index": 1},
        ],
        asset_snapshot=[
            {"asset_id": "a", "source_content_hash": "sha256:aaaa"},
            {"asset_id": "b", "source_content_hash": "sha256:bbbb"},
        ],
    )

    assert recipe_input_from_render_manifest(manifest, variant_index=1) == [
        "sha256:aaaa",
        "sha256:bbbb",
    ]


def test_recipe_input_ignores_trim_and_includes_repeats():
    from promo.core.recipe_fingerprint import recipe_input_from_render_manifest

    # Same asset reused twice at different in-points: trim ignored, both kept.
    manifest = _manifest(
        timeline_entries=[
            {"asset_id": "a", "occurrence_index": 0, "variant_index": 1,
             "trim_start_sec": 0.0},
            {"asset_id": "a", "occurrence_index": 1, "variant_index": 1,
             "trim_start_sec": 4.2},
        ],
        asset_snapshot=[
            {"asset_id": "a", "source_content_hash": "sha256:aaaa"},
        ],
    )

    assert recipe_input_from_render_manifest(manifest, variant_index=1) == [
        "sha256:aaaa",
        "sha256:aaaa",
    ]


def test_recipe_input_excludes_music_by_construction():
    from promo.core.recipe_fingerprint import recipe_input_from_render_manifest

    # Music is never in timeline_entries (it lives in the audio block), so it is
    # excluded automatically — only the video clips appear in recipe_input.
    manifest = {
        "asset_snapshot": [
            {"asset_id": "clip", "source_content_hash": "sha256:clip"},
            {"asset_id": "track", "source_content_hash": "sha256:music"},
        ],
        "audio": {"music": {"asset_id": "track"}},
        "timeline_entries": [
            {"asset_id": "clip", "occurrence_index": 0, "variant_index": 1},
        ],
    }

    assert recipe_input_from_render_manifest(manifest, variant_index=1) == [
        "sha256:clip",
    ]


def test_recipe_input_fail_loud_on_missing_snapshot_hash():
    from promo.core.recipe_fingerprint import recipe_input_from_render_manifest

    # timeline references asset_id 'b' which has no snapshot entry → fail loud,
    # never falls back to asset_id.
    manifest = _manifest(
        timeline_entries=[
            {"asset_id": "a", "occurrence_index": 0, "variant_index": 1},
            {"asset_id": "b", "occurrence_index": 1, "variant_index": 1},
        ],
        asset_snapshot=[
            {"asset_id": "a", "source_content_hash": "sha256:aaaa"},
        ],
    )

    with pytest.raises(ValueError):
        recipe_input_from_render_manifest(manifest, variant_index=1)
