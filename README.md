# Earth's Biggest Changes

Ranks the most transformed places on Earth between two years, using Ai2's free OlmoEarth v1.3
satellite embeddings. See [PLAN.md](./PLAN.md) for the full build plan and [RESULTS.md](./RESULTS.md)
for milestone notes.

This build is local-only: a handful of test regions, running as a local Astro + MapLibre web app
with one combined, globally-ranked leaderboard across all of them. No Cloudflare deployment yet
(that's M4/M5 in the plan).

## Layout

- `pipeline/` -- Python (uv), reads OlmoEarth embeddings, computes change scores, extracts sites.
  - `pipeline/regions/*.yaml` -- one config per test region (bbox, years, thresholds).
- `web/` -- Astro + MapLibre static site, reads pipeline outputs from `web/public/data/`.
- `scripts/` -- upload/deploy scripts (unused until M4).
- `data/<region>/` -- gitignored per-region cache and pipeline outputs.

## Run the pipeline

Per region (repeat for each `pipeline/regions/*.yaml`):

```bash
cd pipeline
uv run python m1_pipeline.py regions/phoenix.yaml        # read + decode + change score + cells + sites
uv run python m2_filter_sites.py regions/phoenix.yaml    # noise filtering (z-score + persistence)
```

Then export and enrich -- these run once and cover every region in `regions/`:

```bash
uv run python export_web_data.py    # merge all regions into web/public/data/ (one global ranking)
uv run python enrich_names.py       # reverse-geocode new site names (Nominatim, rate-limited)
uv run python fetch_thumbnails.py   # before/after Sentinel-2 thumbnails (Planetary Computer)
```

All steps are resumable: rerunning skips tiles/images already on disk, and `enrich_names.py` /
`fetch_thumbnails.py` only fill in sites that don't have a name/image yet. To add a new region,
drop a new `pipeline/regions/<name>.yaml` (copy an existing one, change `region:` and `paths.data_dir`),
run `m1_pipeline.py` + `m2_filter_sites.py` for it, then rerun the three scripts above.

## Run the web app

```bash
cd web
npm install
npm run dev
```

Open http://localhost:4321
