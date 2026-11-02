"""The bounds of the location filter's jsonpath steps (`jobs.listing._near_box`; plan M4 Task 5, B29).

Every point of the inner box lies within the radius by `geo.gazetteer.distance_km` (the formula of `geo_km`
term by term), so a candidate the box accepts is one `job_within_km` accepts; and every point within the
radius lies in the latitude band, which `job_within_km` checks first, so the band passes over none of its
matches. Both hold at the poles and across the antimeridian.
"""

import math

from hypothesis import given, settings
from hypothesis import strategies as st
from jobhunter.geo.gazetteer import distance_km
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
    phi, delta = math.radians(lat), distance / 6371.0088
    point = math.asin(math.sin(phi) * math.cos(delta) + math.cos(phi) * math.sin(delta) * math.cos(bearing))
    box = _near_box(lat, lon, radius_km)
    assert box["lo"] <= math.degrees(point) <= box["hi"]


def test_the_box_is_inside_the_band_and_spans_the_meridians_it_must() -> None:
    box = _near_box(52.52437, 13.41053, 30)
    assert box["lo"] < box["lo_in"] < 52.52437 < box["hi_in"] < box["hi"]
    assert box["west"] < 13.41053 < box["east"]
    across = _near_box(0.0, 0.0, 30)  # the band holds the equator: a degree of longitude is longest there
    assert math.isclose(across["east"] - across["west"], across["hi_in"] - across["lo_in"])
