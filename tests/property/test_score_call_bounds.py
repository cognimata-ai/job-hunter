"""The call bounds of a score attempt over random walks (spec §9.4.2 L3567, §18.1 L5699; plan Task 9):
whatever each step returns, the routing never makes more than 7 logical calls or 42 HTTP tries,
`logical_calls ≤ http_calls`, the counters satisfy the `score_attempts` CHECKs (`logical_calls BETWEEN 0
AND 7`, `http_calls BETWEEN 0 AND 42`, `logical_calls <= http_calls`), the revisions and critic rounds stay
within 2 and 3, and every walk ends in `publish` or `fail`.
"""

from hypothesis import given, settings
from hypothesis import strategies as st
from jobhunter.scoring.routing import MAX_CRITIC_ROUNDS, MAX_HTTP_CALLS, MAX_LOGICAL_CALLS, MAX_REVISIONS

from tests.support.scoring import Walk, route

ERRORS = ("refused", "max_tokens", "llm_transient", "budget_exhausted", "claim_lost", "invalid_output_stop")
GENERATIONS = st.sampled_from(("ok", "ok", "invalid", *ERRORS))
REVIEWS = st.sampled_from(("accept", "revise", "revise", "incomplete", "invalid", *ERRORS))
TRIGGERS = st.lists(st.sampled_from(((), ("T0",), ("T2",), ("T5",), ("T1", "T6"))), min_size=1, max_size=4)
FAILURES = st.lists(st.sampled_from(((), (), ("skills",), ("must_have:0",))), min_size=1, max_size=4)


@st.composite
def walks(draw: st.DrawFn) -> Walk:
    return Walk(
        # enough of each for the longest walk: one more than the step can be called
        generate=tuple(draw(st.lists(GENERATIONS, min_size=2, max_size=2))),
        critic=tuple(draw(st.lists(REVIEWS, min_size=4, max_size=4))),
        revise=tuple(draw(st.lists(GENERATIONS, min_size=3, max_size=3))),
        triggers=tuple(draw(TRIGGERS)),
        failures=tuple(draw(FAILURES)),
        tries=tuple(draw(st.lists(st.integers(1, 6), min_size=1, max_size=7))),
    )


@settings(max_examples=500, deadline=None)
@given(walks())
def test_random_walks_stay_within_the_call_bounds(walk: Walk) -> None:
    trace = route(walk)
    assert 0 <= trace.logical_calls <= MAX_LOGICAL_CALLS == 7
    assert 0 <= trace.http_calls <= MAX_HTTP_CALLS == 42
    assert trace.logical_calls <= trace.http_calls
    assert trace.round <= MAX_REVISIONS and trace.critic_rounds <= MAX_CRITIC_ROUNDS
    assert trace.steps.count("repair") <= 1
    assert trace.steps[-1] in ("publish", "fail")
    assert trace.ends_with.startswith(("publish(accepted)", "publish(contested)", "fail("))
