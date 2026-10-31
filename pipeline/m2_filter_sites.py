"""M2: noise filtering pass, re-running site extraction from the cached tiles
(no new network reads -- reuses data/tiles_cache/ written by m1_pipeline.py).

Filters applied, in order, each logged:
  1. Valid-data floor: a site's underlying cells must have enough valid pixels in
     both years (already enforced upstream by the AB/BC valid mask, logged here).
  2. Local-baseline normalization: each cell's AB score is z-scored against a
     20km-radius neighborhood, so a site must stand out from its surroundings,
     not just be in a generally high-change area.
  3. Persistence test: keep a site only if its B->C change (mean_change_bc) is
     well below its A->B change (mean_change_ab) -- i.e. the new state stuck,
     rather than reverting (drought year, a cloudy pass, crop rotation).
  4. Minimum area (already applied in M1, re-checked here).

Overwrites <region>/sites.geojson; rerun export_web_data.py afterwards to update the web app.

Run: uv run python m2_filter_sites.py regions/seattle.yaml
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import yaml
from scipy import ndimage
from scipy.ndimage import uniform_filter
from shapely.geometry import Polygon, box
from skimage import measure
from tqdm import tqdm

from geo import ZoneGeo, zone_name_for_lonlat
from store import open_root

DEFAULT_CONFIG = "regions/seattle.yaml"

PERSISTENCE_MAX_RATIO = 0.65  # keep site only if mean_change_bc <= ratio * mean_change_ab
ZSCORE_NEIGHBORHOOD_KM = 20.0
ZSCORE_MIN = 1.5  # a site's mean cell must be at least this many local std-devs above the local mean


def load_config() -> dict:
    if len(sys.argv) < 2:
        raise SystemExit("usage: uv run python " + sys.argv[0] + " regions/<name>.yaml")
    config_path = Path(__file__).parent / sys.argv[1]
    with open(config_path) as f:
        return yaml.safe_load(f)


def load_mosaics_from_cache(cfg, geo, row0, col0, H, W):
    tiles_cache_dir = Path(__file__).parent / cfg["paths"]["data_dir"] / "tiles_cache"
    tile_px = cfg["processing"]["tile_px"]
    n_tiles_y, n_tiles_x = H // tile_px, W // tile_px

    mosaic_ab = np.full((H, W), np.nan, dtype=np.float32)
    mosaic_bc = np.full((H, W), np.nan, dtype=np.float32)
    mosaic_valid = np.zeros((H, W), dtype=bool)

    missing = []
    for ty in range(n_tiles_y):
        for tx in range(n_tiles_x):
            cache_path = tiles_cache_dir / f"tile_r{ty:03d}_c{tx:03d}.npz"
            if not cache_path.exists():
                missing.append((ty, tx))
                continue
            d = np.load(cache_path)
            oy, ox = ty * tile_px, tx * tile_px
            mosaic_ab[oy : oy + tile_px, ox : ox + tile_px] = d["ab"]
            mosaic_bc[oy : oy + tile_px, ox : ox + tile_px] = d["bc"]
            mosaic_valid[oy : oy + tile_px, ox : ox + tile_px] = d["valid"]

    if missing:
        raise RuntimeError(f"{len(missing)} tiles not cached -- run m1_pipeline.py first")
    return mosaic_ab, mosaic_bc, mosaic_valid


def local_zscore(mosaic_ab, mosaic_valid, px_size_m, neighborhood_km):
    """Z-score each pixel's AB score against a local neighborhood mean/std."""
    radius_px = max(1, int((neighborhood_km * 1000) / px_size_m))
    size = 2 * radius_px + 1

    filled = np.where(mosaic_valid, mosaic_ab, 0.0)
    valid_f = mosaic_valid.astype(np.float32)

    local_sum = uniform_filter(filled, size=size, mode="nearest")
    local_count = uniform_filter(valid_f, size=size, mode="nearest")
    local_count = np.maximum(local_count, 1e-6)
    local_mean = local_sum / local_count

    sq_sum = uniform_filter(filled**2, size=size, mode="nearest")
    local_var = np.maximum(sq_sum / local_count - local_mean**2, 1e-8)
    local_std = np.sqrt(local_var)

    z = (mosaic_ab - local_mean) / np.maximum(local_std, 1e-3)
    z[~mosaic_valid] = np.nan
    return z


