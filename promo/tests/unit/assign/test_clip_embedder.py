"""Unit tests for promo.core.assign.clip_embedder."""

import json
import os
import re
import shutil
import sys
import tempfile
from unittest.mock import patch, MagicMock

from pathlib import Path

import pytest

def _fake_openai_embedder(inputs):
    """Deterministic stub — returns one-hot 1536-dim vectors keyed on hash of input.

    Two different input strings produce two different vectors (low cosine);
    the same string always produces the same vector (idempotent). Used to
    replace `_post_openai_embeddings` so embedder unit tests never hit the
    network.
    """
    import hashlib

    vecs = []
    for text in inputs:
        h = int(hashlib.sha1(text.encode("utf-8")).hexdigest(), 16)
        idx = h % 1536
        v = [0.0] * 1536
        v[idx] = 1.0
        vecs.append(v)
    return vecs

@pytest.fixture
def stub_openai(monkeypatch):
    """Replace `_post_embeddings` with a deterministic one-hot stub.

    Returns a small tracker object so tests can count invocations. (Fixture
    name kept as ``stub_openai`` for brevity — the underlying model IS still
    OpenAI's text-embedding-3-small; only the transport goes through
    OpenRouter's OpenAI-compatible proxy.)
    """
    from promo.core.assign import clip_embedder

    class Tracker:
        calls = 0
        last_inputs = None

    def _stub(inputs, api_key):
        Tracker.calls += 1
        Tracker.last_inputs = list(inputs)
        return _fake_openai_embedder(inputs)

    monkeypatch.setattr(clip_embedder, "_post_embeddings", _stub)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key-not-used")
    return Tracker

class TestSprint12aEmbedderPublicAPI:
    """AC1 — public API exists and composition is correct."""

    def test_public_exports_present(self):
        from promo.core.assign import clip_embedder

        for name in (
            "embed_clips_for_poi",
            "load_embeddings_for_poi",
            "attach_embeddings_to_metadata",
            "embed_texts",
            "compose_embedding_text",
            "current_mimo_prompt_sha1",
            "sidecar_filename",
            "sidecar_path",
            "EMBEDDING_MODEL",
            "EMBEDDING_DIM",
            "CACHE_DIR_NAME",
            "COMPOSITION_VERSION",
        ):
            assert hasattr(clip_embedder, name), (
                f"clip_embedder missing public symbol: {name}"
            )

    def test_compose_embedding_text_excludes_dominant_motion_phase(self):
        """AC1 post-advisor: dominant_motion_phase is NOT in the composed string."""
        from promo.core.assign.clip_embedder import compose_embedding_text

        text = compose_embedding_text({
            "scene_description": "A pool at sunset",
            "category": "pool",
            "dominant_motion_phase": "middle",
        })
        assert text == "A pool at sunset | pool", (
            f"composition changed: {text!r}"
        )
        # Explicit: dominant_motion_phase token must not appear anywhere.
        for forbidden in ("middle", "late", "early", "dominant_motion_phase"):
            assert forbidden not in text, (
                f"'{forbidden}' leaked into composed text: {text!r}"
            )

    def test_compose_handles_missing_fields(self):
        from promo.core.assign.clip_embedder import compose_embedding_text

        assert compose_embedding_text({}) == " | "
        assert compose_embedding_text({"scene_description": "X"}) == "X | "
        assert compose_embedding_text({"category": "pool"}) == " | pool"

