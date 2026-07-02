import sys, math
sys.argv = ["x", sys.argv[1] if len(sys.argv) > 1 else "."]
import research_packer_spike as R

vids = R.load_videos()
tot = alloc = 0
lines = []
for v in vids:
    rec = R.build(v)
    idx = {c: i for i, c in enumerate(rec["clip_ids"])}
    clips = rec["clips"]
    actual = [b["chosen"] for b in rec["beats"]]
    aset = set(actual)
    for bi, b in enumerate(rec["beats"]):
        bf = R.best_feasible(rec, bi)
        sc = rec["S"][bi, idx[b["chosen"]]]
        if math.isfinite(bf) and (bf - sc) >= 0.15 and sc < 0.25:
            tot += 1
            bestcid, bests = None, -9.0
            for cid in rec["clip_ids"]:
                if not R.coverable(b["span"], clips[cid]["dur"]):
                    continue
                s = rec["S"][bi, idx[cid]]
                if s > bests:
                    bests, bestcid = s, cid
            ue = (bestcid in aset) and (bestcid != b["chosen"])
            alloc += 1 if ue else 0
            q = repr(b["query"][:36])
            lines.append(
                "  %s/%s b%2d %-40s chosen=%s(%.3f) best=%s(%.3f) gap=%.3f alloc=%s"
                % (v["poi"][:14], v["vid"], bi, q, b["chosen"], sc, bestcid, bests, bf - sc, ue)
            )
print("DRAMATIC MISSES in ACTUAL output (gap>=0.15 & chosen<0.25):")
print("\n".join(lines))
print("TOTAL=%d allocation-fixable=%d coverage=%d" % (tot, alloc, tot - alloc))
