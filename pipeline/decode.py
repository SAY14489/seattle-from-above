"""Decode int8-quantized OlmoEarth embeddings to float32.

Formula confirmed from the dataset's zarr.json attrs (geoemb:quantization):
    float = sign(q) * (abs(q) / 127.5) ** 2.0
-128 is nodata.
"""

from __future__ import annotations

import numpy as np

NODATA = -128


def decode(q: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Return (decoded float32 array, valid mask), same shape as q."""
    valid = q != NODATA
    qf = q.astype(np.float32)
    out = np.sign(qf) * (np.abs(qf) / 127.5) ** 2.0
    out[~valid] = 0.0
    return out, valid


def l2_normalize(x: np.ndarray, axis: int = 0) -> np.ndarray:
    norm = np.linalg.norm(x, axis=axis, keepdims=True)
    norm[norm == 0] = 1.0
    return x / norm


def cosine_distance(a_norm: np.ndarray, b_norm: np.ndarray, axis: int = 0) -> np.ndarray:
    """a_norm, b_norm already L2-normalized along `axis`. Returns 1 - cosine_similarity."""
    sim = (a_norm * b_norm).sum(axis=axis)
    return 1.0 - sim