class TestSprint12aEmbedderSidecarShape:
    """AC1 + AC2 — sidecar filename, payload shape, atomic writes."""

    def test_filename_regex_matches_four_axis_format(self):
        from promo.core.assign.clip_embedder import sidecar_filename

        name = sidecar_filename("abcd1234", composition_version=1)
        assert re.match(
            r"^text-embedding-3-small-1536-[0-9a-f]{8}-v\d+\.json$", name,
        ), f"filename shape wrong: {name!r}"

    def test_sidecar_reuses_clip_analyzer_version_suffix(self):
        """AC2 — embedder must use `clip_analyzer._cache_version_suffix`,
        not a re-derived SHA1."""
        import inspect
        from promo.core.assign import clip_embedder

        src = inspect.getsource(clip_embedder)
        assert "_cache_version_suffix" in src, (
            "embedder must import/use clip_analyzer._cache_version_suffix "
            "(found no reference)"
        )
        assert "_ANALYSIS_PROMPT" in src, (
            "embedder must reference clip_analyzer._ANALYSIS_PROMPT"
        )
        assert "COMPOSITION_VERSION" in src, (
            "embedder must define COMPOSITION_VERSION (4th axis)"
        )

    def test_sidecar_payload_shape_and_no_tmp_leftover(
        self, stub_openai, tmp_path,
    ):
        """AC1 verify: 2-clip run → sidecar has vector + input per clip,
        correct metadata keys, no .json.tmp leftover."""
        from promo.core.assign import clip_embedder

        clips = [
            {"id": "c1", "scene_description": "a pool at sunset", "category": "pool"},
            {"id": "c2", "scene_description": "a lobby at dawn",  "category": "lobby"},
        ]
        result = clip_embedder.embed_clips_for_poi(
            clips, cache_dir=str(tmp_path), mimo_prompt_sha1="deadbeef",
        )

        # Payload metadata
        assert result["model"] == "text-embedding-3-small"
        assert result["dim"] == 1536
        assert result["mimo_prompt_sha1"] == "deadbeef"
        assert result["composition_version"] == clip_embedder.COMPOSITION_VERSION

        # Per-clip shape: {"vector": [...], "input": "..."}
        for cid in ("c1", "c2"):
            entry = result["embeddings"][cid]
            assert isinstance(entry, dict)
            assert "vector" in entry and "input" in entry
            assert len(entry["vector"]) == 1536
            # AC1 additional: the input text must exactly match composition v1
            assert entry["input"] == clip_embedder.compose_embedding_text(
                next(c for c in clips if c["id"] == cid)
            )

        # Sidecar is on disk at the expected path, no tmp leftover.
        sidecar = result["sidecar_path"]
        assert os.path.exists(sidecar)
        leftover = [
            f for f in os.listdir(tmp_path) if f.endswith(".json.tmp")
        ]
        assert leftover == [], f".json.tmp not cleaned up: {leftover}"

        # Sidecar on disk round-trips to the same payload shape.
        with open(sidecar) as f:
            loaded = json.load(f)
        assert loaded["embeddings"]["c1"]["vector"] == result["embeddings"]["c1"]["vector"]

    def test_raises_on_empty_clip_metadata(self, stub_openai, tmp_path):
        from promo.core.assign.clip_embedder import embed_clips_for_poi

        with pytest.raises(ValueError, match="empty"):
            embed_clips_for_poi([], cache_dir=str(tmp_path), mimo_prompt_sha1="x")

class TestSprint12aEmbedderInvalidation:
    """AC3 — version-suffix invalidation on both axes."""

    def test_mimo_prompt_sha1_change_produces_new_sidecar(
        self, stub_openai, tmp_path,
    ):
        """AC3a — changing the MiMo prompt SHA produces a new filename;
        the prior sidecar remains untouched."""
        from promo.core.assign import clip_embedder

        clips = [{"id": "c1", "scene_description": "X", "category": "pool"}]

        r1 = clip_embedder.embed_clips_for_poi(
            clips, cache_dir=str(tmp_path), mimo_prompt_sha1="aaaaaaaa",
        )
        r2 = clip_embedder.embed_clips_for_poi(
            clips, cache_dir=str(tmp_path), mimo_prompt_sha1="bbbbbbbb",
        )

        assert r1["sidecar_path"] != r2["sidecar_path"]
        assert "aaaaaaaa" in os.path.basename(r1["sidecar_path"])
        assert "bbbbbbbb" in os.path.basename(r2["sidecar_path"])
        assert os.path.exists(r1["sidecar_path"]), "old sidecar was deleted"
        assert os.path.exists(r2["sidecar_path"])

    def test_composition_version_change_produces_new_sidecar(
        self, stub_openai, tmp_path,
    ):
        """AC3b — bumping COMPOSITION_VERSION produces a new filename."""
        from promo.core.assign import clip_embedder

        clips = [{"id": "c1", "scene_description": "X", "category": "pool"}]

        r1 = clip_embedder.embed_clips_for_poi(
            clips, cache_dir=str(tmp_path),
            mimo_prompt_sha1="deadbeef", composition_version=1,
        )
        r2 = clip_embedder.embed_clips_for_poi(
            clips, cache_dir=str(tmp_path),
            mimo_prompt_sha1="deadbeef", composition_version=2,
        )

        assert r1["sidecar_path"] != r2["sidecar_path"]
        assert r1["sidecar_path"].endswith("-v1.json")
        assert r2["sidecar_path"].endswith("-v2.json")
        assert os.path.exists(r1["sidecar_path"])
        assert os.path.exists(r2["sidecar_path"])

