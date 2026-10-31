"""Check the time axis values and sample a small patch for nodata fraction across zones."""

import datetime

import numpy as np
import zarr
from obstore.store import S3Store

STORE_URL = "s3://olmoearth/olmoearth-v1-3-embeddings/embeddings.zarr"
ENDPOINT = "https://data.source.coop"


def main():
    s3 = S3Store.from_url(STORE_URL, endpoint=ENDPOINT, skip_signature=True)
    store = zarr.storage.ObjectStore(s3, read_only=True)
    root = zarr.open_group(store, mode="r")

    for zname in ["utm10", "utm32", "utm33", "utm36"]:
        zone = root[zname]
        t = zone["time"][:]
        print(f"\n=== {zname} time values ===")
        print(t)
        try:
            print([datetime.datetime.utcfromtimestamp(int(v)).isoformat() for v in t])
        except Exception as e:
            print("not unix seconds:", e)

        emb = zone["embeddings"]
        print("shape", emb.shape)
        # sample a small 64x64 patch at a fixed offset, first band group, all times
        y0, x0 = emb.shape[2] // 2, emb.shape[3] // 2
        patch = emb[:, 0, y0 : y0 + 64, x0 : x0 + 64]
        nodata_frac = (patch == -128).mean(axis=(1, 2))
        print("nodata fraction per time (center patch, band0):", nodata_frac)


if __name__ == "__main__":
    main()
