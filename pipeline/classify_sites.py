"""Patch hand-reviewed change-type classifications into the exported web data.

classifications.json was built by visually reviewing before/after contact sheets
of every site's imagery (see make_contact_sheets.py) and assigning each one a
change type -- this is a manual v1 pass, not an automated classifier (the plan's
M1.5 kNN classifier is still future work). Idempotent: just overwrites "type".

Run after fetch_thumbnails.py. Run: uv run python classify_sites.py
"""

import json
from pathlib import Path

WEB_DATA_DIR = Path(__file__).parent.parent / "web" / "public" / "data"
CLASSIFICATIONS_PATH = Path(__file__).parent / "classifications.json"


def main():
    with open(CLASSIFICATIONS_PATH) as f:
        classifications = json.load(f)

    sites_path = WEB_DATA_DIR / "sites.json"
    with open(sites_path) as f:
        sites = json.load(f)

    updated = 0
    unmatched = []
    for s in sites:
        if s["id"] in classifications:
            s["type"] = classifications[s["id"]]
            updated += 1
        else:
            unmatched.append(s["id"])

    with open(sites_path, "w") as f:
        json.dump(sites, f, indent=2)
    print(f"classified {updated}/{len(sites)} sites -> {sites_path}")
    if unmatched:
        print(f"no classification for: {unmatched}")

    geojson_path = WEB_DATA_DIR / "sites.geojson"
    with open(geojson_path) as f:
        gj = json.load(f)
    for feat in gj["features"]:
        sid = feat["properties"]["id"]
        if sid in classifications:
            feat["properties"]["type"] = classifications[sid]
    with open(geojson_path, "w") as f:
        json.dump(gj, f)
    print(f"patched types into {geojson_path}")


if __name__ == "__main__":
    main()
