"""M0 exploration: open the OlmoEarth v1.3 Zarr store and print metadata for one zone.

Run: uv run python explore_store.py
"""

import json

import zarr
from obstore.store import S3Store

STORE_URL = "s3://olmoearth/olmoearth-v1-3-embeddings/embeddings.zarr"
ENDPOINT = "https://data.source.coop"
ZONE = "utm10"


def main():
    s3 = S3Store.from_url(STORE_URL, endpoint=ENDPOINT, skip_signature=True)
    store = zarr.storage.ObjectStore(s3, read_only=True)

    root = zarr.open_group(store, mode="r")
    print("=== Root group members ===")
    print(list(root.array_keys()))
    print(list(root.group_keys()))
    print("=== Root attrs ===")
    print(json.dumps(dict(root.attrs), indent=2, default=str)[:4000])

    print(f"\n=== Opening zone {ZONE} ===")
    zone = root[ZONE]
    print("Members:", list(zone.array_keys()), list(zone.group_keys()))
    print("Zone attrs:", json.dumps(dict(zone.attrs), indent=2, default=str)[:4000])

    for name, arr in zone.arrays():
        print(f"\n--- array: {name} ---")
        print("shape:", arr.shape)
        print("dtype:", arr.dtype)
        print("chunks:", arr.chunks)
        try:
            print("shards:", arr.shards)
        except Exception as e:
            print("shards: n/a", e)
        print("attrs:", json.dumps(dict(arr.attrs), indent=2, default=str)[:4000])
        print("fill_value:", arr.fill_value)


if __name__ == "__main__":
    main()
