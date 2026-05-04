# promo/core/render/ — Stage 5: Python → Remotion bridge

The final stage. Reads the Gemini #2 clip assignments + the TTS narration + the BGM track, builds a `props.json` for the Remotion `HotelPromo` composition, validates it, and shells out to `npx remotion render` to produce the final `.mp4`. This is the **only module that knows about `final_display_end` and the bridge pool** — everything past `narration_end` lives here. See [/architecture.md](../../../architecture.md) "Two-space model" for the assigner-vs-renderer space invariant.

## Files (inventory)

| File | Role |
|---|---|
| `__init__.py` | Stage marker; no exports. |
| `remotion_renderer.py` | The whole stage. Public: `build_props_from_script`, `validate_props`, `stage_media`, `render_promo`. Private: `_bind_clips_to_narration` (the bridge-mechanism hot path). Module exports `REMOTION_DIR` resolving the sibling `promo/remotion/` TypeScript project. |

## How it wires together

**Cross-file seams:**

- Imports `HARD_CONSTRAINT_TOL_SEC` from `assign/clip_assigner` so the renderer-side display-span check uses the same 50ms tolerance as the assigner.
- Types against `schema.{ClipAssignment, Narration, ScriptSegment, SegmentTimestamp, WordTimestamp}` for the Python-side payload contracts.
- Raises `errors.FreezeWouldOccurError` when the bridge pool is empty AND the last clip's source has been exhausted before `final_display_end`. No retry — the variant aborts.
- Consumed by `pipeline/variant_loop` for every successful Gemini #2 → Remotion handoff (`build_props_from_script` → `validate_props` → `stage_media` → `render_promo`).
- `REMOTION_DIR` resolves the sibling `promo/remotion/`; `npx remotion render` is invoked from there.

**Invariants:**

- **Renderer space ceiling = `final_display_end = max(target_duration_sec, narration_end)`** — the renderer is the only module that knows this. The buffer between `narration_end` and `target_duration_sec` (`bridge_tail`) is renderer territory; assigner space stops at `narration_end`.
- **Bridge mechanism** — `_bind_clips_to_narration` first extends the last clip past `narration_end` (when its source allows). When source runs out, it pulls clips from the unused-clip pool to fill the remaining tail. A `bridge_tail > 0` with **0 bridges fired** is a healthy render — the last clip's source happened to cover the delta.
- **`FreezeWouldOccurError` is the only freeze-prevention exit** — replaces the pre-Sprint-09a "log and continue into a freeze-prone render" path. Loud failure, no silent fallback.
- **`ffmpeg` is a required system dependency** — the renderer itself only shells out to Remotion, but Remotion's encode pipeline plus the `narrate/` stage's audio assembly both depend on `ffmpeg` being on `PATH`.
- **Vertical 1080×1920 / 30 fps defaults** — short-form vertical promo. BGM volume 0.35 (ducked to 0.08 during narration with a 0.3s ramp).
