"""The call bounds of a score attempt over random walks of its routing (spec §9.4.2 L3567-3570, §18.1 L5699;
plan Task 9), from a fresh start and from a resumed one (a restored draft, with or without its review):
whatever each step returns, the routing makes at most 7 logical calls (one generation and its repair, two
revisions, three critic rounds), never a third revision or a fourth critic round, at most one structural
repair, never a generation on a resumed attempt, never a critic call on a restored draft that has its review,
and every walk ends in `publish` or `fail`; an `accepted` publication comes from a derived `accept` of a draft
without failures, or from a clean first draft no critic saw (never from numerical convergence, L3570).

The draws favour the outcomes that go on (a revision, an incomplete review, a stored draft) and
`hypothesis.target` steers toward long walks, so an off-by-one in a bound shows.
"""

from hypothesis import given, settings, target
from hypothesis import strategies as st
from jobhunter.scoring.routing import MAX_CRITIC_ROUNDS, MAX_LOGICAL_CALLS, MAX_REVISIONS, DerivedOutcome

from tests.support.scoring import Resume, Walk, route

ERRORS = ("refused", "max_tokens", "llm_transient", "budget_exhausted", "claim_lost")
GENERATIONS = st.sampled_from(("ok",) * 6 + ("invalid",) * 2 + ERRORS)
REVIEWS = st.sampled_from(("revise",) * 6 + ("incomplete",) * 3 + ("accept",) * 2 + ("invalid",) * 2 + ERRORS)
TRIGGERS = st.lists(st.sampled_from(((), ("T0",), ("T2",), ("T5",), ("T1", "T6"))), min_size=1, max_size=4)
FAILURES = st.lists(st.sampled_from(((), (), ("skills",), ("must_have:0",))), min_size=1, max_size=4)
RESTORED: tuple[DerivedOutcome, ...] = ("accept", "revise", "incomplete")
# More of each than any routing within its bounds can take, so a broken bound runs into the assertions.
SCRIPT = 8


@st.composite
def resumes(draw: st.DrawFn) -> Resume:
    """A restored current draft: its review was committed (the chain's critic rounds are its round + 1), or a
    draft newer than every note (as many critic rounds as revisions)."""
    round = draw(st.integers(0, MAX_REVISIONS))
    review = draw(st.none() | st.sampled_from(RESTORED))
    return Resume(round=round, critic_rounds=round + (review is not None), review=review)


@st.composite
def walks(draw: st.DrawFn) -> Walk:
    return Walk(
        generate=tuple(draw(st.lists(GENERATIONS, min_size=SCRIPT, max_size=SCRIPT))),
        critic=tuple(draw(st.lists(REVIEWS, min_size=SCRIPT, max_size=SCRIPT))),
        revise=tuple(draw(st.lists(GENERATIONS, min_size=SCRIPT, max_size=SCRIPT))),
        triggers=tuple(draw(TRIGGERS)),
        failures=tuple(draw(FAILURES)),
        tries=tuple(draw(st.lists(st.integers(1, 6), min_size=1, max_size=7))),
        resume=draw(st.none() | resumes()),
    )


@settings(max_examples=1000, deadline=None)
@given(walks())
def test_random_walks_stay_within_the_call_bounds(walk: Walk) -> None:
    trace = route(walk)
    target(len(trace.steps))
    steps = trace.steps
    assert trace.logical_calls <= MAX_LOGICAL_CALLS == 7
    assert trace.round <= MAX_REVISIONS and trace.critic_rounds <= MAX_CRITIC_ROUNDS
    assert steps.count("repair") <= 1
    revisions = steps.count("revise") + (walk.resume.round if walk.resume else 0)
    assert revisions <= MAX_REVISIONS and steps.count("generate") <= 1
    assert steps[-1] in ("publish", "fail")
    if walk.resume is not None:
        assert "generate" not in steps
        if walk.resume.review is not None:  # routed by its restored review: never the critic again
            assert steps[1] != "critic"
    if trace.ends_with == "publish(accepted)":
        accepted = trace.last_review == "accept" and not trace.reviewed_failures
        unreviewed = trace.critic_rounds == 0 and walk.triggers[0] == () and walk.failures[0] == ()
        assert accepted or unreviewed