def main():
    cfg = load_config()
    data_dir = Path(__file__).parent / cfg["paths"]["data_dir"]

    with open(data_dir / "m1_summary.json") as f:
        m1_summary = json.load(f)

    print("Opening store (metadata only)...")
    root = open_root(cfg["store"]["url"], cfg["store"]["endpoint"])
    region = cfg["region"]
    zname = zone_name_for_lonlat(region["center_lon"], region["center_lat"])
    zone = root[zname]
    geo = ZoneGeo.from_zarr_attrs(dict(zone.attrs))

    box_ = m1_summary["pixel_box"]
    row0, col0, H, W = box_["row0"], box_["col0"], box_["H"], box_["W"]

    print("Reconstructing mosaics from cached tiles...")
    mosaic_ab, mosaic_bc, mosaic_valid = load_mosaics_from_cache(cfg, geo, row0, col0, H, W)
    print(f"valid fraction: {mosaic_valid.mean():.4f}")

    print(f"Computing local z-score (neighborhood {ZSCORE_NEIGHBORHOOD_KM}km)...")
    px_size_m = abs(geo.a)
    zscore = local_zscore(mosaic_ab, mosaic_valid, px_size_m, ZSCORE_NEIGHBORHOOD_KM)

    threshold = np.nanpercentile(mosaic_ab, cfg["sites"]["change_percentile_threshold"])
    print(f"change threshold (p{cfg['sites']['change_percentile_threshold']}): {threshold:.4f}")

    # Candidate pixels: high absolute change AND stands out locally.
    high_change = (mosaic_ab > threshold) & (zscore > ZSCORE_MIN) & mosaic_valid
    labeled, n_blobs = ndimage.label(high_change, structure=np.ones((3, 3)))
    print(f"{n_blobs} raw connected components after z-score filter (was candidate pool before M1's simple threshold)")

    px_area_km2 = (geo.a / 1000.0) * (abs(geo.e) / 1000.0)
    min_pixels = max(cfg["sites"]["min_pixels"], int(cfg["sites"]["min_area_km2"] / px_area_km2))

    sites = []
    n_dropped_area = 0
    n_dropped_persistence = 0
    objs = ndimage.find_objects(labeled)
    for blob_id, slc in enumerate(tqdm(objs, desc="sites"), start=1):
        if slc is None:
            continue
        mask = labeled[slc] == blob_id
        n_px = int(mask.sum())
        if n_px < min_pixels:
            n_dropped_area += 1
            continue

        sub_ab = mosaic_ab[slc]
        sub_bc = mosaic_bc[slc]
        mean_score = float(np.nanmean(sub_ab[mask]))
        mean_bc_score = float(np.nanmean(sub_bc[mask]))
        area_km2 = n_px * px_area_km2

        persistence_ratio = mean_bc_score / mean_score if mean_score > 1e-6 else 1.0
        if persistence_ratio > PERSISTENCE_MAX_RATIO:
            n_dropped_persistence += 1
            continue

        y_off, x_off = slc[0].start, slc[1].start
        ys, xs = np.nonzero(mask)
        centroid_row = row0 + y_off + ys.mean()
        centroid_col = col0 + x_off + xs.mean()
        centroid_lon, centroid_lat = geo.colrow_to_lonlat(centroid_col, centroid_row)

        padded = np.pad(mask, 1, mode="constant", constant_values=False)
        contours = measure.find_contours(padded.astype(float), level=0.5)
        poly = None
        if contours:
            longest = max(contours, key=len)
            pts = []
            for ry, rx in longest:
                global_row = row0 + y_off + (ry - 1)
                global_col = col0 + x_off + (rx - 1)
                lon, lat = geo.colrow_to_lonlat(global_col, global_row)
                pts.append((lon, lat))
            if len(pts) >= 4:
                poly = Polygon(pts).simplify(0.00005, preserve_topology=True)
        if poly is None or not poly.is_valid or poly.is_empty:
            lon0, lat0 = geo.colrow_to_lonlat(col0 + x_off, row0 + y_off + mask.shape[0])
            lon1, lat1 = geo.colrow_to_lonlat(col0 + x_off + mask.shape[1], row0 + y_off)
            poly = box(lon0, lat0, lon1, lat1)

        rank_score = area_km2 * mean_score
        sites.append(
            {
                "blob_id": blob_id,
                "area_km2": round(area_km2, 4),
                "mean_change_ab": round(mean_score, 5),
                "mean_change_bc": round(mean_bc_score, 5),
                "persistence_ratio": round(persistence_ratio, 4),
                "rank_score": rank_score,
                "centroid_lon": centroid_lon,
                "centroid_lat": centroid_lat,
                "n_pixels": n_px,
                "geometry": poly,
            }
        )

    sites.sort(key=lambda s: s["rank_score"], reverse=True)
    for i, s in enumerate(sites, start=1):
        s["rank"] = i
        s["id"] = f"{region['name']}-{i:04d}"

    sites_gdf = gpd.GeoDataFrame(sites, geometry="geometry", crs="EPSG:4326")
    sites_path = data_dir / "sites.geojson"
    sites_gdf.to_file(sites_path, driver="GeoJSON")

    print(f"\ndropped {n_dropped_area} blobs below min area ({cfg['sites']['min_area_km2']} km2)")
    print(f"dropped {n_dropped_persistence} blobs failing persistence test (bc/ab ratio > {PERSISTENCE_MAX_RATIO})")
    print(f"kept {len(sites_gdf)} sites -> {sites_path}")

    m1_summary["n_sites"] = len(sites_gdf)
    m1_summary["m2_filters"] = {
        "zscore_neighborhood_km": ZSCORE_NEIGHBORHOOD_KM,
        "zscore_min": ZSCORE_MIN,
        "persistence_max_ratio": PERSISTENCE_MAX_RATIO,
        "n_raw_blobs": int(n_blobs),
        "n_dropped_area": n_dropped_area,
        "n_dropped_persistence": n_dropped_persistence,
    }
    with open(data_dir / "m1_summary.json", "w") as f:
        json.dump(m1_summary, f, indent=2)


if __name__ == "__main__":
    main()
