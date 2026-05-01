"""Unit tests for promo.core.backend."""

import json
import logging
import os
import re
import shutil
import sys
import tempfile
from unittest.mock import patch, MagicMock

from pathlib import Path

import pytest

class TestLocalBackendOutputPreservation:
    """LocalBackend should preserve variant-specific filenames when saving outputs."""

    def test_save_output_preserves_variant_filenames(self):
        from promo.core.backend import LocalBackend

        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = os.path.join(tmpdir, "saved")
            backend = LocalBackend(clips_dir=tmpdir, output_dir=output_dir)

            variant_one = os.path.join(tmpdir, "promo_test_hotel_v1.mp4")
            variant_two = os.path.join(tmpdir, "promo_test_hotel_v2.mp4")
            Path(variant_one).write_bytes(b"video-one")
            Path(variant_two).write_bytes(b"video-two")

            saved_one = backend.save_output("Test Hotel", variant_one)
            saved_two = backend.save_output("Test Hotel", variant_two)

            assert os.path.basename(saved_one) == "promo_test_hotel_v1.mp4"
            assert os.path.basename(saved_two) == "promo_test_hotel_v2.mp4"
            assert saved_one != saved_two
            assert Path(saved_one).read_bytes() == b"video-one"
            assert Path(saved_two).read_bytes() == b"video-two"


class TestLocalBackendSaveOutputCollisionBump:
    """Sprint 18 C — MP4 collision-bump in LocalBackend.save_output.

    Mirrors ``_write_sidecar``'s ``-N`` algorithm so a back-to-back
    same-POI rerun keeps the prior MP4 on disk under the unbumped name
    and the new MP4 lands at ``<stem>-2.mp4``, paired by suffix with the
    sidecar writer's bumped JSON files.
    """

    def test_save_output_bumps_on_same_destination_collision(self):
        from promo.core.backend import LocalBackend

        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = os.path.join(tmpdir, "saved")
            backend = LocalBackend(clips_dir=tmpdir, output_dir=output_dir)

            v1 = os.path.join(tmpdir, "promo_hotel_x.mp4")
            v2 = os.path.join(tmpdir, "promo_hotel_x.mp4-source-2")
            # Use the same destination basename on both calls (the typical
            # back-to-back same-POI same-duration rerun).
            Path(v1).write_bytes(b"video-one")
            saved_one = backend.save_output("Hotel X", v1)
            # Rewrite v1 with new content for the second call so we can prove
            # run-1's bytes are preserved at the unbumped name.
            Path(v1).write_bytes(b"video-two")
            saved_two = backend.save_output("Hotel X", v1)

            unbumped = os.path.join(output_dir, "promo_hotel_x.mp4")
            bumped = os.path.join(output_dir, "promo_hotel_x-2.mp4")
            assert os.path.isfile(unbumped)
            assert os.path.isfile(bumped)
            assert saved_one == unbumped
            assert saved_two == bumped
            assert Path(unbumped).read_bytes() == b"video-one"
            assert Path(bumped).read_bytes() == b"video-two"

    def test_save_output_bump_chain_three_runs(self):
        from promo.core.backend import LocalBackend

        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = os.path.join(tmpdir, "saved")
            backend = LocalBackend(clips_dir=tmpdir, output_dir=output_dir)
            src = os.path.join(tmpdir, "promo_hotel_x.mp4")
            for n, payload in enumerate([b"a", b"b", b"c"], start=1):
                Path(src).write_bytes(payload)
                backend.save_output("Hotel X", src)
            assert os.path.isfile(os.path.join(output_dir, "promo_hotel_x.mp4"))
            assert os.path.isfile(os.path.join(output_dir, "promo_hotel_x-2.mp4"))
            assert os.path.isfile(os.path.join(output_dir, "promo_hotel_x-3.mp4"))

    def test_save_output_bump_algorithm_mirrors_sidecar_writer(self):
        """Sprint 18 C regression guard: the MP4 collision-bump must use
        the SAME `-N` numbering as `_write_sidecar` (sidecar_writer.py:62-80)
        so that operator-side pairing by suffix is character-identical
        across the MP4 and the three JSON sidecars produced in the same
        run. If a future refactor switches save_output to `_v2.mp4` /
        `(2).mp4` / similar, this test catches the divergence."""
        import os as _os

        from promo.core.backend import LocalBackend
        from promo.core.pipeline.sidecar_writer import _write_sidecar

        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = os.path.join(tmpdir, "saved")
            backend = LocalBackend(clips_dir=tmpdir, output_dir=output_dir)
            src = os.path.join(tmpdir, "promo_x.mp4")
            for payload in (b"v1", b"v2", b"v3"):
                Path(src).write_bytes(payload)
                backend.save_output("X", src)
            # Same algorithm exercised on the sidecar writer in the SAME dir.
            for n in (1, 2, 3):
                _write_sidecar(output_dir, "tts_metrics_x_30s.json",
                               [{"v": n}], "tts_metrics")
            mp4_basenames = sorted(
                p for p in _os.listdir(output_dir) if p.endswith(".mp4")
            )
            json_basenames = sorted(
                p for p in _os.listdir(output_dir)
                if p.startswith("tts_metrics_x_30s")
            )
            # Both writers must produce the SAME `-N` suffix shape.
            assert mp4_basenames == [
                "promo_x-2.mp4", "promo_x-3.mp4", "promo_x.mp4",
            ]
            assert json_basenames == [
                "tts_metrics_x_30s-2.json",
                "tts_metrics_x_30s-3.json",
                "tts_metrics_x_30s.json",
            ]

    def test_save_output_cap_exhausted_raises_oserror(self, caplog):
        from promo.core.backend import LocalBackend

        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = os.path.join(tmpdir, "saved")
            os.makedirs(output_dir, exist_ok=True)
            # Pre-populate the unbumped slot + 998 bumped slots so the next
            # candidate would be `-1000` (beyond the 999 cap).
            Path(os.path.join(output_dir, "promo_hotel_x.mp4")).write_bytes(b"orig")
            for n in range(2, 1000):
                Path(os.path.join(output_dir, f"promo_hotel_x-{n}.mp4")).write_bytes(b"x")
            backend = LocalBackend(clips_dir=tmpdir, output_dir=output_dir)
            src = os.path.join(tmpdir, "promo_hotel_x.mp4")
            Path(src).write_bytes(b"new")

            with caplog.at_level(logging.WARNING, logger="promo.core.backend"):
                with pytest.raises(OSError, match="collision-bump exhausted"):
                    backend.save_output("Hotel X", src)
            # The exception message must include the attempted base name.
            assert any(
                "promo_hotel_x.mp4" in rec.getMessage() and rec.levelname == "WARNING"
                for rec in caplog.records
            )
            # No file at -1000.
            assert not os.path.exists(
                os.path.join(output_dir, "promo_hotel_x-1000.mp4")
            )