class TestSprint12aEmbedderIncremental:
    """AC4 — second run with one new clip embeds exactly the new clip."""

    def test_incremental_only_new_clip_hits_api(self, stub_openai, tmp_path):
        from promo.core.assign import clip_embedder

        initial = [
            {"id": "c1", "scene_description": "pool view", "category": "pool"},
            {"id": "c2", "scene_description": "lobby view", "category": "lobby"},
        ]
        clip_embedder.embed_clips_for_poi(
            initial, cache_dir=str(tmp_path), mimo_prompt_sha1="ffff0000",
        )
        stub_openai.calls = 0  # reset counter

        extended = initial + [
            {"id": "c3", "scene_description": "spa view", "category": "spa"},
        ]
        result = clip_embedder.embed_clips_for_poi(
            extended, cache_dir=str(tmp_path), mimo_prompt_sha1="ffff0000",
        )

        # Exactly one OpenAI call, with only the new clip's text.
        assert stub_openai.calls == 1, (
            f"expected 1 API call for the single new clip, got {stub_openai.calls}"
        )
        assert stub_openai.last_inputs == ["spa view | spa"]

        # Sidecar now holds all 3 embeddings.
        assert set(result["embeddings"].keys()) == {"c1", "c2", "c3"}
        assert result["stats"] == {
            "clips_embedded": 1,
            "cache_hits": 2,
            "incremental": 1,
        }

    def test_fully_cached_rerun_makes_zero_api_calls(self, stub_openai, tmp_path):
        from promo.core.assign import clip_embedder

        clips = [
            {"id": "c1", "scene_description": "pool", "category": "pool"},
            {"id": "c2", "scene_description": "lobby", "category": "lobby"},
        ]
        clip_embedder.embed_clips_for_poi(
            clips, cache_dir=str(tmp_path), mimo_prompt_sha1="ee00ee00",
        )
        stub_openai.calls = 0

        result = clip_embedder.embed_clips_for_poi(
            clips, cache_dir=str(tmp_path), mimo_prompt_sha1="ee00ee00",
        )
        assert stub_openai.calls == 0
        assert result["stats"]["clips_embedded"] == 0
        assert result["stats"]["cache_hits"] == 2
        assert result["stats"]["incremental"] == 0

class TestSprint12aOpenAIResponseGuards:
    """Amendment finding #4 — strict `it["index"]` (no silent fallback)."""

    def test_missing_index_key_raises(self, monkeypatch):
        """If the embeddings API ever returns an item without `index`, raise
        loudly instead of silently collapsing onto position 0."""
        import requests
        from promo.core.assign import clip_embedder

        class FakeResp:
            def raise_for_status(self):
                pass

            def json(self):
                return {
                    "data": [
                        {"index": 0, "embedding": [0.0] * 1536},
                        {"embedding": [0.0] * 1536},  # missing "index" !!
                    ],
                }

        monkeypatch.setattr(requests, "post", lambda *a, **kw: FakeResp())
        with pytest.raises(KeyError):
            clip_embedder._post_embeddings(["a", "b"], "fake-key")

