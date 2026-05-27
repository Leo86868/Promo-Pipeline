# Music Library Contract

PGC's production BGM source should be `public.music_library`, not the bundled
`promo/remotion/public/bgm.mp3` fixture.

## Minimum V1 Column

Add one audited duration column:

```sql
alter table public.music_library
add column if not exists duration_sec double precision;

do $$
begin
  if not exists (
    select 1
    from pg_constraint
    where conname = 'music_library_duration_sec_positive'
  ) then
    alter table public.music_library
    add constraint music_library_duration_sec_positive
    check (duration_sec is null or duration_sec > 0);
  end if;
end $$;
```

`duration_sec` is measured once during backfill with `ffprobe` against the
actual audio file referenced by `drive_file_id`.

## Runtime Rule

For a target video duration `target_duration_sec`, PGC should only select rows
where:

```sql
duration_sec >= target_duration_sec
```

This prevents a 65 second render from receiving a 60 second or placeholder BGM
track. The old local `--bgm-dir` ffprobe filtering remains a legacy/local guard,
but the Supabase Music Library path should trust the audited `duration_sec`.

## Current Transport

Current rows use Google Drive:

```text
drive_file_id -> https://drive.google.com/uc?export=download&id=<drive_file_id>
```

Future migrations may copy music into Supabase Storage, but that is not needed
for the first 65 second proof.

## Dry-Run Probe

Generate duration evidence without writing to Supabase:

```bash
python3 -m promo.cli.probe_music_library_durations --limit 5
python3 -m promo.cli.probe_music_library_durations --limit 5 --sql
```

The `--sql` output is intentionally printed for operator review; this command
does not execute updates.
