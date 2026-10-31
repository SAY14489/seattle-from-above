"""Export pipeline outputs for every region in pipeline/regions/ into a single
combined web/public/data/ dataset for the Astro + MapLibre site.

Each region's sites.geojson already carries a globally-unique id (region-name
prefixed), so sites from multiple regions merge without collisions. Sites are
re-ranked globally by rank_score across all regions, turning this into a real
multi-region "Top N changed places" leaderboard rather than one per region.

Run after m1_pipeline.py (+ optionally m2_filter_sites.py) for each region.
Safe to rerun -- regenerates files from the current pipeline outputs without
touching anything enrich scripts have added (those patch the exported JSON in
place afterwards, and are themselves idempotent / safe to rerun too).

Run: uv run python export_web_data.py
"""

import json
from pathlib import Path

import geopandas as gpd
import pandas as pd
import yaml

REGIONS_DIR = Path(__file__).parent / "regions"
WEB_DATA_DIR = Path(__file__).parent.parent / "web" / "public" / "data"
MAX_SITES = 100  # cap the leaderboard/map/site-pages at a real "Top 100"


def load_region(config_path: Path) -> dict:
    with open(config_path) as f:
        cfg = yaml.safe_load(f)
    data_dir = Path(__file__).parent / cfg["paths"]["data_dir"]

    with open(data_dir / "m1_summary.json") as f:
        m1_summary = json.load(f)

    cells_gdf = gpd.read_parquet(data_dir / "cells.parquet")
    sites_gdf = gpd.read_file(data_dir / "sites.geojson")

    cells_gdf["region"] = cfg["region"]["name"]
    if len(sites_gdf):
        sites_gdf["region"] = cfg["region"]["name"]

    return {"cfg": cfg, "m1_summary": m1_summary, "cells_gdf": cells_gdf, "sites_gdf": sites_gdf}


def main():
    WEB_DATA_DIR.mkdir(parents=True, exist_ok=True)
    region_configs = sorted(REGIONS_DIR.glob("*.yaml"))
    if not region_configs:
        raise SystemExit(f"no region configs found in {REGIONS_DIR}")

    regions = [load_region(p) for p in region_configs]

    # ---- merge cells ----
    all_cells = pd.concat([r["cells_gdf"] for r in regions], ignore_index=True)
    cells_out = gpd.GeoDataFrame(
        all_cells[["region", "mean_change_ab", "p95_change_ab", "mean_change_bc", "valid_frac", "frac_above_threshold", "geometry"]],
        crs="EPSG:4326",
    )
    cells_out.to_file(WEB_DATA_DIR / "cells.geojson", driver="GeoJSON")
    print(f"wrote {len(cells_out)} cells across {len(regions)} region(s) -> {WEB_DATA_DIR / 'cells.geojson'}")

    # ---- merge + globally re-rank sites, cap at MAX_SITES ----
    all_sites = pd.concat([r["sites_gdf"] for r in regions if len(r["sites_gdf"])], ignore_index=True)
    all_sites = gpd.GeoDataFrame(all_sites, crs="EPSG:4326")
    all_sites = all_sites.sort_values("rank_score", ascending=False).reset_index(drop=True)
    all_sites = all_sites.iloc[:MAX_SITES].reset_index(drop=True)

    cfg_by_region = {r["cfg"]["region"]["name"]: r["cfg"] for r in regions}

    sites_records = []
    for i, row in all_sites.iterrows():
        bounds = row.geometry.bounds
        region_cfg = cfg_by_region[row["region"]]
        sites_records.append(
            {
                "id": row["id"],
                "rank": i + 1,
                "name": row.get("name") or f"Site #{i + 1}",
                "locality": row.get("locality"),
                "country": row.get("country"),
                "type": row.get("change_type") or "Unclassified change",
                "region": row["region"],
                "region_label": region_cfg["region"]["name"].replace("_", " ").title(),
                "years": region_cfg["years"],
                "area_km2": float(row["area_km2"]),
                "mean_change_ab": float(row["mean_change_ab"]),
                "mean_change_bc": float(row["mean_change_bc"]),
                "centroid_lon": float(row["centroid_lon"]),
                "centroid_lat": float(row["centroid_lat"]),
                "bbox": [bounds[0], bounds[1], bounds[2], bounds[3]],
                "before_image": row.get("before_image"),
                "after_image": row.get("after_image"),
            }
        )

    with open(WEB_DATA_DIR / "sites.json", "w") as f:
        json.dump(sites_records, f, indent=2)
    print(f"wrote {len(sites_records)} sites (globally ranked) -> {WEB_DATA_DIR / 'sites.json'}")

    sites_gdf_out = all_sites.copy()
    sites_gdf_out["rank"] = [r["rank"] for r in sites_records]
    sites_gdf_out["name"] = [r["name"] for r in sites_records]
    sites_gdf_out["type"] = [r["type"] for r in sites_records]
    sites_gdf_out = sites_gdf_out[["id", "rank", "name", "type", "region", "area_km2", "mean_change_ab", "geometry"]]
    sites_gdf_out.to_file(WEB_DATA_DIR / "sites.geojson", driver="GeoJSON")
    print(f"wrote sites map layer -> {WEB_DATA_DIR / 'sites.geojson'}")

    # ---- summary: overall totals + a per-region breakdown ----
    region_summaries = []
    for r in regions:
        cfg, m1_summary = r["cfg"], r["m1_summary"]
        region_name = cfg["region"]["name"]
        n_sites_here = sum(1 for s in sites_records if s["region"] == region_name)
        region_bounds = r["sites_gdf"].total_bounds if len(r["sites_gdf"]) else r["cells_gdf"].total_bounds
        region_summaries.append(
            {
                "region": region_name,
                "region_label": region_name.replace("_", " ").title(),
                "years": cfg["years"],
                "center_lon": cfg["region"]["center_lon"],
                "center_lat": cfg["region"]["center_lat"],
                "width_km": cfg["region"].get("width_km", cfg["region"].get("size_km")),
                "height_km": cfg["region"].get("height_km", cfg["region"].get("size_km")),
                "n_cells": m1_summary["n_cells"],
                "n_sites": n_sites_here,
                "change_threshold": m1_summary["change_threshold"],
                "min_site_area_km2": m1_summary["min_site_area_km2"],
                "bbox": [float(b) for b in region_bounds],
            }
        )

    combined_bounds = all_sites.total_bounds if len(all_sites) else cells_out.total_bounds
    summary = {
        "n_regions": len(regions),
        "n_cells": sum(r["m1_summary"]["n_cells"] for r in regions),
        "n_sites": len(sites_records),
        "bbox": [float(b) for b in combined_bounds],
        "regions": region_summaries,
    }
    with open(WEB_DATA_DIR / "summary.json", "w") as f:
        json.dump(summary, f, indent=2)
    print(f"wrote summary ({len(regions)} region(s)) -> {WEB_DATA_DIR / 'summary.json'}")


if __name__ == "__main__":
    main()
