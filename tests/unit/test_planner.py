from datetime import UTC, datetime, timedelta
from typing import Any

from jobhunter.quota.planner import CellRow, location_keys, order_paid, priority
from jobhunter.run_settings import scope_from_payload

# DEFAULT_CELL: the CellRow fields of a never-attempted JSearch cell, as a dict
from tests.support.collect import DEFAULT_CELL
from tests.support.quota import seeded_payload

T = datetime(2026, 10, 16, tzinfo=UTC)


def cell(**k: Any) -> CellRow:
    return CellRow(**{**DEFAULT_CELL, **k})


def test_priority_formula() -> None:
    assert priority(cell(last_attempted_at=None), T) == 10_000.0
    assert priority(cell(last_attempted_at=T - timedelta(hours=48), yield_ema=0.2), T) == 48 * 1.0 * 0.7
    assert (
        priority(cell(last_attempted_at=T - timedelta(hours=48), yield_ema=0.2, zero_yield_streak=3), T)
        == 48 * 0.25 * 0.7
    )
    assert (
        priority(cell(last_attempted_at=T - timedelta(days=8), yield_ema=0.0, zero_yield_streak=5), T)
        == 192 * 1.0 * 0.5
    )  # a demoted cell is refreshed after 7 days


def test_exploration_goes_first_after_ten_units() -> None:
    normal, explore = (
        cell(id=1, last_attempted_at=None),
        cell(id=2, exploration=True, last_attempted_at=T - timedelta(hours=1)),
    )
    assert [c.id for c in order_paid([normal, explore], now=T, units_since_exploration=9)] == [1, 2]
    assert [c.id for c in order_paid([normal, explore], now=T, units_since_exploration=10)] == [2, 1]


def test_paid_order_is_by_priority_then_id() -> None:
    stale, fresh, twin = (
        cell(id=3, last_attempted_at=T - timedelta(hours=10)),
        cell(id=1, last_attempted_at=T - timedelta(hours=2)),
        cell(id=2, last_attempted_at=T - timedelta(hours=10)),
    )
    assert [c.id for c in order_paid([stale, fresh, twin], now=T, units_since_exploration=0)] == [2, 3, 1]


def test_location_keys_are_slugs_without_duplicates_plus_remote() -> None:
    payload = seeded_payload()["scope"]
    place = {"gazetteer_id": "geonames:1", "lat": 52.5, "lon": 13.4, "radius_km": 25}
    payload["locations"] = [
        {**place, "label": "Frankfurt am Main"},
        {**place, "label": "Köln"},
        {**place, "label": "frankfurt-am-main"},  # the same slug: the first label wins
    ]
    scope = scope_from_payload(payload)
    assert location_keys(scope) == [
        ("frankfurt-am-main", "Frankfurt am Main"),
        ("koln", "Köln"),
        ("remote-de", None),
    ]
    payload["remote_within_germany"] = False
    assert location_keys(scope_from_payload(payload)) == [
        ("frankfurt-am-main", "Frankfurt am Main"),
        ("koln", "Köln"),
    ]
