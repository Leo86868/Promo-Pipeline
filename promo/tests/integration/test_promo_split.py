"""Sprint 15 split-hygiene guards (AC11).

Two trivial invariants that fail if the monolith is resurrected or the
conftest.py drifts away from the canonical single-line sys.path.insert.
"""

from pathlib import Path


def test_monolith_deleted():
    assert not (Path(__file__).resolve().parent.parent / "test_promo_module.py").exists()


def test_conftest_carries_sys_path_insert():
    conftest = (Path(__file__).resolve().parent.parent / "conftest.py").read_text()
    assert conftest.count("sys.path.insert") == 1
