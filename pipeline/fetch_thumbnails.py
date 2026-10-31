"""Fetch cloud-free true-color thumbnails for each site, one per year across the
dataset's full 2017 to 2025 range, via the Microsoft Planetary Computer STAC catalog
(public, no API key). Powers the year-by-year timelapse scrubber on each site page,
not just a single before/after pair.

For each site and year: search Sentinel-2 L2A scenes covering the site's bbox
(expanded to a minimum viewing window) within that year, then -- since a scene's
reported whole-tile cloud cover says nothing about this one small crop -- read the
Scene Classification (SCL) band for the crop of each of the least-cloudy candidates
and pick whichever crop actually has the fewest cloud/shadow/nodata pixels. Then
windowed-read B04/B03/B02 for that scene, percentile-stretch to 8-bit, save as WebP.

Run after export_web_data.py. Idempotent: skips images that already exist on disk,
and reuses the already-fetched _before/_after images for their matching years
instead of re-fetching them.

Run: uv run python fetch_thumbnails.py
"""

import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# Must be set before rasterio/GDAL opens any dataset -- these bound every
# network read so a slow/unresponsive host can't hang the whole run.
os.environ["GDAL_HTTP_TIMEOUT"] = "15"
os.environ["GDAL_HTTP_CONNECTTIMEOUT"] = "10"
os.environ["GDAL_HTTP_MAX_RETRY"] = "1"
os.environ["GDAL_HTTP_RETRY_DELAY"] = "1"
os.environ["CPL_VSIL_CURL_USE_HEAD"] = "NO"

import numpy as np
import planetary_computer
import pystac_client
import rasterio
from PIL import Image
from rasterio import warp
from rasterio.windows import from_bounds
from tqdm import tqdm

REQUEST_TIMEOUT_S = 20
MAX_WORKERS = 3  # purely I/O-bound (network); kept modest since Planetary Computer
# drops connections under higher concurrency (observed with 8 workers)
TIMELINE_YEARS = list(range(2017, 2026))  # the dataset's full year range

WEB_DATA_DIR = Path(__file__).parent.parent / "web" / "public" / "data"
THUMB_DIR = Path(__file__).parent.parent / "web" / "public" / "thumbs"
STAC_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"

MIN_WINDOW_DEG = 0.012  # ~1.3km at mid-latitudes, minimum crop size around a site
CANDIDATE_SCENES = 6  # how many least-cloudy whole-scene candidates to crop-check
CLOUD_CHECK_SIZE = 64  # small/cheap read just to score cloud fraction in the crop

# Sentinel-2 L2A Scene Classification (SCL) codes we treat as "bad" for a thumbnail:
# 0 no data, 1 saturated/defective, 3 cloud shadow, 8/9 cloud med/high prob, 10 thin cirrus.
SCL_BAD_CLASSES = {0, 1, 3, 8, 9, 10}

_thread_local = threading.local()


def get_catalog():
    # pystac_client.Client wraps a requests.Session; give each worker thread its own
    # to avoid sharing connection-pool state across threads.
    if not hasattr(_thread_local, "catalog"):
        _thread_local.catalog = pystac_client.Client.open(
            STAC_URL, modifier=planetary_computer.sign_inplace, timeout=REQUEST_TIMEOUT_S
        )
    return _thread_local.catalog


def expand_bbox(bbox, min_size=MIN_WINDOW_DEG):
    minx, miny, maxx, maxy = bbox
    cx, cy = (minx + maxx) / 2, (miny + maxy) / 2
    w, h = max(maxx - minx, min_size), max(maxy - miny, min_size)
    pad = 0.15  # extra context around the site
    w, h = w * (1 + pad), h * (1 + pad)
    return [cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2]


def candidate_scenes(catalog, bbox, year, limit=CANDIDATE_SCENES):
    search = catalog.search(collections=["sentinel-2-l2a"], bbox=bbox, datetime=f"{year}-01-01/{year}-12-31", limit=50)
    items = list(search.items())
    if not items:
        return []
    items.sort(key=lambda it: it.properties.get("eo:cloud_cover", 100))
    return items[:limit]


def read_band_window(href: str, bbox, out_shape, resampling):
    with rasterio.open(href) as src:
        # bbox is lon/lat (EPSG:4326); the raster is in its own UTM CRS, so
        # reproject the bounds before computing a pixel window -- from_bounds
        # assumes its inputs are already in the dataset's CRS.
        proj_bounds = warp.transform_bounds("EPSG:4326", src.crs, *bbox)
        window = from_bounds(*proj_bounds, transform=src.transform)
        window = window.round_offsets().round_lengths()
        if window.width <= 0 or window.height <= 0:
            return None
        return src.read(1, window=window, out_shape=out_shape, resampling=resampling)


def crop_cloud_fraction(item, bbox) -> float:
    """Fraction of SCL-flagged bad (cloud/shadow/nodata) pixels in this item's crop."""
    signed = planetary_computer.sign(item)
    scl_href = signed.assets["SCL"].href
    scl = read_band_window(scl_href, bbox, (CLOUD_CHECK_SIZE, CLOUD_CHECK_SIZE), rasterio.enums.Resampling.nearest)
    if scl is None or scl.size == 0:
        return 1.0
    bad = np.isin(scl, list(SCL_BAD_CLASSES))
    return float(bad.mean())


