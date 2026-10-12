"""Fonctions géographiques : distances, cap, polyline Google, projection sur un tracé."""

import math
from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass

EARTH_RADIUS_KM = 6371.0088

LatLon = tuple[float, float]


def haversine_km(a: LatLon, b: LatLon) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(h))


def bearing_deg(a: LatLon, b: LatLon) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    y = math.sin(lon2 - lon1) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(lon2 - lon1)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def destination_point(origin: LatLon, bearing: float, distance_km: float) -> LatLon:
    lat1, lon1 = math.radians(origin[0]), math.radians(origin[1])
    brg = math.radians(bearing)
    d = distance_km / EARTH_RADIUS_KM
    lat2 = math.asin(math.sin(lat1) * math.cos(d) + math.cos(lat1) * math.sin(d) * math.cos(brg))
    lon2 = lon1 + math.atan2(
        math.sin(brg) * math.sin(d) * math.cos(lat1), math.cos(d) - math.sin(lat1) * math.sin(lat2)
    )
    return math.degrees(lat2), (math.degrees(lon2) + 540) % 360 - 180


# --- Polyline (format Google, précision 1e5) -------------------------------------------


def _encode_value(value: int) -> str:
    value = ~(value << 1) if value < 0 else value << 1
    chunks = []
    while value >= 0x20:
        chunks.append(chr((0x20 | (value & 0x1F)) + 63))
        value >>= 5
    chunks.append(chr(value + 63))
    return "".join(chunks)


def encode_polyline(points: Sequence[LatLon], precision: int = 5) -> str:
    factor = 10**precision
    out, prev_lat, prev_lon = [], 0, 0
    for lat, lon in points:
        ilat, ilon = round(lat * factor), round(lon * factor)
        out.append(_encode_value(ilat - prev_lat))
        out.append(_encode_value(ilon - prev_lon))
        prev_lat, prev_lon = ilat, ilon
    return "".join(out)


def decode_polyline(encoded: str, precision: int = 5) -> list[LatLon]:
    factor = 10**precision
    points: list[LatLon] = []
    index = lat = lon = 0
    while index < len(encoded):
        deltas = []
        for _ in range(2):
            shift = result = 0
            while True:
                b = ord(encoded[index]) - 63
                index += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            deltas.append(~(result >> 1) if result & 1 else result >> 1)
        lat += deltas[0]
        lon += deltas[1]
        points.append((lat / factor, lon / factor))
    return points


# --- Tracé ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Projection:
    along_km: float  # distance parcourue le long du tracé jusqu'au point projeté
    offset_km: float  # distance entre le point et le tracé
    point: LatLon


class RoutePath:
    """Tracé polyligne avec abscisse curviligne, pour interpoler et projeter."""

    def __init__(self, points: Sequence[LatLon]):
        if len(points) < 2:
            raise ValueError("un tracé nécessite au moins deux points")
        self.points = [tuple(p) for p in points]
        self.cumulative = [0.0]
        for a, b in zip(self.points, self.points[1:]):
            self.cumulative.append(self.cumulative[-1] + haversine_km(a, b))

    @property
    def length_km(self) -> float:
        return self.cumulative[-1]

    def point_at(self, distance_km: float) -> tuple[LatLon, float]:
        """Position et cap à une distance donnée depuis le départ."""
        d = min(max(distance_km, 0.0), self.length_km)
        i = min(bisect_right(self.cumulative, d) - 1, len(self.points) - 2)
        a, b = self.points[i], self.points[i + 1]
        seg = self.cumulative[i + 1] - self.cumulative[i]
        t = 0.0 if seg == 0 else (d - self.cumulative[i]) / seg
        point = (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
        return point, bearing_deg(a, b)

    def project(self, p: LatLon) -> Projection:
        """Projection orthogonale du point sur le tracé (approximation équirectangulaire locale)."""
        best: Projection | None = None
        for i, (a, b) in enumerate(zip(self.points, self.points[1:])):
            kx = math.cos(math.radians((a[0] + b[0]) / 2)) * 111.32
            ky = 110.574
            ax, ay = a[1] * kx, a[0] * ky
            bx, by = b[1] * kx, b[0] * ky
            px, py = p[1] * kx, p[0] * ky
            dx, dy = bx - ax, by - ay
            seg2 = dx * dx + dy * dy
            t = 0.0 if seg2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg2))
            qx, qy = ax + t * dx, ay + t * dy
            offset = math.hypot(px - qx, py - qy)
            if best is None or offset < best.offset_km:
                along = self.cumulative[i] + t * (self.cumulative[i + 1] - self.cumulative[i])
                best = Projection(along, offset, (qy / ky, qx / kx))
        assert best is not None
        return best


def densify(points: Sequence[LatLon], step_km: float = 1.0) -> list[LatLon]:
    """Ajoute des points intermédiaires pour un tracé plus régulier."""
    out: list[LatLon] = [tuple(points[0])]
    for a, b in zip(points, points[1:]):
        n = max(1, math.ceil(haversine_km(a, b) / step_km))
        for k in range(1, n + 1):
            t = k / n
            out.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
    return out
