import sys, math
sys.argv = ["x", sys.argv[1] if len(sys.argv) > 1 else "."]
import research_packer_spike as R

vids = R.load_videos()
# 1) does global resolve the 12 dramatic misses?
resolved = stillbad = 0
# 2) monotony: longest same-category run per arm, pooled
import numpy as np

def runs(picks, clips):
    longest = 0
    cur = 1
    prev = None
    triple_plus = 0
    for cid in picks:
        cat = clips[cid]["category"]
        if prev is not None and cat == prev:
            cur += 1
        else:
            if cur >= 3:
                triple_plus += 1
            cur = 1
        prev = cat
    if cur >= 3:
        triple_plus += 1
    return triple_plus

g_trip = a_trip = act_trip = 0
for v in vids:
    rec = R.build(v)
    idx = {c: i for i, c in enumerate(rec["clip_ids"])}
    clips = rec["clips"]
    actual = [b["chosen"] for b in rec["beats"]]
    g_glob = R.global_assign(rec)
    g_on = R.greedy(rec, adjacency=True)
    g_off = R.greedy(rec, adjacency=False)
    # check resolution of dramatic misses under global
    for bi, b in enumerate(rec["beats"]):
        bf = R.best_feasible(rec, bi)
        sc = rec["S"][bi, idx[b["chosen"]]]
        if math.isfinite(bf) and (bf - sc) >= 0.15 and sc < 0.25:
            gsc = rec["S"][bi, idx[g_glob[bi]]]
            if (bf - gsc) < 0.15 or gsc >= 0.25:
                resolved += 1
            else:
                stillbad += 1
    g_trip += runs(g_glob, clips)
    a_trip += runs(g_off, clips)
    act_trip += runs(actual, clips)

print("Global resolves dramatic misses: resolved=%d still_bad=%d" % (resolved, stillbad))
print("Runs of >=3 same-category in a row (clusters):  actual=%d  adj_off=%d  global=%d"
      % (act_trip, a_trip, g_trip))