class TestSprint12aAttachHelper:
    """Amendment finding #1 — `attach_embeddings_to_metadata` merges cleanly."""

    def test_attach_joins_metadata_and_vectors(self):
        from promo.core.assign.clip_embedder import attach_embeddings_to_metadata

        metadata = [
            {"id": "c1", "scene_description": "pool", "category": "pool"},
            {"id": "c2", "scene_description": "lobby", "category": "lobby"},
        ]
        sidecar = {
            "embeddings": {
                "c1": {"vector": [1.0, 0.0], "input": "pool | pool"},
                "c2": {"vector": [0.0, 1.0], "input": "lobby | lobby"},
            },
        }
        joined, dropped = attach_embeddings_to_metadata(metadata, sidecar)
        assert dropped == []
        assert len(joined) == 2
        assert joined[0]["embedding"] == [1.0, 0.0]
        assert joined[0]["embedding_input"] == "pool | pool"
        assert joined[0]["scene_description"] == "pool"  # passthrough
        assert joined[1]["embedding"] == [0.0, 1.0]

    def test_attach_skips_clips_missing_from_sidecar(self, caplog):
        """Sprint 13 AC18 (D-002): the per-clip-dropped WARNING previously
        emitted inside attach_embeddings_to_metadata is now emitted by
        compile_promo._step_assign_clips ONLY. This function returns the
        dropped_ids to the caller silently."""
        from promo.core.assign.clip_embedder import attach_embeddings_to_metadata

        metadata = [
            {"id": "c1", "scene_description": "a", "category": "pool"},
            {"id": "c_ghost", "scene_description": "b", "category": "room"},
        ]
        sidecar = {"embeddings": {"c1": {"vector": [1.0], "input": "a | pool"}}}
        with caplog.at_level("WARNING", logger="promo.core.assign.clip_embedder"):
            joined, dropped = attach_embeddings_to_metadata(metadata, sidecar)
        assert [c["id"] for c in joined] == ["c1"]
        assert dropped == ["c_ghost"]
        # D-002 invariant: the function itself emits no per-clip-dropped
        # WARNING now — the record must surface via the compile_promo caller.
        assert not any(
            "c_ghost" in rec.message
            for rec in caplog.records
            if rec.name == "promo.core.assign.clip_embedder"
        )

class TestSprint12aAuditFixM1DedupeBatch:
    """M-1 — embed_clips_for_poi dedupes same-id duplicates in the incoming
    clip_metadata list, keeps the first occurrence, warns on subsequent."""

    def test_duplicate_ids_in_batch_are_deduped_first_wins(
        self, stub_openai, tmp_path, caplog,
    ):
        from promo.core.assign import clip_embedder

        batch = [
            {"id": "c1", "scene_description": "A", "category": "pool"},
            {"id": "c1", "scene_description": "B", "category": "room"},  # dup
            {"id": "c2", "scene_description": "C", "category": "spa"},
        ]
        with caplog.at_level("WARNING"):
            result = clip_embedder.embed_clips_for_poi(
                batch, cache_dir=str(tmp_path), mimo_prompt_sha1="abcd0001",
            )
        # Second c1 was dropped — stats reflect the 2 unique clips.
        assert result["stats"]["clips_embedded"] == 2
        assert set(result["embeddings"].keys()) == {"c1", "c2"}
        # First occurrence kept — c1's embedding_input comes from "A | pool",
        # NOT "B | room".
        assert result["embeddings"]["c1"]["input"] == "A | pool"
        # One WARNING log line per dropped duplicate.
        dup_logs = [r for r in caplog.records if "Duplicate clip_id" in r.message]
        assert len(dup_logs) == 1
        assert "'c1'" in dup_logs[0].message
        # OpenRouter stub was called exactly once with 2 inputs (deduped),
        # not 3 — duplicate never hit the API.
        assert stub_openai.calls == 1
        assert stub_openai.last_inputs == ["A | pool", "C | spa"]

    def test_no_duplicate_logs_on_unique_batch(self, stub_openai, tmp_path, caplog):
        from promo.core.assign import clip_embedder

        batch = [
            {"id": "c1", "scene_description": "a", "category": "pool"},
            {"id": "c2", "scene_description": "b", "category": "spa"},
        ]
        with caplog.at_level("WARNING"):
            clip_embedder.embed_clips_for_poi(
                batch, cache_dir=str(tmp_path), mimo_prompt_sha1="abcd0002",
            )
        dup_logs = [r for r in caplog.records if "Duplicate clip_id" in r.message]
        assert dup_logs == []

