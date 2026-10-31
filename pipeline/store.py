"""Shared helper for opening the OlmoEarth Zarr store."""

from __future__ import annotations

import zarr
from obstore.store import S3Store


def open_root(store_url: str, endpoint: str):
    s3 = S3Store.from_url(store_url, endpoint=endpoint, skip_signature=True)
    store = zarr.storage.ObjectStore(s3, read_only=True)
    return zarr.open_group(store, mode="r")
