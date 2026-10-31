"""Geo helpers: lon/lat <-> UTM zone + pixel row/col, using each zone's Zarr spatial:transform."""

from __future__ import annotations

import math
from dataclasses import dataclass

from pyproj import Transformer


def utm_zone_number(lon: float) -> int:
    return int(math.floor((lon + 180) / 6) + 1)


def utm_epsg(lon: float, lat: float) -> int:
    zone = utm_zone_number(lon)
    return 32600 + zone if lat >= 0 else 32700 + zone


@dataclass
class ZoneGeo:
    """Pixel <-> projected-coords mapping for one UTM zone array, from its spatial:transform."""

    epsg: int
    a: float  # x pixel width
    b: float
    c: float  # x origin
    d: float
    e: float  # y pixel height (negative, north-up)
    f: float  # y origin (top)
    shape_y: int
    shape_x: int

    @classmethod
    def from_zarr_attrs(cls, attrs: dict) -> "ZoneGeo":
        epsg = int(attrs["proj:code"].split(":")[1])
        a, b, c, d, e, f = attrs["spatial:transform"]
        shape_y, shape_x = attrs["spatial:shape"]
        return cls(epsg=epsg, a=a, b=b, c=c, d=d, e=e, f=f, shape_y=shape_y, shape_x=shape_x)

    def lonlat_to_colrow(self, lon: float, lat: float) -> tuple[int, int]:
        transformer = Transformer.from_crs("EPSG:4326", f"EPSG:{self.epsg}", always_xy=True)
        x, y = transformer.transform(lon, lat)
        col = int(round((x - self.c) / self.a))
        row = int(round((self.f - y) / -self.e))
        return col, row

    def colrow_to_lonlat(self, col: int, row: int) -> tuple[float, float]:
        x = self.c + col * self.a
        y = self.f + row * self.e
        transformer = Transformer.from_crs(f"EPSG:{self.epsg}", "EPSG:4326", always_xy=True)
        lon, lat = transformer.transform(x, y)
        return lon, lat

    def meters_to_pixels(self, meters: float) -> int:
        return int(round(meters / abs(self.a)))


def zone_name_for_lonlat(lon: float, lat: float) -> str:
    zone = utm_zone_number(lon)
    return f"utm{zone:02d}"
