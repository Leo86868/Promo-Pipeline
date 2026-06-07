import json

import pytest


def test_plan_batch_items_isolates_each_video_run(tmp_path):
    from promo.cli.run_batch import BatchPoi, plan_batch_items

    items = plan_batch_items(
        pois=[
            BatchPoi(
                name="Terranea Resort",
                location="Rancho Palos Verdes",
                poi_id="poi_123",
                canonical_key=None,
            )
        ],
        videos_per_poi=3,
        target_duration_sec=65,
        output_root=str(tmp_path),
        voices=["jarnathan", "hope"],
        music_ids=["music_a", "music_b"],
        base_seed=100,
    )

    assert [item.video_index for item in items] == [1, 2, 3]
    assert [item.voice_key for item in items] == ["jarnathan", "hope", "jarnathan"]
    assert [item.music_id for item in items] == ["music_a", "music_b", "music_a"]
    assert [item.seed for item in items] == [100, 101, 102]
    assert all(
        item.output_dir.endswith(f"video_{i:03d}")
        for i, item in enumerate(items, 1)
    )
    assert all("terranea_resort" in item.output_path for item in items)


def test_run_batch_executes_independent_one_video_runs(tmp_path):
    from promo.cli.run_batch import run_batch

    batch_path = tmp_path / "batch.json"
    batch_path.write_text(
        json.dumps({
            "pois": [{
                "poi_id": "poi_123",
                "name": "Terranea Resort",
                "location": "Rancho Palos Verdes",
            }],
            "videos_per_poi": 2,
            "target_duration_sec": 65,
            "voices": ["jarnathan", "hope"],
        }),
        encoding="utf-8",
    )
    commands = []

    def command_runner(command):
        commands.append(list(command))
        return 0

    def music_id_resolver(**kwargs):
        assert kwargs == {"target_duration_sec": 65.0, "count": 2}
        return ["music_a", "music_b"]

    exit_code = run_batch(
        batch_path=str(batch_path),
        output_root=str(tmp_path / "out"),
        use_music_library=True,
        seed=10,
        command_runner=command_runner,
        music_id_resolver=music_id_resolver,
    )

    assert exit_code == 0
    assert len(commands) == 2
    assert all("--n-variants" in command for command in commands)
    assert [command[command.index("--n-variants") + 1] for command in commands] == [
        "1",
        "1",
    ]
    assert [command[command.index("--voice") + 1] for command in commands] == [
        "jarnathan",
        "hope",
    ]
    assert [command[command.index("--seed") + 1] for command in commands] == [
        "10",
        "11",
    ]
    assert [command[command.index("--supabase-music-id") + 1] for command in commands] == [
        "music_a",
        "music_b",
    ]
    assert all("--supabase-poi-id" in command for command in commands)
    assert all(command[command.index("--supabase-poi-id") + 1] == "poi_123" for command in commands)
    assert commands[0][commands[0].index("--output") + 1] != commands[1][
        commands[1].index("--output") + 1
    ]


def test_run_batch_returns_failure_when_any_item_fails(tmp_path):
    from promo.cli.run_batch import run_batch

    batch_path = tmp_path / "batch.json"
    batch_path.write_text(
        json.dumps({
            "pois": [{"poi_id": "poi_123", "name": "Terranea Resort"}],
            "videos_per_poi": 2,
        }),
        encoding="utf-8",
    )
    results = iter([True, False])

    exit_code = run_batch(
        batch_path=str(batch_path),
        output_root=str(tmp_path / "out"),
        target_duration_sec=65,
        command_runner=lambda command: 0 if next(results) else 1,
    )

    assert exit_code == 1


def test_run_batch_rejects_parallel_jobs_until_safe(tmp_path):
    from promo.cli.run_batch import run_batch

    batch_path = tmp_path / "batch.json"
    batch_path.write_text(
        json.dumps({"pois": [{"poi_id": "poi_123", "name": "Terranea Resort"}]}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="jobs 1"):
        run_batch(
            batch_path=str(batch_path),
            output_root=str(tmp_path / "out"),
            target_duration_sec=65,
            jobs=2,
            command_runner=lambda command: 0,
        )


def test_build_compile_command_uses_canonical_key_and_music_library(tmp_path):
    from promo.cli.run_batch import BatchItem, BatchPoi, build_compile_command

    item = BatchItem(
        poi=BatchPoi(
            name="Hotel Maya",
            location="Long Beach",
            poi_id=None,
            canonical_key="hotel_maya",
        ),
        video_index=1,
        output_dir=str(tmp_path),
        output_path=str(tmp_path / "promo.mp4"),
        voice_key="hope",
        music_id=None,
        seed=None,
    )

    command = build_compile_command(
        item=item,
        target_duration_sec=65,
        use_music_library=True,
        script_candidates=2,
        tts_speed=0.92,
    )

    assert command[1:3] == ["-m", "promo.cli.compile_promo"]
    assert command[command.index("--supabase-canonical-key") + 1] == "hotel_maya"
    assert "--supabase-music-library" in command
    assert "--supabase-music-id" not in command
    assert command[command.index("--script-candidates") + 1] == "2"
    assert command[command.index("--tts-speed") + 1] == "0.92"
