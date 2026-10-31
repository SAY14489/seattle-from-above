# Results Log

## M0 -- data access (2026-10-30)

Opened the OlmoEarth v1.3 Zarr store anonymously and inspected `utm12` zone metadata.

**Confirmed facts (previously assumptions in the plan):**
- Decode formula matches exactly: `float = sign(q) * (abs(q)/127.5) ** 2.0`, read straight out of
  the zarr `geoemb:quantization` attrs -- no need to reverse-engineer it.
- `time` axis values are plain years as int32 (`[2017..2025]`), not timestamps.
- Each zone array is `(time=9, band=128, y, x)`, chunked `(1, 64, 256, 256)`, sharded
  `(1, 128, 2048, 2048)` -- one shard = all 128 bands x 20.48km x 20.48km x one year.
- CRS is per-zone UTM (e.g. `utm12` -> EPSG:32612), with a `spatial:transform` affine
  (`geo.py` turns this into lon/lat <-> pixel col/row conversion).
- Coverage check across utm10/32/33/36: 2017-2019 are patchy in several zones (confirms the
  plan's warning), but by 2020-2021 coverage is solid. utm12 (Phoenix, AZ) has full coverage
  back to 2017.

**M0 test:** read a 512x512px (5.12km) patch at Buckeye, AZ for 2017 vs 2025, decoded, computed
per-pixel cosine distance. Output `data/m0_change_score_buckeye.png` shows clear parcel/subdivision
grid structure -- real signal, not noise.

## M1 -- test box pipeline (2026-10-30)

Full pipeline (read -> decode -> per-pixel change -> cell aggregation -> site extraction) run on a
56km x 56km box centered on the Buckeye/Goodyear/Phoenix West Valley corridor, AZ -- a known
fast-growing exurb (farmland -> subdivisions, 2017-2025).

Scoped down from the plan's suggested 200km test box because of local disk headroom (33GB free)
-- a 200km box across 3 years at full 128 bands would be ~150GB of streamed reads. Used the
64-dim matryoshka embedding subset and streamed in 800px tiles with per-tile disk caching
(resumable: reruns skip tiles already fetched), so peak memory and disk stay small regardless
of region size. Full-size regions are a cloud-VM task for M5.

See `data/m1_summary.json` for exact counts; `data/m1_change_ab_overview.png` for the change map.

Results: 3,136 cells (1km²) analyzed, 13,666 raw connected components above the p90 change
threshold, 240 sites after the minimum-area filter (0.25km²).

## M2 -- noise filtering (2026-10-30)

Re-ran site extraction from the already-cached tiles (`m2_filter_sites.py`, zero new network
reads) with two additional filters layered on top of M1's threshold + min-area:

1. **Local z-score baseline**: each pixel's change score is normalized against a 20km-radius
   neighborhood mean/std (via a uniform filter), so a site must stand out from its surroundings,
   not just sit in a generally high-change area. Candidate pool went from 13,666 -> 9,325 blobs.
2. **Persistence test**: using the third year (2021), a site is kept only if its 2021->2025
   change is well below (<=65%) its 2017->2025 change -- i.e. the new state stuck rather than
   reverting. Dropped 130 of the remaining large-enough blobs.

Final: **58 sites**, all in the actual Phoenix West Valley growth corridor (Surprise, Goodyear,
Avondale, Glendale, Buckeye) -- matches the real-world story of this area's exurban growth.

A full 30+30 known-change/known-stable validation set (per the plan) is still open -- the
persistence + z-score filters above are a reasonable first pass, not yet validated against
ground truth.

## M3 -- local web app (2026-10-30)

Astro + MapLibre site running locally (`npm run dev`, http://localhost:4321), reading directly
from the pipeline's exported JSON/GeoJSON (`web/public/data/`) -- no PMTiles/R2 needed at this
scale. Four pages, all verified rendering correctly via a headless-Chromium screenshot check:

- **Map** (`/`): OpenFreeMap basemap, a cell heat layer (mean change score per 1km cell), and
  58 site markers, all clustered correctly over Goodyear/Avondale/Surprise/Buckeye.
- **Top 100** (`/leaderboard`): ranked table with reverse-geocoded place names (Nominatim),
  area, and score; type-filter buttons (site classification is still a placeholder -- M1.5).
- **Site pages** (`/site/{id}`): before/after Sentinel-2 true-color slider (2017 vs 2025, via
  Microsoft Planetary Computer's STAC catalog), a small MapLibre map with the site polygon, and
  auto-generated stats/description. Confirmed on-screen: real farmland -> subdivision imagery.
- **About** (`/about`): methodology, limitations, data/license credits.

**Bugs hit and fixed along the way** (worth knowing if touching this code again):
- `define:vars` scripts in Astro are not processed by Vite, so a bare `import("maplibre-gl")`
  inside one fails to resolve. Fixed by passing data through a `<script type="application/json">`
  tag read by a normal (bundled) `<script>` module instead.
- `maplibre-gl`'s ESM bundle has no default export (only named exports); `import maplibre from
  "maplibre-gl"` throws at runtime under Vite even though it type-checks. Use named imports
  (`Map`, `NavigationControl`, `Marker`, `Popup`).
- Vite's dependency pre-bundling breaks maplibre-gl's internal Web Worker URL resolution in dev
  mode ("Worker failed to load") -- fixed with `vite.optimizeDeps.exclude: ['maplibre-gl']` in
  `astro.config.mjs`.
- maplibre-gl's own stylesheet sets `.maplibregl-map { position: relative }`, which collides
  with Tailwind's `.absolute` utility on the same element (same specificity; cascade order and
  CSS layers made Tailwind lose) and collapsed the map container to zero height. Fixed by giving
  the map container's sizing via inline styles, which beat any stylesheet rule unconditionally.
- `fetch_thumbnails.py`'s windowed COG reads were silently producing empty (0x0) windows for
  every site: `rasterio.windows.from_bounds()` assumes its bounds are already in the raster's
  CRS, but the site bbox is lon/lat (EPSG:4326) while Sentinel-2 COGs are UTM. Fixed by
  reprojecting the bbox with `rasterio.warp.transform_bounds()` before computing the window.
- Thumbnail fetches to Planetary Computer / Azure Blob Storage occasionally stall for several
  minutes per request despite GDAL HTTP timeout env vars; the script tolerates this (catches and
  logs, keeps going) rather than hanging indefinitely, so a full 58-site x 2-year thumbnail run
  takes longer than it "should" but still completes unattended.

**Not done in this build** (explicitly out of scope for tonight's "local webapp" focus):
Cloudflare Pages/R2 deployment (M4), PMTiles conversion, scale-out to a full country/continent on
a cloud VM (M5), change-type classification (M1.5/M6), OG share-card images, launch polish.

## Multi-region: adding Lagos, Nigeria (2026-10-30)

Added a second test region -- Eko Atlantic / Victoria Island / Lekki, Lagos (40km x 40km,
2017/2021/2025) -- picked as a developing-country example with a very dramatic, unambiguous
change signal: Eko Atlantic is a new city built on land reclaimed from the Atlantic Ocean since
~2008, alongside rapid growth along the Lekki corridor. This required restructuring the pipeline
from "one hardcoded config" to multi-region:

- `pipeline/regions/*.yaml` -- one config per region (was a single `config.yaml`); every pipeline
  script now takes a config path as a CLI arg (`uv run python m1_pipeline.py regions/lagos.yaml`).
- `data/<region>/` -- each region's tile cache, cells, and sites now live in their own namespaced
  folder, so running a new region can never clobber another's cached data.
- `export_web_data.py` now merges **every** region in `pipeline/regions/` into one combined
  `web/public/data/` dataset. Site ids are already region-prefixed (e.g.
  `lagos_eko_atlantic-0001`), so merging is collision-free, and sites are globally re-ranked by
  `rank_score` across all regions -- this turns the leaderboard into an actual "Top N worldwide"
  list rather than one list per region, which is the real end-state the plan describes.
- The web app's map now `fitBounds`'s to the combined bbox instead of a fixed center/zoom per
  region, and the leaderboard/site pages show each site's own region, country, and year pair
  (previously a single global `summary.years` / `summary.region_label`).

**Result:** 23 Lagos sites survived M1+M2 filtering (57 pre-persistence-filter, down from 15,467
raw blobs -- the min-area filter alone removes almost everything, as in Phoenix). Merged with
Phoenix's 58: **81 sites total, globally ranked**. The #1 ranked site worldwide is now
"Okun-Ajah, Nigeria" (24.31 km^2, the Eko Atlantic reclamation + Lekki growth area) -- its large
contiguous area outweighs Phoenix sites' higher per-pixel change scores in the `area x score`
ranking formula, exactly as expected for a literal ocean-to-land change.

**Thumbnail quality bug found and fixed**: the first thumbnail pass for Lagos produced
unusable, mostly-cloud images for every site, despite scenes being chosen by lowest *whole-scene*
cloud cover %. Root cause: a Sentinel-2 scene's reported cloud % is for the entire ~100x100km
tile, which says nothing about whether clouds happen to sit over one small ~1-3km crop. Fixed by
reading each candidate scene's SCL (Scene Classification) band for the actual crop window and
picking whichever of the top 6 least-cloudy candidates has the fewest cloud/shadow/nodata pixels
*in that crop specifically*, not just the lowest scene-level %. This cut the worst site's crop
cloud fraction from essentially unusable to 6-14%, and is a generally-applicable fix (any future
tropical/coastal/monsoon region would hit the same problem). Documented as a residual limitation
on the About page: Lagos's imagery still shows some cloud haze, since equatorial coastal areas
genuinely have persistent cloud cover and "best available Sentinel-2 scene" has a floor.

## Multi-region: adding Mount Rainier, WA (2026-10-30)

Added a third region at the user's request (a Washington State example) -- Mount Rainier, WA
(48km x 48km, 2017/2021/2025), picked specifically to add a *different kind* of change to the
leaderboard: climate-driven glacier retreat, as opposed to urban growth (Phoenix) or land
reclamation (Lagos). Verified full data coverage for all three years before running.

**Result:** 58 sites survived M1+M2 filtering (92 pre-persistence-filter, down from 17,846 raw
blobs). Merged total across all three regions: **139 sites, globally ranked**.

**Honest finding, not what was expected:** the top-ranked Rainier sites are not glacier retreat --
they're large, sharply-bounded forest clearings with visible access roads cut into them, i.e.
commercial logging/clear-cutting in the national forest land surrounding the park, not the
glaciated summit itself. Visually confirmed on two of the top Rainier sites (both show dense
conifer canopy in 2017 replaced by bare/exposed ground with road cuts in 2025 -- textbook
clear-cut signature, not a burn scar or snowline pattern). This makes sense in hindsight: canopy
removal is a larger embedding-space change than seasonal/interannual glacier-edge variation, so
it dominates the area x score ranking within this box. Reported to the user as-is rather than
reframed as "glacier retreat" -- the methodology surfaced what's actually the biggest change in
that box, which happens to be logging, and that is itself a useful, different change *type* from
the other two regions (land-use/forestry vs. urban growth vs. reclamation). Glacier-adjacent sites
may still be present lower in the list; not exhaustively checked.
