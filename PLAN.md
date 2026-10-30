# Earth's Biggest Changes: Build Plan

Oct 30, 2026 · @Yusuf

## Goal

Build a public web app that ranks the most transformed places on Earth between two years, using Ai2's free OlmoEarth v1.3 satellite embeddings. Visitors see a world map of change, a "Top 100" leaderboard, and a before/after page for each site.

The product has two halves:
- Offline pipeline (Python): reads embeddings, computes a change score per area, filters noise, groups pixels into sites, and exports static files.
- Static web app (Cloudflare): serves a map and leaderboard from those files. No backend server or database is needed for v1.

**Current focus: get the pipeline + web app running locally first.** Cloudflare deployment (M4/M5) comes after the local version proves out on a test box.

Success for v1: one region (start with a single continent) with a Top 100 list where most entries are real, explainable changes (new cities, solar farms, reservoirs, burn scars, deforestation, new data centers) rather than noise.

## Data source

The input is the OlmoEarth v1.3 Embeddings dataset on Source Cooperative: free, public, no API key.

| Property | Value |
|---|---|
| What | One embedding per 10 m pixel per year, summarizing Sentinel-1, Sentinel-2, and Landsat 8/9 for that year |
| Store | Zarr v3 at `s3://olmoearth/olmoearth-v1-3-embeddings/embeddings.zarr`, endpoint `https://data.source.coop`, anonymous reads |
| Layout | One group per UTM zone, utm01 to utm60; array dims (time, band, y, x) |
| Dimensions | 128 per pixel; the first 64 also work alone as a smaller embedding |
| Data type | int8, quantized; -128 = nodata |
| Decode | Signed square root scheme: `float = sign(q) * (abs(q) / 127.5) ** 2` (verify against the dataset README) |
| Years | 2025 is the latest; earlier years (2017 to 2020) were still backfilling as of late Oct 2026, so check coverage per zone at runtime |
| Size | Roughly 150 TB per year globally, so never download whole years |

Minimal read example (from the dataset docs):

```python
# pip install "zarr>=3" obstore pyproj numpy xarray
import zarr
from obstore.store import S3Store

STORE_URL = "s3://olmoearth/olmoearth-v1-3-embeddings/embeddings.zarr"
s3 = S3Store.from_url(STORE_URL, endpoint="https://data.source.coop", skip_signature=True)
store = zarr.storage.ObjectStore(s3, read_only=True)
root = zarr.open_group(store, mode="r")
zone = root["utm10"]
q = zone["embeddings"][t, :, row, col]  # int8, decode before use
```

**First task for Claude Code:** open the store, print the zarr.json metadata for one zone (years axis, chunk shape, geotransform/CRS attributes), and confirm the decode formula from the README before writing any pipeline code.

## Architecture

All heavy computation happens offline in Python; the website is just static files, so it is cheap to host and impossible to overload.

The pipeline reruns only when you add a region or a new year of embeddings comes out; everything a visitor touches is a file in R2 or Pages.

### Pipeline phases

The pipeline turns two years of embeddings into a ranked list of change sites, in five steps. Everything is driven by one config file (regions as bounding boxes, year pair, thresholds) so runs are reproducible.

1. **Read and decode.** Iterate over Zarr chunks that intersect the region, in parallel with a bounded worker pool. Decode int8 to float, mask nodata, L2-normalize each vector. Cache raw chunks on local disk so reruns are free.
2. **Per-pixel change score.** Cosine distance between year A and year B for every pixel (0 = identical, higher = more change). Also compute the same score for a third year (see Noise filtering) so persistent change can be told apart from one-year flicker.
3. **Aggregate to cells.** Roll pixels up into roughly 1 km cells (100 x 100 pixels). Per cell, store: mean score, 95th percentile score, fraction of pixels above a threshold, and valid pixel count. Write one GeoParquet file per UTM zone. This is the main artifact for the map heat layer.
4. **Extract sites.** In the top cells, threshold high-change pixels at full resolution, merge them with connected components, drop blobs under a minimum area (start at 0.25 km², tune later). Each site gets an id, polygon, centroid, area, mean score, and a ranking score (start with area x mean score).
5. **Label change type (v1.5).** Hand-label about 50 sites into types (new construction, solar farm, water gained, water lost, forest loss, burn scar, agriculture change). Classify the rest with kNN on the concatenated before and after embeddings. Ship v1 without types if this slows things down.

**Scale warning:** 128 bytes per pixel means a 200 x 200 km area is about 51 GB per year. Start with a test box of that size, then a country, and only attempt a continent on a cloud VM close to the data. Check the Zarr chunk and shard shape first: if small byte-range reads are possible, a strided sample (every Nth pixel) can make the coarse pass much cheaper.

### Noise filtering

A raw change ranking will be dominated by noise, so filtering decides whether the leaderboard is interesting. Apply these in order and log how many cells each one removes.

| Noise source | Why it looks like change | Filter |
|---|---|---|
| Bad or thin data | Few valid observations in one year | Drop cells with low valid pixel count in either year |
| One-year flicker | Drought year, snowy year, wet year | Persistence test with 3 years (A, B, C): keep only if A to B is large AND B to C is small (the new state stuck) |
| Crop rotation | Fields switch crops every year | Learn a typical change level for farmland (sample known agricultural cells) and require scores well above it, or downweight cells whose "before" embedding clusters with cropland |
| Water level swings | Reservoirs, tidal flats, rivers move | Treat water gain and loss as its own category instead of dropping it, but require persistence |
| Local baseline | Some regions are naturally more variable | Normalize each cell's score against its neighborhood (z-score within a 20 to 50 km window) so a site must stand out from its surroundings |
| Tiny blobs | Single fields or noise speckle | Minimum site area and minimum compactness |

