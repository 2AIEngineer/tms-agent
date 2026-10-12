import pytest
from tms.geo import (
    RoutePath,
    decode_polyline,
    destination_point,
    encode_polyline,
    haversine_km,
)


def test_polyline_roundtrip_google_reference():
    # Exemple de la documentation Google.
    points = [(38.5, -120.2), (40.7, -120.95), (43.252, -126.453)]
    encoded = encode_polyline(points)
    assert encoded == "_p~iF~ps|U_ulLnnqC_mqNvxq`@"
    assert decode_polyline(encoded) == pytest.approx(points)


def test_haversine_casablanca_rabat():
    assert haversine_km((33.5731, -7.5898), (34.0209, -6.8416)) == pytest.approx(85.2, abs=0.5)


def test_projection_offset():
    path = RoutePath([(33.0, -7.0), (34.0, -7.0)])
    point = destination_point((33.5, -7.0), 90, 6.0)
    proj = path.project(point)
    assert proj.offset_km == pytest.approx(6.0, abs=0.05)
    assert proj.along_km == pytest.approx(path.length_km / 2, abs=0.2)
