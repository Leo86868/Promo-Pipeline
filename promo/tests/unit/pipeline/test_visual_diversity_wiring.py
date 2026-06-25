"""Regression: 工单② armed visual-vector fetch must reach the production
candidate_only path.

Before the fix, ``_fetch_visual_vectors_if_armed`` did not exist and the visual
fetch lived only inside ``_retrieve_shared_asset_candidates`` (the shared_assets
fallback branch). Production runs candidate_only_mode, which calls
``_retrieve_shared_asset_candidates_from_ready_assets`` directly and never
fetched visual vectors — so armed ② silently degraded to relevance-only
(visual_pool=0). These tests pin the wiring contract.
"""

from __future__ import annotations

from types import SimpleNamespace

from promo.core.pipeline.pipeline import _fetch_visual_vectors_if_armed


class _RecordingBackend:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def visual_vectors_for_assets(self, asset_ids: list[str]) -> dict[str, list[float]]:
        self.calls.append(list(asset_ids))
        return {"asset_001": [1.0, 0.0], "asset_003": [0.0, 1.0]}


def _ready(asset_id: str) -> SimpleNamespace:
    return SimpleNamespace(asset_id=asset_id)


def test_off_returns_none_without_touching_backend(monkeypatch):
    monkeypatch.delenv("PROMO_DOWNLOAD_DIVERSITY", raising=False)
    backend = _RecordingBackend()
    out = _fetch_visual_vectors_if_armed(backend, [_ready("asset_001")])
    assert out is None
    assert backend.calls == []  # no extra DB read when the flag is off


def test_armed_fetches_visual_vectors_keyed_by_asset_id(monkeypatch):
    monkeypatch.setenv("PROMO_DOWNLOAD_DIVERSITY", "1")
    backend = _RecordingBackend()
    ready = [_ready("asset_001"), _ready("asset_002"), _ready("asset_003")]
    out = _fetch_visual_vectors_if_armed(backend, ready)
    # Passed the ready-pool asset_id space (the same one _diverse_download_asset_ids
    # filters on) and returned the backend's vectors.
    assert backend.calls == [["asset_001", "asset_002", "asset_003"]]
    assert set(out) == {"asset_001", "asset_003"}


def test_armed_without_reader_fails_open_to_none(monkeypatch):
    monkeypatch.setenv("PROMO_DOWNLOAD_DIVERSITY", "1")
    backend = SimpleNamespace()  # no visual_vectors_for_assets attribute
    out = _fetch_visual_vectors_if_armed(backend, [_ready("asset_001")])
    assert out is None
