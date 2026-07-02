"""Per-beat before/after diff (greedy vs global) on real sidecars — shows
exactly which beats change clip and the text-cosine before/after. The concrete
'allocation miss resolved' evidence. ZERO writes; embeds read-only."""
from __future__ import annotations
import math, os, sys
import research_verify_production as V

ROOTS = sys.argv[1:] or ["."]
V.ROOTS = ROOTS
from promo.core.assign.packer import pack_clips

dirs = V.find_videos()
shown = 0
for d in dirs:
    try:
        rec = V.load(d)
    except Exception:
        continue
    if not rec:
        continue
    rankings = V.build_rankings(rec)
    beats = rec["beats"]
    wts = V.wts_for(len(beats))
    common = dict(word_timestamps=wts, clip_durations=rec["durations"],
                  clip_metadata=rec["metadata"], max_beat_sec=4.0)
    try:
        g_a, _ = pack_clips(beats, rankings, **common)
        gl_a, gl_p = pack_clips(beats, rankings, global_assignment=True, **common)
    except Exception as e:
        continue
    score = [{str(c).zfill(4): s for c, s in r} for r in rankings]
    cats = {m["id"]: m["category"] for m in rec["metadata"]}
    qtext = rec["queries"]
    diffs = []
    for bi in range(len(beats)):
        gc, lc = g_a[bi]["clip_id"], gl_a[bi]["clip_id"]
        if gc != lc:
            gs = score[bi].get(gc, float("-inf"))
            ls = score[bi].get(lc, float("-inf"))
            diffs.append((bi, qtext[bi][:42], gc, gs, lc, ls, ls - gs))
    if not diffs:
        continue
    name = d.split("/")[-2] + "/" + d.split("/")[-1]
    print(f"\n### {name}  ({len(beats)} beats, {len(diffs)} reassigned)")
    for bi, q, gc, gs, lc, ls, delta in diffs:
        flag = "  <== lifted" if delta > 0.05 else ("  (cat-swap)" if abs(delta) <= 0.05 else "")
        print(f"  b{bi:2d} {q:44s} greedy {gc}({gs:+.3f}) -> global {lc}({ls:+.3f}) "
              f"d={delta:+.3f} cat[{cats.get(gc,'')[:6]}->{cats.get(lc,'')[:6]}]{flag}")
    shown += 1
    if shown >= 3:
        break
