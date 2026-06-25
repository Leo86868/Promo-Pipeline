-- 工单② STEP-1 dry-run harness — visual-diversity download selection.
-- MEASUREMENT ONLY. Does NOT touch the production retrieval path.
--
-- Compares today's "relevance-30" download set against relevance-seeded
-- greedy max-min (whitened DINOv2 visual cosine) across consideration
-- windows {35,45,60}. Reports per selection: near-dup pairs, worst pair,
-- distinct visual clusters, deepest relevance rank reached, top-15 retained.
--
-- Algorithm mirrors workflow/projects/visual-embedding-dedup/prototype/
-- select_diverse.py, with the PGC adaptation: seed = relevance #1 (NOT the
-- prototype's medoid) so the single most-relevant clip is always kept, then
-- max-min spreads from there.
--
-- WHY SQL (not the Python tool): the OpenRouter embedding key 401s (can't
-- re-embed real script queries offline) AND there are no local DB creds —
-- only the read-only Supabase MCP. So relevance here is a labeled PROXY:
-- text-embedding cosine to the POI's own text centroid (most-representative
-- first). The max-min SIDE of the comparison is independent of this proxy.
--
-- Whitening = mean-center over the top-60 working pool, then L2 (handoff
-- "consumer-side whitening"). Same whitened space for every selection so the
-- comparison is apples-to-apples.
--
-- Usage: substitute :POI and :NAME. Pool capped to top-60 by relevance (the
-- widest window). Near-dup threshold 0.85 (handoff "same place"); 0.92 also
-- reported (handoff "same shot").

with recursive elig as (
  select v.asset_id,
         t.embedding_vector::vector(1536) tvec,
         e.embedding_vector::vector(768)  vvec
  from poi_asset_valid_clips v
  join poi_asset_embeddings t
    on t.asset_id = v.asset_id and t.status = 'ready'
  join poi_asset_visual_embeddings e
    on e.asset_id = v.asset_id and e.status = 'ready'   -- visual fail-open: pending rows simply absent
  where v.poi_id = :POI
    and coalesce(v.usage_count, 0) < 3                  -- same hard door as production
    and v.embedding_status = 'ready'
),
cen    as (select avg(tvec) c from elig),
ranked as (select asset_id, vvec,
                  row_number() over (order by 1 - (tvec <=> (select c from cen)) desc) rk
           from elig),
top    as (select * from ranked where rk <= 60),
mu     as (select avg(vvec) m from top),
white  as (select rk, l2_normalize(vvec - (select m from mu)) w from top),

-- greedy farthest-point, relevance-#1 seed, one CTE per window bound.
r35 as (
  select array[1]::bigint[] ch, 1 n
  union all
  select ch || (select a.rk from white a
                where a.rk <= 35 and not (a.rk = any(r35.ch))
                order by (select max(1 - (a.w <=> c.w)) from white c where c.rk = any(r35.ch)) asc, a.rk
                limit 1), n + 1
  from r35 where n < 30),
r45 as (
  select array[1]::bigint[] ch, 1 n
  union all
  select ch || (select a.rk from white a
                where a.rk <= 45 and not (a.rk = any(r45.ch))
                order by (select max(1 - (a.w <=> c.w)) from white c where c.rk = any(r45.ch)) asc, a.rk
                limit 1), n + 1
  from r45 where n < 30),
r60 as (
  select array[1]::bigint[] ch, 1 n
  union all
  select ch || (select a.rk from white a
                where a.rk <= 60 and not (a.rk = any(r60.ch))
                order by (select max(1 - (a.w <=> c.w)) from white c where c.rk = any(r60.ch)) asc, a.rk
                limit 1), n + 1
  from r60 where n < 30),

sets as (
  select 'relevance-30' lbl, (select array_agg(rk) from white where rk <= 30) s
  union all select 'maxmin@35', (select ch from r35 order by n desc limit 1)
  union all select 'maxmin@45', (select ch from r45 order by n desc limit 1)
  union all select 'maxmin@60', (select ch from r60 order by n desc limit 1)
)
select :NAME poi, s.lbl,
  (select count(*) from white a join white b on a.rk < b.rk
     where a.rk = any(s.s) and b.rk = any(s.s) and 1 - (a.w <=> b.w) >= 0.85) ndup85,
  (select count(*) from white a join white b on a.rk < b.rk
     where a.rk = any(s.s) and b.rk = any(s.s) and 1 - (a.w <=> b.w) >= 0.92) ndup92,
  (select round(max(1 - (a.w <=> b.w))::numeric, 3) from white a join white b on a.rk < b.rk
     where a.rk = any(s.s) and b.rk = any(s.s)) worst,
  -- single-linkage cluster count @0.85: a pick is a new cluster unless it is
  -- >=0.85 to an earlier (lower-rank) pick in the set.
  (select count(*) from unnest(s.s) x
     where not exists (select 1 from unnest(s.s) y
                       where y < x and 1 - ((select w from white where rk = x)
                                            <=> (select w from white where rk = y)) >= 0.85)) clusters,
  (select max(x) from unnest(s.s) x)                  deepest_rk,   -- how deep into relevance it reached
  (select count(*) from unnest(s.s) x where x <= 15)  top15_kept    -- recall proxy: top-15 relevant retained
from sets s order by s.lbl;
