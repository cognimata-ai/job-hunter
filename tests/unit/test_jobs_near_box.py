"""The bounds of the location filter's jsonpath steps (`jobs.listing._near_box`; plan M4 Task 5, B29).

Every point of the inner box lies within the radius by `geo.gazetteer.distance_km` (the formula of `geo_km`
term by term), so a candidate the box accepts is one `job_within_km` accepts; and every point within the
radius lies in the band (its latitude bound and, where `job_within_km` applies one, its longitude bound), so
the band passes over none of its matches. Both hold at the poles and across the antimeridian.
"""

import math

from hypothesis import given, settings
from hypothesis import strategies as st
from jobhunter.geo.gazetteer import EARTH_RADIUS_KM, distance_km
from jobhunter.jobs.listing import _near_box

latitudes = st.floats(min_value=-90, max_value=90, allow_nan=False)
longitudes = st.floats(min_value=-180, max_value=180, allow_nan=False)
radii = st.integers(min_value=1, max_value=200)
shares = st.floats(min_value=-1, max_value=1, allow_nan=False)


@settings(max_examples=2_000, deadline=None)
@given(latitudes, longitudes, radii, shares, shares)
def test_every_point_of_the_inner_box_is_within_the_radius(
    lat: float, lon: float, radius_km: int, north: float, east: float
) -> None:
    box = _near_box(lat, lon, radius_km)
    lo, hi = box["lo_in"], box["hi_in"]
    point_lat = min(90.0, max(-90.0, (lo + hi) / 2 + north * (hi - lo) / 2))
    point_lon = (box["west"] + box["east"]) / 2 + east * (box["east"] - box["west"]) / 2
    assert distance_km(lat, lon, point_lat, point_lon) <= radius_km


@settings(max_examples=2_000, deadline=None)
@given(latitudes, longitudes, radii, st.floats(0, 1), st.floats(0, 2 * math.pi))
def test_every_point_within_the_radius_lies_in_the_band(
    lat: float, lon: float, radius_km: int, share: float, bearing: float
) -> None:
    distance = share * radius_km
    phi, delta = math.radians(lat), distance / EARTH_RADIUS_KM
    point = math.asin(math.sin(phi) * math.cos(delta) + math.cos(phi) * math.sin(delta) * math.cos(bearing))
    east = math.atan2(
        math.sin(bearing) * math.sin(delta) * math.cos(phi), math.cos(delta) - math.sin(phi) * math.sin(point)
    )
    point_lon = (lon + math.degrees(east) + 180) % 360 - 180  # a candidate's longitude is in [-180, 180)
    box = _near_box(lat, lon, radius_km)
    assert box["lo"] <= math.degrees(point) <= box["hi"]
    assert box["lo_lon"] <= point_lon <= box["hi_lon"]


def test_the_band_bounds_longitude_only_where_job_within_km_does_and_never_across_180() -> None:
    """`job_within_km` bounds |Δlon| while |lat| + radius / 110.5 < 89; the band does so with its widening,
    and only when the bounds stay within ±180: beside each cut-off, inside it and beyond it (30 km: the
    latitude reach is 0.2715°, the longitude reach at the equator 0.2715° too)."""
    bounded = [(88.72, 0.0), (-88.72, 0.0), (0.0, 179.72), (0.0, -179.72), (52.52437, 13.41053)]
    unbounded = [(88.73, 0.0), (-88.73, 0.0), (0.0, 179.73), (0.0, -179.73), (90.0, 0.0)]
    for lat, lon in bounded:
        box = _near_box(lat, lon, 30)
        assert -180 < box["lo_lon"] < lon < box["hi_lon"] < 180, (lat, lon)
    for lat, lon in unbounded:
        box = _near_box(lat, lon, 30)
        assert (box["lo_lon"], box["hi_lon"]) == (-360.0, 360.0), (lat, lon)


def test_the_box_is_inside_the_band_and_spans_the_meridians_it_must() -> None:
    box = _near_box(52.52437, 13.41053, 30)
    assert box["lo"] < box["lo_in"] < 52.52437 < box["hi_in"] < box["hi"]
    assert box["west"] < 13.41053 < box["east"]
    across = _near_box(0.0, 0.0, 30)  # the band holds the equator: a degree of longitude is longest there
    assert math.isclose(across["east"] - across["west"], across["hi_in"] - across["lo_in"])
