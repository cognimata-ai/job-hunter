from dataclasses import replace
from datetime import UTC, datetime, timedelta

from jobhunter.sources.coverage import JSearchContinuation, adzuna_max_days_old, freshness_bucket
from jobhunter.types import JSONValue

T = datetime(2026, 10, 16, 6, 0, tzinfo=UTC)
H = timedelta(hours=1)


def test_bucket_selection_and_abandoned_span() -> None:
    assert (
        freshness_bucket(T, None)[0] == "month" and freshness_bucket(T, T - 1 * H)[0] == "3days"
    )  # needs ≥ 25 h: the smallest bucket ≥ that is 3days
    assert (
        freshness_bucket(T, T - 48 * H) == ("3days", (T - 72 * H, T), None)
        and freshness_bucket(T, T - 100 * H)[0] == "week"
    )
    bucket, window, abandoned = freshness_bucket(T, T - 800 * H)
    assert (bucket, window, abandoned) == ("month", (T - 720 * H, T), (T - 800 * H, T - 720 * H))
    assert (
        adzuna_max_days_old(T, T - timedelta(days=2, hours=1), 30) == 4
        and adzuna_max_days_old(T, None, 30) == 30
        and adzuna_max_days_old(T, T - timedelta(days=60), 30) == 30
    )


def test_a_span_just_over_the_month_abandons_nothing_reachable() -> None:
    # needs 734 h: `month`, but the window [T - 720 h, T] still reaches back past the last coverage
    assert freshness_bucket(T, T - 710 * H) == ("month", (T - 720 * H, T), None)
    assert freshness_bucket(T, T - 720 * H) == ("month", (T - 720 * H, T), None)
    assert adzuna_max_days_old(T, T, 30) == 1 and adzuna_max_days_old(T, T + H, 30) == 1


def test_the_jsearch_continuation_document_round_trips_and_refuses_anything_else() -> None:
    lower, upper = T - 73 * H, T - H
    cont = JSearchContinuation(
        cursor="c-2", obtained_at=T - H, window=(lower, upper), page_no=2, new_native_ids=7
    )
    assert JSearchContinuation.parse(cont.dump()) == cont and cont.admitted()
    # the §5.3.1 page rule (final review F3): page 2 after a page 1 with at least 7 new ids, no page 3
    assert not replace(cont, new_native_ids=6).admitted() and not replace(cont, page_no=3).admitted()
    doc = cont.dump()
    broken: list[JSONValue] = [
        None,
        [],
        "c-2",
        {**doc, "cursor": ""},
        {**doc, "cursor": 7},
        {**doc, "obtained_at": "yesterday"},
        {**doc, "obtained_at": "2026-10-16T05:00:00"},  # no offset
        {**doc, "window": [upper.isoformat(), lower.isoformat()]},  # reversed
        {**doc, "window": [lower.isoformat()]},
        {**doc, "page_no": 1},
        {**doc, "page_no": True},
        {key: value for key, value in doc.items() if key != "page_no"},
        {**doc, "new_native_ids": -1},
        {**doc, "new_native_ids": True},
        {key: value for key, value in doc.items() if key != "new_native_ids"},  # written before the rule
    ]
    for case in broken:
        assert JSearchContinuation.parse(case) is None, case
