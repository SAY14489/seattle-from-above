"""M0: read one small tile for two years, decode, save a PNG of the per-pixel change score.

Test site: Buckeye, AZ -- one of the fastest-growing exurbs in the US, well documented
in satellite timelapses as farmland turning into subdivisions.

Run: uv run python m0_data_access.py
"""

import numpy as np
import zarr
from obstore.store import S3Store
from PIL import Image

from geo import ZoneGeo, zone_name_for_lonlat

STORE_URL = "s3://olmoearth/olmoearth-v1-3-embeddings/embeddings.zarr"
ENDPOINT = "https://data.source.coop"

# Buckeye, AZ -- fast-growing Phoenix exurb, farmland -> subdivisions since ~2017.
CENTER_LON, CENTER_LAT = -112.5838, 33.3703
PATCH_PIXELS = 512  # 512 * 10m = 5.12 km square
YEAR_A, YEAR_B = 2017, 2025


def decode(q: np.ndarray) -> np.ndarray:
    """Signed-power dequantization confirmed from zarr attrs: (|q|/127.5)^2 * sign(q)."""
    q = q.astype(np.float32)
    valid = q != -128
    out = np.zeros_like(q, dtype=np.float32)
    out[valid] = np.sign(q[valid]) * (np.abs(q[valid]) / 127.5) ** 2.0
    return out, valid


def main():
    s3 = S3Store.from_url(STORE_URL, endpoint=ENDPOINT, skip_signature=True)
    store = zarr.storage.ObjectStore(s3, read_only=True)
    root = zarr.open_group(store, mode="r")

    zname = zone_name_for_lonlat(CENTER_LON, CENTER_LAT)
    print("zone:", zname)
    zone = root[zname]
    geo = ZoneGeo.from_zarr_attrs(dict(zone.attrs))
    print("epsg:", geo.epsg)

    col, row = geo.lonlat_to_colrow(CENTER_LON, CENTER_LAT)
    print("center col,row:", col, row)

    half = PATCH_PIXELS // 2
    y0, y1 = row - half, row + half
    x0, x1 = col - half, col + half
    print("pixel window: y", y0, y1, "x", x0, x1)

    time_vals = list(zone["time"][:])
    ta = time_vals.index(YEAR_A)
    tb = time_vals.index(YEAR_B)
    print("time indices:", ta, tb, "for years", YEAR_A, YEAR_B)

    emb = zone["embeddings"]
    print("reading patch A...")
    qa = emb[ta, :, y0:y1, x0:x1]  # (128, H, W) int8
    print("reading patch B...")
    qb = emb[tb, :, y0:y1, x0:x1]

    fa, valid_a = decode(qa)
    fb, valid_b = decode(qb)
    valid = valid_a.all(axis=0) & valid_b.all(axis=0)
    print("valid pixel fraction:", valid.mean())

    # L2-normalize each pixel's embedding vector across the band dimension.
    def l2norm(x):
        n = np.linalg.norm(x, axis=0, keepdims=True)
        n[n == 0] = 1.0
        return x / n

    na = l2norm(fa)
    nb = l2norm(fb)

    cos_sim = (na * nb).sum(axis=0)
    change = 1.0 - cos_sim  # cosine distance, 0 = identical
    change[~valid] = np.nan

    print("change score stats: min", np.nanmin(change), "max", np.nanmax(change), "mean", np.nanmean(change))

    # Save PNG: normalize valid change scores to 0-255, nodata as black.
    vmin, vmax = np.nanpercentile(change, 1), np.nanpercentile(change, 99)
    norm = np.clip((change - vmin) / max(vmax - vmin, 1e-6), 0, 1)
    img = (norm * 255).astype(np.uint8)
    img[~valid] = 0

    out_path = "../data/m0_change_score_buckeye.png"
    Image.fromarray(img, mode="L").save(out_path)
    print("saved", out_path)

    # Also save a simple before/after true-color-ish preview isn't possible from embeddings
    # (no RGB bands), so save raw decoded band-0 maps for a sanity check instead.
    def band0_png(f, valid_mask, path):
        v = f[0].copy()
        v[~valid_mask] = np.nan
        vmin2, vmax2 = np.nanpercentile(v, 1), np.nanpercentile(v, 99)
        n2 = np.clip((v - vmin2) / max(vmax2 - vmin2, 1e-6), 0, 1)
        im = (n2 * 255).astype(np.uint8)
        im[~valid_mask] = 0
        Image.fromarray(im, mode="L").save(path)

    band0_png(fa, valid_a.all(axis=0), "../data/m0_band0_2017.png")
    band0_png(fb, valid_b.all(axis=0), "../data/m0_band0_2025.png")
    print("saved band0 sanity PNGs")


if __name__ == "__main__":
    main()