Build a small validation set early: 30 known big changes (a new airport, a large solar farm, a known wildfire, a new reservoir) and 30 known stable places. Track how many known changes land in the Top 100 and how many stable places sneak in. Every filter change should be judged against this set.

## Web app

A fully static site with four pages; every page is generated at build time from the pipeline's output files.

| Page | What it shows |
|---|---|
| Map (home) | World map with a change heat layer and site markers; click a marker to open its site page |
| Top 100 | Ranked leaderboard: rank, place name, change type, area, score, thumbnail; filter by type and country |
| Site page `/site/{id}` | Before/after slider of satellite imagery, a small map, stats, short auto-generated description, share button |
| About | How it works in plain language, data credits, license, limitations |

Suggested stack (Claude Code can propose alternatives):
- Astro for static pages, so every site page has its own URL and preview card for sharing.
- MapLibre GL JS for the map, with the PMTiles protocol so tiles are read straight from one file in object storage.
- A free open basemap (Protomaps or OpenFreeMap style).
- Before/after imagery: cloud-free Sentinel-2 true-color thumbnails for each site and year, generated offline from a public STAC catalog (Earth Search or Planetary Computer) and stored as WebP.
- Place names: reverse geocode each site centroid once during the build (Nominatim with polite rate limits, or an offline gazetteer) and store the name.
- Share cards: generate an Open Graph image per site (before/after side by side + rank) at build time. This is what makes links look good on X, Reddit, and LinkedIn.

## Cloudflare deployment

The site lives on Cloudflare Pages and the heavy files live in R2, so hosting stays close to free and needs no server.

- Cloudflare Pages: hosts the Astro build (HTML, JS, CSS, the leaderboard JSON). Deploy from GitHub on every push to main.
- R2 bucket: holds the large files (PMTiles for the heat layer and site polygons, before/after thumbnails, share images). Serve it on a subdomain like `data.yourdomain.com` with CORS allowing your site origin and HTTP range requests enabled, which PMTiles needs.
- Caching: long cache lifetimes on versioned file names (e.g. `change-2023-2025-v3.pmtiles`), so publishing a new run never breaks old links.
- Workers (later, optional): only if you add features that need a backend, such as search across sites or a "submit a correction" form.
- Upload step: a script in the repo that pushes pipeline outputs to R2 with wrangler or the S3-compatible API, then triggers a Pages rebuild.

Claude Code should check Cloudflare's current file size and file count limits for Pages before deciding what goes in the build versus R2; anything large belongs in R2.

## Milestones

Each milestone ends with something you can look at, so you can stop and judge before scaling up.

- [ ] **M0, data access**: script that reads one small tile for two years, decodes it, and saves a PNG of the per-pixel change score. Confirms decode, years available, and chunk layout.
- [ ] **M1, test box**: full pipeline on one 200 x 200 km area with a known big change (for example a fast-growing city edge or a large recent wildfire). Output: cell GeoParquet, site list, a quick local map in a notebook.
- [ ] **M2, filters and validation set**: add the noise filters and the 30 + 30 validation set; tune until most known changes rank high and stable places stay out.
- [ ] **M3, local web app**: Astro + MapLibre site running locally on the M1/M2 outputs, with map, leaderboard, and site pages including before/after imagery.
- [ ] **M4, deploy**: R2 bucket, Pages project, custom domain; publish the test box version publicly.
- [ ] **M5, scale out**: one country, then one continent, on a cloud VM near the data; publish the Top 100.
- [ ] **M6, polish for launch**: change type labels, share images, About page; write a short launch post with the five most surprising sites.
- [ ] **M7, follow-up**: a data center filter on top of the change sites (the next project).

## License and notes for Claude Code

The embeddings are under the OlmoEarth Artifact License, which allows using, displaying, and distributing derived data. It bans military, defense, and extractive-industry uses; a public change map is fine.

Obligations:
- Include the OlmoEarth Artifact License text with any derived data you publish (for example a `LICENSE-OLMOEARTH.txt` next to the PMTiles in R2) and link it from the About page.
- Credit on the About page and map footer: OlmoEarth v1.3 embeddings by Ai2, hosted on Source Cooperative.
- Credit satellite imagery: contains modified Copernicus Sentinel data (year).
- Credit the basemap and geocoder per their terms.

## Working rules for Claude Code

- Repo layout: `pipeline/` (Python, uv, config in YAML), `web/` (Astro), `scripts/` (upload and deploy), `data/` (gitignored cache and outputs).
- Always develop against a tiny bounding box first; never trigger reads of a whole UTM zone or year without asking.
- Ask before anything that costs money (cloud VMs, large downloads, paid APIs).
- Make every pipeline step resumable and idempotent: skip chunks already processed.
- Write a short RESULTS.md after each milestone with what changed, the validation numbers, and screenshots.
- Treat the decode formula, years available, and Cloudflare limits as facts to verify in code or docs, not assumptions from this plan.