class TestSprint12aAuditFixL3ClipIdCollision:
    """L-3 — _collect_clip_paths logs a WARNING on clip-id collision, matching
    backend.py:268-273 precedent."""

    def test_collision_logs_warning_and_keeps_first(self, tmp_path, caplog):
        """Two .mp4 files whose filenames extract to the same 4-digit id:
        the sorted-first one wins, the second emits a WARNING."""
        from promo.cli.build_embedding_index import _collect_clip_paths

        # Both filenames extract to '0042' via the fallback regex (no "clip_"
        # prefix adjacent to the digits). sorted() picks 'a...' before 'b...'.
        (tmp_path / "a_0042.mp4").write_bytes(b"")
        (tmp_path / "b_0042.mp4").write_bytes(b"")
        (tmp_path / "clip_0055.mp4").write_bytes(b"")

        with caplog.at_level("WARNING"):
            result = _collect_clip_paths(str(tmp_path))

        # First-wins: 'a_0042.mp4' mapped to '0042'; '0055' distinct; 'b_0042' dropped.
        assert set(result.keys()) == {"0042", "0055"}
        assert result["0042"].endswith("a_0042.mp4")

        collision_logs = [
            r for r in caplog.records if "Clip ID collision" in r.message
        ]
        assert len(collision_logs) == 1
        msg = collision_logs[0].message
        assert "'0042'" in msg
        assert "a_0042.mp4" in msg  # already-mapped
        assert "b_0042.mp4" in msg  # skipped

    def test_non_clip_files_silently_skipped(self, tmp_path, caplog):
        """Files without an extractable clip_id produce no WARNING — they're
        simply not clips."""
        from promo.cli.build_embedding_index import _collect_clip_paths

        (tmp_path / "README.mp4").write_bytes(b"")  # no 4-digit group
        (tmp_path / "clip_0001.mp4").write_bytes(b"")

        with caplog.at_level("WARNING"):
            result = _collect_clip_paths(str(tmp_path))

        assert set(result.keys()) == {"0001"}
        collision_logs = [
            r for r in caplog.records if "Clip ID collision" in r.message
        ]
        assert collision_logs == []

class TestSprint12aAuditFixL5SlugValidation:
    """L-5 — build_index_for_poi rejects unsafe slugs unconditionally;
    library entry point is the security boundary, not main()."""

    def test_validate_slug_rejects_traversal(self):
        from promo.cli.build_embedding_index import _validate_slug

        for bad in ("../etc", "../../etc", "foo/../bar", "foo..bar"):
            with pytest.raises(ValueError, match=r"\.\."):
                _validate_slug(bad)

    def test_validate_slug_rejects_path_separators(self):
        from promo.cli.build_embedding_index import _validate_slug

        for bad in ("hotel/bad", "hotel\\bad", "/abs/path", "\\\\server\\share"):
            with pytest.raises(ValueError, match="separator"):
                _validate_slug(bad)

    def test_validate_slug_rejects_leading_dot_and_empty(self):
        from promo.cli.build_embedding_index import _validate_slug

        with pytest.raises(ValueError, match="empty"):
            _validate_slug("")
        with pytest.raises(ValueError, match="hidden"):
            _validate_slug(".hidden")
        with pytest.raises(ValueError, match="hidden"):
            _validate_slug(".")

    def test_validate_slug_accepts_canonical_material_slugs(self):
        from promo.cli.build_embedding_index import _validate_slug

        # Must NOT raise for any of the current active POI slugs.
        for ok in (
            "hotel-xcaret-arte",
            "ocean-key-resort-spa",
            "old-faithful-inn",
            "jashita-hotel-tulum",  # retired but fixture-referenced
            "a",                     # minimal slug
            "a-b-c-1-2-3",
        ):
            _validate_slug(ok)  # no raise

    def test_main_normalizes_display_name_to_hyphenated_material_slug(self, monkeypatch):
        from promo.cli import build_embedding_index as cli

        called: dict[str, str] = {}

        monkeypatch.setattr(cli, "load_dotenv", lambda: None)
        monkeypatch.setattr(
            "promo.core.logging_config.configure_logging",
            lambda: None,
        )

        def fake_build_index_for_poi(slug: str, *, material_root: str):
            called["slug"] = slug
            called["material_root"] = material_root
            return {}

        monkeypatch.setattr(cli, "build_index_for_poi", fake_build_index_for_poi)

        rc = cli.main(["--poi", "Hotel Xcaret Arte"])
        assert rc == 0
        assert called == {
            "slug": "hotel-xcaret-arte",
            "material_root": "material",
        }

    def test_build_index_for_poi_rejects_traversal_at_library_boundary(self, tmp_path):
        """Sprint 12b library callers must NOT be able to bypass validation
        by calling build_index_for_poi directly (the CLI-level normalize in
        main() is a convenience, not a security boundary)."""
        from promo.cli.build_embedding_index import build_index_for_poi

        with pytest.raises(ValueError, match=r"\.\."):
            build_index_for_poi("../etc", material_root=str(tmp_path))


