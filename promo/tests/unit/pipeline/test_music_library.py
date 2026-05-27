from types import SimpleNamespace

import pytest


def _track(**overrides):
    row = {
        "id": "11111111-1111-1111-1111-111111111111",
        "music_name": "Long Track",
        "drive_file_id": "drive_file_1",
        "duration_sec": 90.25,
        "genre": "cinematic",
        "bpm": 100,
        "tags": {"mood": ["warm"]},
        "embedding_text": "warm cinematic travel",
    }
    row.update(overrides)
    return row


class _FakeQuery:
    def __init__(self, rows):
        self.rows = list(rows)
        self.calls = []

    def select(self, columns):
        self.calls.append(("select", columns))
        return self

    def eq(self, field, value):
        self.calls.append(("eq", field, value))
        self.rows = [row for row in self.rows if row.get(field) == value]
        return self

    def gte(self, field, value):
        self.calls.append(("gte", field, value))
        self.rows = [row for row in self.rows if float(row.get(field) or 0) >= value]
        return self

    def order(self, field):
        self.calls.append(("order", field))
        self.rows = sorted(self.rows, key=lambda row: row[field])
        return self

    def limit(self, count):
        self.calls.append(("limit", count))
        self.rows = self.rows[:count]
        return self

    def execute(self):
        self.calls.append(("execute",))
        return SimpleNamespace(data=self.rows)


class _FakeSupabase:
    def __init__(self, rows):
        self.query = _FakeQuery(rows)
        self.tables = []

    def table(self, name):
        self.tables.append(name)
        return self.query


def test_normalize_music_library_track_requires_duration_for_runtime():
    from promo.core.music_library import MusicLibraryError, normalize_music_library_track

    normalized = normalize_music_library_track(_track(), min_duration_sec=65)

    assert normalized["id"] == "11111111-1111-1111-1111-111111111111"
    assert normalized["music_name"] == "Long Track"
    assert normalized["duration_sec"] == 90.25
    with pytest.raises(MusicLibraryError, match="duration_sec"):
        normalize_music_library_track(_track(duration_sec=None), min_duration_sec=65)
    with pytest.raises(MusicLibraryError, match="below target"):
        normalize_music_library_track(_track(duration_sec=60), min_duration_sec=65)


def test_eligible_music_tracks_filters_and_sorts_by_duration():
    from promo.core.music_library import eligible_music_tracks

    rows = [
        _track(music_name="Too Short", duration_sec=30),
        _track(
            id="22222222-2222-2222-2222-222222222222",
            music_name="Just Long Enough",
            duration_sec=65,
        ),
        _track(
            id="33333333-3333-3333-3333-333333333333",
            music_name="Longer",
            duration_sec=80,
        ),
    ]

    assert [row["music_name"] for row in eligible_music_tracks(rows, min_duration_sec=65)] == [
        "Just Long Enough",
        "Longer",
    ]


def test_supabase_music_library_selects_and_downloads_eligible_track(
    monkeypatch,
    tmp_path,
):
    from promo.core import music_library
    from promo.core.music_library import SupabaseMusicLibrary

    short = _track(music_name="Short", duration_sec=30)
    long = _track(
        id="22222222-2222-2222-2222-222222222222",
        music_name="Long",
        duration_sec=70,
        drive_file_id="drive_file_2",
    )
    client = _FakeSupabase([short, long])
    downloaded = []

    def fake_download_drive_file(drive_file_id, dest):
        downloaded.append((drive_file_id, dest))
        with open(dest, "wb") as fh:
            fh.write(b"mp3")

    monkeypatch.setattr(music_library, "download_drive_file", fake_download_drive_file)

    bgm_path = SupabaseMusicLibrary(client, min_duration_sec=65).fetch_bgm(str(tmp_path))

    assert client.tables == ["music_library"]
    assert ("gte", "duration_sec", 65.0) in client.query.calls
    assert bgm_path.endswith("22222222-2222-2222-2222-222222222222.mp3")
    assert (tmp_path / bgm_path.split("/")[-1]).read_bytes() == b"mp3"
    assert downloaded == [("drive_file_2", bgm_path)]


def test_duration_probe_update_sql_is_review_only_shape():
    from promo.cli.probe_music_library_durations import duration_update_sql

    assert duration_update_sql(
        "11111111-1111-1111-1111-111111111111",
        65.1234567,
    ) == (
        "update public.music_library set duration_sec = 65.123457 "
        "where id = '11111111-1111-1111-1111-111111111111';"
    )