def render_thumbnail(item, bbox, out_path: Path) -> bool:
    signed = planetary_computer.sign(item)
    bands = [signed.assets["B04"].href, signed.assets["B03"].href, signed.assets["B02"].href]

    channels = []
    for href in bands:
        arr = read_band_window(href, bbox, (256, 256), rasterio.enums.Resampling.bilinear)
        if arr is None:
            return False
        channels.append(arr.astype(np.float32))

    rgb = np.stack(channels, axis=-1)
    valid = rgb.sum(axis=-1) > 0
    if valid.mean() < 0.5:
        return False

    vmin, vmax = np.percentile(rgb[valid], 2), np.percentile(rgb[valid], 98)
    norm = np.clip((rgb - vmin) / max(vmax - vmin, 1), 0, 1)
    img = (norm * 255).astype(np.uint8)
    Image.fromarray(img, mode="RGB").save(out_path, "WEBP", quality=85)
    return True


def pick_clearest_scene(catalog, bbox, year):
    """Among the least-cloudy whole-scene candidates, pick whichever this crop is
    actually clearest in (checked via its SCL band), not just the lowest scene-level
    cloud %, which says nothing about one small crop inside a ~100x100km tile."""
    candidates = candidate_scenes(catalog, bbox, year)
    if not candidates:
        return None, None
    best_item, best_frac = None, 2.0
    for item in candidates:
        try:
            frac = crop_cloud_fraction(item, bbox)
        except Exception:  # noqa: BLE001 - a bad candidate just loses, try the next
            continue
        if frac < best_frac:
            best_item, best_frac = item, frac
        if best_frac < 0.05:  # good enough, stop early
            break
    return best_item, best_frac


def fetch_one(site_id: str, bbox, year: int, path: Path):
    """Runs in a worker thread. Returns (site_id, year, image_url_or_None)."""
    if path.exists():
        return site_id, year, f"/thumbs/{path.name}"
    try:
        item, cloud_frac = pick_clearest_scene(get_catalog(), bbox, year)
        if item is None:
            print(f"no scene for {site_id} {year}")
            return site_id, year, None
        if cloud_frac is not None and cloud_frac > 0.3:
            print(f"warning: {site_id} {year} best candidate still {cloud_frac:.0%} cloud/nodata in crop")
        ok = render_thumbnail(item, bbox, path)
        return site_id, year, (f"/thumbs/{path.name}" if ok else None)
    except Exception as e:  # noqa: BLE001 - network/STAC/GDAL errors, skip and continue
        print(f"failed {site_id} {year}: {e}")
        return site_id, year, None


def reuse_existing_before_after(site: dict):
    """The old single-pair fetch wrote _before.webp/_after.webp for each site's
    own (a, b) years. Copy those into the per-year filename scheme so the
    timeline build doesn't re-fetch years we already have on disk."""
    year_a, year_b = site["years"]["a"], site["years"]["b"]
    for year, old_name in [(year_a, "before"), (year_b, "after")]:
        old_path = THUMB_DIR / f"{site['id']}_{old_name}.webp"
        new_path = THUMB_DIR / f"{site['id']}_{year}.webp"
        if old_path.exists() and not new_path.exists():
            new_path.write_bytes(old_path.read_bytes())


def main():
    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    with open(WEB_DATA_DIR / "sites.json") as f:
        sites = json.load(f)

    sites_by_id = {s["id"]: s for s in sites}
    for s in sites:
        reuse_existing_before_after(s)

    tasks = []
    for s in sites:
        bbox = expand_bbox(s["bbox"])
        for year in TIMELINE_YEARS:
            path = THUMB_DIR / f"{s['id']}_{year}.webp"
            tasks.append((s["id"], bbox, year, path))

    write_lock = threading.Lock()
    done = 0
    results_by_site: dict[str, dict[int, str]] = {s["id"]: {} for s in sites}

    def checkpoint():
        for s in sites:
            timeline = [
                {"year": y, "image": results_by_site[s["id"]][y]}
                for y in TIMELINE_YEARS
                if y in results_by_site[s["id"]]
            ]
            if timeline:
                s["timeline"] = timeline
                s["before_image"] = timeline[0]["image"]
                s["after_image"] = timeline[-1]["image"]
        with open(WEB_DATA_DIR / "sites.json", "w") as f:
            json.dump(sites, f, indent=2)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = [pool.submit(fetch_one, *t) for t in tasks]
        with tqdm(total=len(futures), desc="timeline thumbnails") as pbar:
            for fut in as_completed(futures):
                site_id, year, url = fut.result()
                if url:
                    with write_lock:
                        results_by_site[site_id][year] = url
                done += 1
                pbar.update(1)
                if done % 20 == 0:
                    with write_lock:
                        checkpoint()

    checkpoint()
    n_complete = sum(1 for s in sites if len(s.get("timeline", [])) == len(TIMELINE_YEARS))
    print(f"done. {n_complete}/{len(sites)} sites have a full {len(TIMELINE_YEARS)}-year timeline")


if __name__ == "__main__":
    main()
