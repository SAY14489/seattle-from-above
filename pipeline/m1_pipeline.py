"""M1: full pipeline (read, decode, per-pixel change, cell aggregation, site extraction)
on one test box, driven by a region config file (see pipeline/regions/).

Streams the region tile-by-tile (tile_px x tile_px, all bands, 3 years) so memory stays
bounded regardless of region size. Each tile's computed change scores are cached to disk
as a small .npz so a rerun skips tiles already done (resumable/idempotent).

Run: uv run python m1_pipeline.py regions/seattle.yaml
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import yaml
from PIL import Image
from scipy import ndimage
from shapely.geometry import Polygon, box
from skimage import measure
from tqdm import tqdm

from decode import cosine_distance, decode, l2_normalize
from geo import ZoneGeo, zone_name_for_lonlat
from store import open_root

def load_config() -> dict:
    if len(sys.argv) < 2:
        raise SystemExit("usage: uv run python " + sys.argv[0] + " regions/<name>.yaml")
    config_path = Path(__file__).parent / sys.argv[1]
    with open(config_path) as f:
        return yaml.safe_load(f)


def compute_region_pixel_box(geo: ZoneGeo, center_lon: float, center_lat: float, width_km: float, height_km: float, tile_px: int):
    col_c, row_c = geo.lonlat_to_colrow(center_lon, center_lat)

    def snap(km):
        px = geo.meters_to_pixels(km * 1000.0)
        return (px // tile_px) * tile_px  # round down to a multiple of tile_px

    W, H = snap(width_km), snap(height_km)
    row0 = row_c - H // 2
    col0 = col_c - W // 2
    return row0, col0, H, W


def read_tile(emb_arr, t_idx: int, y0: int, y1: int, x0: int, x1: int, bands: int, retries: int = 4) -> np.ndarray:
    last_err = None
    for attempt in range(retries):
        try:
            return emb_arr[t_idx, :bands, y0:y1, x0:x1]
        except Exception as e:  # noqa: BLE001 - transient network errors from obstore
            last_err = e
            time.sleep(1.5 * (attempt + 1))
    raise last_err


def process_tile(emb_arr, ta, tb, tc, y0, y1, x0, x1, bands):
    qa = read_tile(emb_arr, ta, y0, y1, x0, x1, bands)
    qb = read_tile(emb_arr, tb, y0, y1, x0, x1, bands)
    qc = read_tile(emb_arr, tc, y0, y1, x0, x1, bands)

    fa, va = decode(qa)
    fb, vb = decode(qb)
    fc, vc = decode(qc)

    na = l2_normalize(fa, axis=0)
    nb = l2_normalize(fb, axis=0)
    nc = l2_normalize(fc, axis=0)

    change_ab = cosine_distance(na, nb, axis=0).astype(np.float32)
    change_bc = cosine_distance(nb, nc, axis=0).astype(np.float32)

    valid = va.all(axis=0) & vb.all(axis=0) & vc.all(axis=0)
    change_ab[~valid] = np.nan
    change_bc[~valid] = np.nan

    return change_ab, change_bc, valid


def main():
    cfg = load_config()
    data_dir = Path(__file__).parent / cfg["paths"]["data_dir"]
    data_dir.mkdir(parents=True, exist_ok=True)
    tiles_cache_dir = data_dir / "tiles_cache"
    tiles_cache_dir.mkdir(parents=True, exist_ok=True)

    print("Opening store...")
    root = open_root(cfg["store"]["url"], cfg["store"]["endpoint"])

    region = cfg["region"]
    zname = zone_name_for_lonlat(region["center_lon"], region["center_lat"])
    print("zone:", zname)
    zone = root[zname]
    geo = ZoneGeo.from_zarr_attrs(dict(zone.attrs))
    emb_arr = zone["embeddings"]
    time_vals = list(zone["time"][:])

    ta = time_vals.index(cfg["years"]["a"])
    tb = time_vals.index(cfg["years"]["b"])
    tc = time_vals.index(cfg["years"]["c"])

    tile_px = cfg["processing"]["tile_px"]
    cell_px = cfg["processing"]["cell_px"]
    bands = cfg["processing"]["bands"]
    assert tile_px % cell_px == 0, "tile_px must be a multiple of cell_px"

    width_km = region.get("width_km", region.get("size_km"))
    height_km = region.get("height_km", region.get("size_km"))
    row0, col0, H, W = compute_region_pixel_box(geo, region["center_lon"], region["center_lat"], width_km, height_km, tile_px)
    print(f"region pixel box: row0={row0} col0={col0} H={H} W={W} ({H/100:.1f}km x {W/100:.1f}km)")

    n_tiles_y = H // tile_px
    n_tiles_x = W // tile_px
    print(f"tiles: {n_tiles_y} x {n_tiles_x} = {n_tiles_y * n_tiles_x}, tile size {tile_px}px ({tile_px/100:.1f}km)")

    mosaic_ab = np.full((H, W), np.nan, dtype=np.float32)
    mosaic_bc = np.full((H, W), np.nan, dtype=np.float32)
    mosaic_valid = np.zeros((H, W), dtype=bool)

    skipped, fetched = 0, 0
    for ty in tqdm(range(n_tiles_y), desc="tile rows"):
        for tx in range(n_tiles_x):
            y0 = row0 + ty * tile_px
            x0 = col0 + tx * tile_px
            y1, x1 = y0 + tile_px, x0 + tile_px
            cache_path = tiles_cache_dir / f"tile_r{ty:03d}_c{tx:03d}.npz"

            if cache_path.exists():
                d = np.load(cache_path)
                ab, bc, valid = d["ab"], d["bc"], d["valid"]
                skipped += 1
            else:
                ab, bc, valid = process_tile(emb_arr, ta, tb, tc, y0, y1, x0, x1, bands)
                np.savez_compressed(cache_path, ab=ab, bc=bc, valid=valid)
                fetched += 1

            oy, ox = ty * tile_px, tx * tile_px
            mosaic_ab[oy : oy + tile_px, ox : ox + tile_px] = ab
            mosaic_bc[oy : oy + tile_px, ox : ox + tile_px] = bc
            mosaic_valid[oy : oy + tile_px, ox : ox + tile_px] = valid

    print(f"tiles: {fetched} fetched, {skipped} loaded from cache")
    valid_frac = mosaic_valid.mean()
    print(f"overall valid pixel fraction: {valid_frac:.4f}")

    # ---- Save a quick overview PNG (phase-2/3 sanity check) ----
    vmin, vmax = np.nanpercentile(mosaic_ab, 1), np.nanpercentile(mosaic_ab, 99)
    norm = np.clip((mosaic_ab - vmin) / max(vmax - vmin, 1e-6), 0, 1)
    img = np.nan_to_num(norm * 255, nan=0).astype(np.uint8)
    Image.fromarray(img, mode="L").save(data_dir / "m1_change_ab_overview.png")
    print("saved", data_dir / "m1_change_ab_overview.png")

    # ---- Phase 3: aggregate to cells, write GeoParquet ----
    n_cells_y, n_cells_x = H // cell_px, W // cell_px
    ab_blocks = mosaic_ab.reshape(n_cells_y, cell_px, n_cells_x, cell_px)
    bc_blocks = mosaic_bc.reshape(n_cells_y, cell_px, n_cells_x, cell_px)
    valid_blocks = mosaic_valid.reshape(n_cells_y, cell_px, n_cells_x, cell_px)

    mean_ab = np.nanmean(ab_blocks, axis=(1, 3))
    p95_ab = np.nanpercentile(ab_blocks, 95, axis=(1, 3))
    mean_bc = np.nanmean(bc_blocks, axis=(1, 3))
    valid_count = valid_blocks.sum(axis=(1, 3))
    frac_valid = valid_count / (cell_px * cell_px)
    threshold = np.nanpercentile(mosaic_ab, cfg["sites"]["change_percentile_threshold"])
    frac_above = (np.where(valid_blocks, ab_blocks, 0) > threshold).sum(axis=(1, 3)) / (cell_px * cell_px)

    records = []
    geoms = []
    for cy in range(n_cells_y):
        for cx in range(n_cells_x):
            py0, px0 = cy * cell_px, cx * cell_px
            py1, px1 = py0 + cell_px, px0 + cell_px
            lon0, lat0 = geo.colrow_to_lonlat(col0 + px0, row0 + py1)  # bottom-left
            lon1, lat1 = geo.colrow_to_lonlat(col0 + px1, row0 + py0)  # top-right
            geoms.append(box(lon0, lat0, lon1, lat1))
            records.append(
                {
                    "cell_y": cy,
                    "cell_x": cx,
                    "mean_change_ab": float(mean_ab[cy, cx]),
                    "p95_change_ab": float(p95_ab[cy, cx]),
                    "mean_change_bc": float(mean_bc[cy, cx]),
                    "valid_frac": float(frac_valid[cy, cx]),
                    "frac_above_threshold": float(frac_above[cy, cx]),
                }
            )

    cells_gdf = gpd.GeoDataFrame(records, geometry=geoms, crs="EPSG:4326")
    cells_path = data_dir / "cells.parquet"
    cells_gdf.to_parquet(cells_path)
    print(f"saved {len(cells_gdf)} cells to {cells_path}")

    # ---- Phase 4: extract sites ----
    print(f"change threshold (p{cfg['sites']['change_percentile_threshold']}): {threshold:.4f}")
    high_change = (mosaic_ab > threshold) & mosaic_valid
    labeled, n_blobs = ndimage.label(high_change, structure=np.ones((3, 3)))
    print(f"{n_blobs} raw connected components")

    px_area_km2 = (geo.a / 1000.0) * (abs(geo.e) / 1000.0)
    min_pixels = max(cfg["sites"]["min_pixels"], int(cfg["sites"]["min_area_km2"] / px_area_km2))

    sites = []
    objs = ndimage.find_objects(labeled)
    for blob_id, slc in enumerate(tqdm(objs, desc="sites"), start=1):
        if slc is None:
            continue
        mask = labeled[slc] == blob_id
        n_px = int(mask.sum())
        if n_px < min_pixels:
            continue

        sub_ab = mosaic_ab[slc]
        sub_bc = mosaic_bc[slc]
        mean_score = float(np.nanmean(sub_ab[mask]))
        mean_bc_score = float(np.nanmean(sub_bc[mask]))
        area_km2 = n_px * px_area_km2

        y_off, x_off = slc[0].start, slc[1].start
        ys, xs = np.nonzero(mask)
        centroid_row = row0 + y_off + ys.mean()
        centroid_col = col0 + x_off + xs.mean()
        centroid_lon, centroid_lat = geo.colrow_to_lonlat(centroid_col, centroid_row)

        # polygon via marching squares on the padded local mask
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
    print(f"saved {len(sites_gdf)} sites to {sites_path}")

    summary = {
        "region": region["name"],
        "years": cfg["years"],
        "pixel_box": {"row0": row0, "col0": col0, "H": H, "W": W},
        "valid_fraction": float(valid_frac),
        "n_cells": len(cells_gdf),
        "n_sites": len(sites_gdf),
        "change_threshold": float(threshold),
        "min_site_area_km2": cfg["sites"]["min_area_km2"],
    }
    with open(data_dir / "m1_summary.json", "w") as f:
        json.dump(summary, f, indent=2)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
