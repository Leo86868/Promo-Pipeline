"""Tests for atomic_write_text — the durability primitive under the
in-progress POI lock's read targets (selection_summary.json / RUN_RECEIPT.json).
"""

import os

import pytest

from promo.core.atomic_io import atomic_write_text


def test_atomic_write_produces_complete_file(tmp_path):
    target = tmp_path / "sub" / "out.json"  # parent created on the fly
    atomic_write_text(target, '{"k": "v"}\n')
    assert target.read_text(encoding="utf-8") == '{"k": "v"}\n'
    # no stray temp files left behind
    assert list((tmp_path / "sub").glob(".*.tmp")) == []


def test_failure_leaves_existing_target_untouched_and_cleans_tmp(tmp_path, monkeypatch):
    target = tmp_path / "out.json"
    target.write_text("OLD\n", encoding="utf-8")

    def boom(src, dst):
        raise OSError("simulated replace failure")

    monkeypatch.setattr("promo.core.atomic_io.os.replace", boom)
    with pytest.raises(OSError, match="simulated replace failure"):
        atomic_write_text(target, "NEW-half-written")

    # real file unchanged (reader never sees a partial), temp cleaned up
    assert target.read_text(encoding="utf-8") == "OLD\n"
    assert list(tmp_path.glob(".*.tmp")) == []


def test_failure_when_target_absent_leaves_no_partial(tmp_path, monkeypatch):
    target = tmp_path / "out.json"

    monkeypatch.setattr(
        "promo.core.atomic_io.os.replace",
        lambda src, dst: (_ for _ in ()).throw(OSError("boom")),
    )
    with pytest.raises(OSError):
        atomic_write_text(target, "should not land")

    assert not target.exists()           # no partial real file
    assert list(tmp_path.glob(".*.tmp")) == []  # temp cleaned
