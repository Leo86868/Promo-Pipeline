# `bgm.mp3` — Smoke-test fixture

This file is a deliberately silent placeholder, not a real background track.

- Duration: 2s
- Codec: mp3 / 22050 Hz / mono / 8 kb/s
- Size: < 3 KB
- Generated once via:
  ```
  ffmpeg -f lavfi -i anullsrc=r=22050:cl=mono -t 2 -c:a libmp3lame -b:a 8k bgm.mp3
  ```

Purpose: unblock `python3 -m promo.scripts.smoke_local_render --local-clips
promo/remotion/public --dry-run` (which asserts `public/bgm.mp3` exists) without
committing licensed audio to the repo.

Real BGM lives under `material/<slug>/bgm/` (operator-supplied, gitignored).
Do not replace this placeholder with a real track.