class TestSprint17BEmbedderStaleInputReembed:
    """Sprint 17 B — embed_clips_for_poi reuse decision now compares the
    cached `entry["input"]` text against the freshly-composed text.
    Sidecar-filename axes catch global drift; per-clip surgical edits to
    the upstream MiMo cache (operator pathway documented in
    feedback_mimo_luxury_bias) leave the filename unchanged. Without this
    check, the embedding cache silently serves the pre-edit vector forever.
    """

    def _seed_sidecar(self, tmp_path, sha1, embeddings):
        """Write a sidecar with the given embeddings dict."""
        from promo.core.assign import clip_embedder
        path = clip_embedder.sidecar_path(
            str(tmp_path), sha1, clip_embedder.COMPOSITION_VERSION,
        )
        os.makedirs(os.path.dirname(path), exist_ok=True)
        payload = {
            "model": clip_embedder.EMBEDDING_MODEL,
            "dim": clip_embedder.EMBEDDING_DIM,
            "mimo_prompt_sha1": sha1,
            "composition_version": clip_embedder.COMPOSITION_VERSION,
            "embeddings": embeddings,
        }
        with open(path, "w") as f:
            json.dump(payload, f)
        return path

    def test_stale_input_triggers_reembed_and_updates_sidecar(
        self, stub_openai, tmp_path, caplog,
    ):
        """AC3 — when the cached input differs from the freshly-composed
        input, the clip is re-embedded, the sidecar's `input` field is
        updated to the new text, and the vector is the new value."""
        from promo.core.assign import clip_embedder

        sha1 = "abcd1234"
        # Stale text: reflects an old MiMo cache value before a surgical edit.
        stale_input = "stale text | scenic"
        stale_vector = [0.1] * 1536
        self._seed_sidecar(
            tmp_path, sha1, {"0001": {"vector": stale_vector, "input": stale_input}},
        )

        # New clip metadata composes to a different input.
        clips = [{"id": "0001", "scene_description": "new text", "category": "pool"}]
        composed = clip_embedder.compose_embedding_text(clips[0])
        assert composed == "new text | pool"
        assert composed != stale_input

        with caplog.at_level("WARNING", logger="promo.core.assign.clip_embedder"):
            result = clip_embedder.embed_clips_for_poi(
                clips, cache_dir=str(tmp_path), mimo_prompt_sha1=sha1,
            )

        # (a) embed_texts was called for that clip — stub_openai tracks calls.
        assert stub_openai.calls == 1
        assert stub_openai.last_inputs == [composed]
        # (b) clips_embedded reflects the re-embed.
        assert result["stats"]["clips_embedded"] >= 1
        # (c) on-disk sidecar's input field updates to the new composed text;
        #     the vector is the new value (stub returns one-hot, not [0.1]*1536).
        with open(result["sidecar_path"]) as f:
            saved = json.load(f)
        assert saved["embeddings"]["0001"]["input"] == composed
        assert saved["embeddings"]["0001"]["vector"] != stale_vector

    def test_steady_state_match_makes_zero_api_calls(
        self, stub_openai, tmp_path, caplog,
    ):
        """AC4 — when every cached `input` matches the composed input,
        embed_texts is not called, cache_hits == len(clip_metadata),
        clips_embedded == 0."""
        from promo.core.assign import clip_embedder

        sha1 = "abcd1234"
        clips = [
            {"id": "0001", "scene_description": "pool view", "category": "pool"},
            {"id": "0002", "scene_description": "lobby view", "category": "lobby"},
        ]
        embeddings = {}
        for clip in clips:
            composed = clip_embedder.compose_embedding_text(clip)
            embeddings[str(clip["id"])] = {"vector": [0.0] * 1536, "input": composed}
        self._seed_sidecar(tmp_path, sha1, embeddings)

        with caplog.at_level("WARNING", logger="promo.core.assign.clip_embedder"):
            result = clip_embedder.embed_clips_for_poi(
                clips, cache_dir=str(tmp_path), mimo_prompt_sha1=sha1,
            )

        assert stub_openai.calls == 0
        assert result["stats"]["cache_hits"] == len(clips)
        assert result["stats"]["clips_embedded"] == 0
        # No staleness WARNINGs on the steady-state path.
        stale_logs = [
            r for r in caplog.records
            if r.name == "promo.core.assign.clip_embedder"
            and ("stale" in r.message or "input mismatch" in r.message)
        ]
        assert stale_logs == []

    def test_stale_input_warning_names_clip_id_and_reason(
        self, stub_openai, tmp_path, caplog,
    ):
        """AC5 — staleness re-embed emits a WARNING-level log line that
        names the affected clip_id plus a substring identifying the
        staleness reason ("stale" / "input mismatch" / "input changed")."""
        from promo.core.assign import clip_embedder

        sha1 = "abcd5678"
        self._seed_sidecar(
            tmp_path, sha1,
            {"0001": {"vector": [0.0] * 1536, "input": "old | pool"}},
        )

        clips = [{"id": "0001", "scene_description": "new", "category": "pool"}]
        with caplog.at_level("WARNING", logger="promo.core.assign.clip_embedder"):
            clip_embedder.embed_clips_for_poi(
                clips, cache_dir=str(tmp_path), mimo_prompt_sha1=sha1,
            )

        warning_records = [
            r for r in caplog.records
            if r.name == "promo.core.assign.clip_embedder" and r.levelname == "WARNING"
        ]
        assert warning_records, "no WARNING record emitted"
        # AC5 verify: the line must name the clip_id AND a staleness substring.
        pattern = re.compile(
            r"0001.*(stale|input mismatch|input changed)"
            r"|(stale|input mismatch|input changed).*0001",
            re.IGNORECASE,
        )
        matches = [r for r in warning_records if pattern.search(r.message)]
        assert matches, (
            f"no WARNING line matches clip_id+reason pattern; "
            f"records: {[r.message for r in warning_records]}"
        )

    def test_partial_stale_only_reembeds_changed_clip(
        self, stub_openai, tmp_path, caplog,
    ):
        """B regression guard: when one clip's input is stale and another
        is fresh, only the stale clip re-embeds. Steady-state clip stays
        cached; the merged sidecar holds both."""
        from promo.core.assign import clip_embedder

        sha1 = "ddee0001"
        clips = [
            {"id": "0001", "scene_description": "pool view", "category": "pool"},
            {"id": "0002", "scene_description": "lobby view", "category": "lobby"},
        ]
        # Seed: 0001 is stale ("old"), 0002 matches.
        self._seed_sidecar(
            tmp_path, sha1,
            {
                "0001": {"vector": [0.5] * 1536, "input": "old | pool"},
                "0002": {
                    "vector": [0.7] * 1536,
                    "input": clip_embedder.compose_embedding_text(clips[1]),
                },
            },
        )

        result = clip_embedder.embed_clips_for_poi(
            clips, cache_dir=str(tmp_path), mimo_prompt_sha1=sha1,
        )

        # Only the stale clip re-embedded.
        assert stub_openai.calls == 1
        assert stub_openai.last_inputs == ["pool view | pool"]
        assert result["stats"]["clips_embedded"] == 1
        assert result["stats"]["cache_hits"] == 1
        # Merged sidecar still carries both ids; 0002's vector unchanged.
        with open(result["sidecar_path"]) as f:
            saved = json.load(f)
        assert set(saved["embeddings"].keys()) == {"0001", "0002"}
        assert saved["embeddings"]["0002"]["vector"] == [0.7] * 1536
        assert saved["embeddings"]["0001"]["input"] == "pool view | pool"

    def test_warning_uses_future_tense_before_api_call(
        self, stub_openai, tmp_path, caplog,
    ):
        """Audit-fix D-001: the WARNING fires BEFORE embed_texts is
        called. If the API call raises (network error), the operator
        log must NOT claim past-tense action ('re-embedding' / 'has
        been re-embedded'). Future tense ('scheduling re-embed' /
        'will re-embed') is honest — past tense overstates outcome."""
        from promo.core.assign import clip_embedder

        sha1 = "ee99ee99"
        self._seed_sidecar(
            tmp_path, sha1,
            {"0001": {"vector": [0.0] * 1536, "input": "old | pool"}},
        )
        clips = [{"id": "0001", "scene_description": "new", "category": "pool"}]
        with caplog.at_level("WARNING", logger="promo.core.assign.clip_embedder"):
            clip_embedder.embed_clips_for_poi(
                clips, cache_dir=str(tmp_path), mimo_prompt_sha1=sha1,
            )
        warning_msgs = [
            r.message for r in caplog.records
            if r.name == "promo.core.assign.clip_embedder" and r.levelname == "WARNING"
        ]
        assert warning_msgs, "no WARNING emitted"
        # The line must read as future-tense (action will happen)
        # rather than past-tense (action has happened).
        joined = " ".join(warning_msgs).lower()
        assert "scheduling re-embed" in joined or "will re-embed" in joined, (
            f"WARNING uses non-future tense (audit-fix D-001 regression): {warning_msgs}"
        )

