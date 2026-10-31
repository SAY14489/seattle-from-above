"""Reverse-geocode each site's centroid via Nominatim and patch name/country into
the exported web data. Rate-limited to Nominatim's usage policy (max 1 req/sec).

Run after export_web_data.py. Idempotent: skips sites that already have a name
that isn't the generic "Site #N" placeholder.

Run: uv run python enrich_names.py
"""

import json
import time
from pathlib import Path

import requests
from tqdm import tqdm

WEB_DATA_DIR = Path(__file__).parent.parent / "web" / "public" / "data"
USER_AGENT = "EarthsBiggestChanges/0.1 (contact: yusufrm123@gmail.com)"


def reverse_geocode(lat: float, lon: float) -> dict:
    url = "https://nominatim.openstreetmap.org/reverse"
    params = {"format": "jsonv2", "lat": lat, "lon": lon, "zoom": 14, "addressdetails": 1}
    r = requests.get(url, params=params, headers={"User-Agent": USER_AGENT}, timeout=10)
    r.raise_for_status()
    return r.json()


def pick_name(data: dict) -> tuple[str, str | None, str | None]:
    """Returns (name, locality, country). `name` is the most specific place
    (neighbourhood/suburb if available); `locality` is one level broader
    (city/town, falling back to county then state) -- shown alongside name
    for context, since within one country/state that's what actually varies."""
    addr = data.get("address", {})
    suburb = addr.get("suburb") or addr.get("neighbourhood") or addr.get("quarter") or addr.get("city_district")
    city = addr.get("city") or addr.get("town") or addr.get("village") or addr.get("municipality")
    county = addr.get("county")
    state = addr.get("state")
    country = addr.get("country")

    name = suburb or city or county or state or data.get("name") or data.get("display_name", "Unknown location").split(",")[0]

    locality = city
    if locality == name or locality is None:
        locality = county if county and county != name else state
    return name, locality, country


def main():
    sites_path = WEB_DATA_DIR / "sites.json"
    geojson_path = WEB_DATA_DIR / "sites.geojson"

    with open(sites_path) as f:
        sites = json.load(f)

    updated = 0
    for s in tqdm(sites, desc="reverse geocoding"):
        if s["name"] and not s["name"].startswith("Site #"):
            continue
        try:
            data = reverse_geocode(s["centroid_lat"], s["centroid_lon"])
            name, locality, country = pick_name(data)
            s["name"] = name
            s["locality"] = locality
            s["country"] = country
            updated += 1
        except Exception as e:  # noqa: BLE001
            print(f"geocode failed for {s['id']}: {e}")
        time.sleep(1.1)  # Nominatim usage policy: max 1 request/sec

    with open(sites_path, "w") as f:
        json.dump(sites, f, indent=2)
    print(f"updated {updated} names -> {sites_path}")

    with open(geojson_path) as f:
        gj = json.load(f)
    name_by_id = {s["id"]: s["name"] for s in sites}
    for feat in gj["features"]:
        feat["properties"]["name"] = name_by_id.get(feat["properties"]["id"], feat["properties"]["name"])
    with open(geojson_path, "w") as f:
        json.dump(gj, f)
    print(f"patched names into {geojson_path}")


if __name__ == "__main__":
    main()
