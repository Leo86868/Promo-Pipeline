# VPS readiness runbook

Use this runbook to verify a clean local -> GitHub -> VPS cycle for the promo
pipeline. The default checks make no vendor calls.

## Local clean check

```bash
git status --short
python3 -m pytest -m "not live" -q
./ci/handoff_check.sh
```

Optional no-vendor render smoke:

```bash
tmpdir="$(mktemp -d "${TMPDIR:-/tmp}/promo-smoke-clips.XXXXXX")"
trap 'rm -rf "$tmpdir"' EXIT
for i in 0001 0002 0003 0004; do
  cp promo/remotion/public/sample-video.mp4 "${tmpdir}/${i}.mp4"
done
python3 -m promo.cli.smoke_local_render --local-clips "$tmpdir"
```

## VPS check

On the VPS, from a fresh checkout of the GitHub branch:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.lock
pip install -e ".[dev]"
( cd promo/remotion && npm ci )

git status --short
./ci/handoff_check.sh
```

For the live batch-readiness smoke, populate one real pool at
`material/<slug>/clips/` and provide a BGM track of at least 60 seconds:

```bash
python3 -m promo.cli.compile_promo \
  --poi "POI Display Name" \
  --local-clips material/<slug>/clips \
  --target-duration-sec 30 \
  --n-variants 1 \
  --voice kore \
  --bgm path/to/bgm-60s-or-longer.mp3 \
  --output-dir output/vps-smoke
```

Expected output is one MP4 plus `tts_metrics_*.json`,
`match_quality_*.json`, and `clip_assignments_*.json`. If Gemini fails with
`400 User location is not supported for the API use`, treat that as a
VPS/vendor access blocker after the no-vendor checks pass.
