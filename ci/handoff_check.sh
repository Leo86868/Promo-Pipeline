#!/usr/bin/env bash
# Reproducible green-path entrypoint.
#
# Default: runs non-live pytest + smoke_local_render --dry-run. No external
# vendor calls. Exits 0 on success, non-zero on any failure.
#
# --full: additionally runs `pip install -e . --dry-run` (packaging gate)
# and a live compile_promo against an active material POI. Requires .env
# populated with OPENROUTER_API_KEY + GEMINI_API_KEY at minimum.

set -euo pipefail

FULL=0
case "${1:-}" in
    --full)
        FULL=1
        ;;
    "")
        ;;
    *)
        echo "unknown arg: $1 (expected: --full)" >&2
        exit 2
        ;;
esac

# Anchor to the repo root regardless of caller CWD.
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# Pre-smoke clip-stub generation: smoke_local_render asserts 4 clip mp4s
# named clip_0001.mp4..clip_0004.mp4 exist alongside sample-video.mp4.
# Each is a hard-copy of sample-video.mp4 (`cp -n` is idempotent).
for i in 0001 0002 0003 0004; do
  cp -n promo/remotion/public/sample-video.mp4 "promo/remotion/public/clip_${i}.mp4"
done

echo "==> [1/2] pytest -m 'not live'"
python3 -m pytest -m "not live" -q

echo
echo "==> [2/2] smoke_local_render --dry-run"
python3 -m promo.cli.smoke_local_render \
  --local-clips promo/remotion/public \
  --dry-run

if [[ $FULL -eq 1 ]]; then
  echo
  echo "==> [--full pre-check] pip install -e . --dry-run (packaging gate)"
  python3 -m pip install -e . --dry-run

  # Active POI: change to whichever slug currently lives under material/.
  # Fail fast if the pool is missing rather than burning Gemini #1 + TTS
  # credits before the pipeline hits the clip-fetch step.
  ACTIVE_POI="Ocean Key Resort & Spa"
  ACTIVE_SLUG="ocean-key-resort-spa"
  if [[ ! -d "material/${ACTIVE_SLUG}/clips" ]]; then
    echo "--full: material/${ACTIVE_SLUG}/clips not found" >&2
    echo "--full: populate the active POI pool before re-running" >&2
    exit 3
  fi

  RUN_STAMP=$(date -u +"%Y%m%dT%H%M%SZ")
  FULL_OUTPUT_DIR="output/handoff-check/${RUN_STAMP}"
  echo
  echo "==> [--full] compile_promo on ${ACTIVE_POI} (live vendors)"
  echo "    output dir: ${FULL_OUTPUT_DIR}"
  python3 -m promo.cli.compile_promo \
    --poi "${ACTIVE_POI}" \
    --local-clips "material/${ACTIVE_SLUG}/clips" \
    --target-duration-sec 30 \
    --n-variants 1 \
    --output-dir "${FULL_OUTPUT_DIR}"
fi

echo
echo "handoff_check: OK"
