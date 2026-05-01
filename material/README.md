# material/

Operator-supplied raw input material for promo generation.

## Convention

One subdirectory per POI, named with a kebab-case slug:

```
material/
  <poi-slug>/
    clips/
      *.mp4          # raw clips for this POI
    notes.md         # optional: source, shoot notes, constraints
```

Example:

```
material/
  little-palm-island-resort/
    clips/
      clip_0001.mp4
      clip_0002.mp4
      ...
```

## Rules

- This directory is **gitignored**. Never commit real material.
- Only `README.md` and `.gitkeep` under `material/` are tracked.
- Raw material is expected to be dropped here by the operator before a promo run.
- The standalone compiler picks it up via `--local-clips material/<poi-slug>/clips`.

## Clip Pool Minimums

From `promo/core/format_profiles.py`:

| Format | Target | Min clips | Recommended |
| --- | --- | --- | --- |
| short | ~30s | 8 | 10+ |
| long | ~65s | 14 | 18+ |

Supplying ~30 clips per POI comfortably covers both formats.
