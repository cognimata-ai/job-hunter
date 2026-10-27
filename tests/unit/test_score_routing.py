"""The routing of one score attempt (spec §9.4.2 L3546-3568; plan Task 9, Deviation 17): the maximal path uses
exactly 7 logical calls, a budget denial before the first dispatch ends the attempt cleanly, every later
draft goes back to the critic once one ran (T0), and the settled exits of Deviation 17 (a resumed draft that
already has its review, an invalid revision after the repair, the error exits of critic and revise)."""

from dataclasses import replace
from decimal import Decimal

import pytest
from jobhunter.errors import FencedOut
from jobhunter.graphs.score.llm_step import StepOutcome, error_code_of, outcome_for, outcome_of
from jobhunter.llm.errors import (
    BudgetExhausted,
    ClaimLost,
    DeadlineExhausted,
    InputUnbounded,
    InvalidOutput,
    LLMNotConfigured,
    LLMRequestError,
    LLMTransient,
    MaxTokensExhausted,
    ModelMismatch,
    Refused,
)
from jobhunter.llm.prompts.render import PromptVersionUnscorable
from jobhunter.scoring import routing
from jobhunter.scoring.routing import Next
from langgraph.errors import NodeTimeoutError

from tests.support.scoring import Walk, route

MAXIMAL = ("generate", "repair", "validate", "critic", "revise", "validate", "critic", "revise", "validate")


def test_the_maximal_path_uses_exactly_seven_logical_calls() -> None:
    walk = Walk(generate="invalid_then_ok", critic=("revise", "revise", "accept"), revise=("ok", "ok"))
    trace = route(
        walk
    )  # generate, repair, validate, critic, revise, validate, critic, revise, validate, critic
    assert trace.steps == (*MAXIMAL, "critic", "publish")
    assert trace.logical_calls == 7 and trace.http_calls <= 42 and trace.ends_with == "publish(accepted)"
    assert (trace.round, trace.critic_rounds) == (2, 3)
    # every logical call at its 6 HTTP tries is the 42 of L3567, exactly
    assert route(replace(walk, tries=(6,))).http_calls == 42


def test_budget_denied_before_the_first_dispatch_ends_cleanly() -> None:
    trace = route(Walk(generate="budget_exhausted"))
    assert (trace.logical_calls, trace.http_calls, trace.ends_with) == (0, 0, "fail(budget_exhausted)")
    assert trace.steps == ("generate", "fail")


def test_once_a_critic_ran_every_later_draft_returns_to_the_critic() -> None:
    # the revised draft has no trigger and no failure of its own: it still goes back to the critic (T0)
    trace = route(Walk(critic=("revise", "accept"), revise=("ok",), triggers=(("T2",), ())))
    assert trace.steps == ("generate", "validate", "critic", "revise", "validate", "critic", "publish")
    assert trace.ends_with == "publish(accepted)"
    nxt = routing.after_validate(triggers=[], det_failures=[], critic_rounds=1, round=1, review=None)
    assert nxt == Next("critic")


def test_a_clean_draft_publishes_accepted_without_a_critic() -> None:
    trace = route(Walk(triggers=((),)))
    assert trace.steps == ("generate", "validate", "publish") and trace.logical_calls == 1
    assert trace.ends_with == "publish(accepted)"


def test_a_failure_alone_sends_the_draft_to_the_critic() -> None:
    nxt = routing.after_validate(triggers=[], det_failures=["skills"], critic_rounds=0, round=0, review=None)
    assert nxt == Next("critic")


def test_a_derived_accept_with_a_failure_is_no_acceptance() -> None:
    assert routing.after_critic(review="accept", det_failures=["skills"], round=0, repair_used=False) == Next(
        "revise"
    )
    assert routing.after_critic(review="accept", det_failures=["skills"], round=2, repair_used=False) == (
        routing.publish("contested")
    )


def test_the_second_revision_ends_contested_after_its_critic() -> None:
    trace = route(Walk(critic=("revise", "incomplete", "revise"), revise=("ok", "ok")))
    assert trace.ends_with == "publish(contested)" and trace.logical_calls == 6
    assert trace.steps[-2:] == ("critic", "publish")


def test_one_repair_is_shared_by_every_step() -> None:
    # the generator used the repair: the critic's invalid output publishes contested, never a second repair
    trace = route(Walk(generate="invalid_then_ok", critic=("invalid",)))
    assert trace.steps == ("generate", "repair", "validate", "critic", "publish")
    assert trace.ends_with == "publish(contested)" and trace.logical_calls == 3
    # an unused repair is the critic's own
    trace = route(Walk(critic=("invalid", "accept")))
    assert trace.steps == ("generate", "validate", "critic", "repair", "publish")
    assert trace.ends_with == "publish(accepted)" and trace.critic_rounds == 1


def test_a_second_invalid_generation_fails_and_a_second_invalid_revision_publishes_contested() -> None:
    assert route(Walk(generate="invalid_then_invalid")).ends_with == "fail(invalid_output)"
    trace = route(Walk(critic=("revise",), revise=("invalid", "invalid")))
    assert trace.steps[-3:] == ("revise", "repair", "publish")
    assert trace.ends_with == "publish(contested)" and trace.round == 0  # the last validated draft


@pytest.mark.parametrize("step", ["critic", "revise"])
@pytest.mark.parametrize("code", ["refused", "max_tokens", "llm_transient", "budget_exhausted", "claim_lost"])
def test_the_error_exits_apply_to_the_critic_and_the_reviser(step: str, code: str) -> None:
    walk = Walk(critic=(code,)) if step == "critic" else Walk(critic=("revise",), revise=(code,))
    trace = route(walk)
    assert trace.ends_with == f"fail({code})" and trace.steps[-2:] == (step, "fail")


@pytest.mark.parametrize(
    ("review", "round", "expected"),
    [
        ("accept", 0, routing.publish("accepted")),
        ("revise", 0, Next("revise")),
        ("incomplete", 1, Next("revise")),
        ("revise", 2, routing.publish("contested")),  # the exhausted-revision rule, restored (Codex r3 B9)
    ],
)
def test_a_resumed_draft_with_its_review_is_routed_by_it(
    review: routing.DerivedOutcome, round: int, expected: Next
) -> None:
    nxt = routing.after_validate(
        triggers=["T0"], det_failures=[], critic_rounds=round + 1, round=round, review=review
    )
    assert nxt == expected  # never the critic again (Codex r2 B23)


def test_three_critic_rounds_already_publish_contested_on_a_resume() -> None:
    nxt = routing.after_validate(triggers=["T0"], det_failures=[], critic_rounds=3, round=2, review=None)
    assert nxt == routing.publish("contested")


@pytest.mark.parametrize(
    ("outcome", "error_code", "resumed", "expected"),
    [
        ("claim_lost", None, False, Next("__end__")),
        ("already_published", None, False, Next("publish")),
        (None, "prompt_version_unscorable", False, routing.fail("prompt_version_unscorable")),
        (None, None, True, Next("validate")),
        (None, None, False, Next("generate")),
    ],
)
def test_after_load_context(
    outcome: str | None, error_code: str | None, resumed: bool, expected: Next
) -> None:
    assert routing.after_load_context(outcome=outcome, error_code=error_code, resumed=resumed) == expected


# ---- the score mapping of a failed step (`llm_step`, A2 §2.6) --------------------------------------------


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (Refused("cyber"), StepOutcome("refused", "refused", "refused", "cyber", False)),
        (Refused(""), StepOutcome("refused", "refused", "refused", "unspecified", False)),
        (
            MaxTokensExhausted(),
            StepOutcome("max_tokens", "failed_max_tokens", "failed_max_tokens", "max_tokens", False),
        ),
        (
            InvalidOutput("structural"),
            StepOutcome(
                "invalid_output", "failed_invalid_output", "failed_invalid_output", "invalid_output", False
            ),
        ),
        (
            InvalidOutput("unexpected_stop_reason"),
            StepOutcome(
                "unexpected_stop_reason",
                "failed_invalid_output",
                "failed_invalid_output",
                "unexpected_stop_reason",
                False,
            ),
        ),
        (
            LLMRequestError(422),
            StepOutcome("llm_request_422", "failed_request", "failed_request", "422", False),
        ),
        (
            ModelMismatch("claude-opus-5-5", "claude-sonnet-5-5"),
            StepOutcome("model_mismatch", "failed_request", "failed_request", "model_mismatch", False),
        ),
        (
            InputUnbounded("unencodable"),
            StepOutcome("input_unbounded", "failed_request", "failed_request", "input_unbounded", False),
        ),
        (
            LLMTransient("http_529"),
            StepOutcome("llm_transient", "transient_error", "pending", "llm_transient", True),
        ),
        (
            DeadlineExhausted("late"),
            StepOutcome("llm_transient", "transient_error", "pending", "llm_transient", True),
        ),
        (
            NodeTimeoutError("critic", 3660.0, kind="run", run_timeout=3660.0),
            StepOutcome("llm_transient", "transient_error", "pending", "llm_transient", True),
        ),
        (
            LLMNotConfigured(),
            StepOutcome("llm_not_configured", "transient_error", "pending", "llm_not_configured", True),
        ),
        (
            PromptVersionUnscorable(1, "generator_v1"),
            StepOutcome(
                "prompt_version_unscorable", "transient_error", "pending", "prompt_version_unscorable", True
            ),
        ),
        (
            BudgetExhausted("scoring", Decimal(0)),
            StepOutcome("budget_exhausted", "budget_exhausted", "budget_deferred", "budget_exhausted", False),
        ),
        (ClaimLost(), StepOutcome("claim_lost", "claim_lost", None, None, False)),
        (FencedOut("purge_fence"), StepOutcome("claim_lost", "claim_lost", None, None, False)),
    ],
    ids=lambda value: type(value).__name__ if isinstance(value, BaseException) else "",
)
def test_each_failure_maps_to_its_attempt_and_target_outcome(
    error: BaseException, expected: StepOutcome
) -> None:
    assert outcome_of(error) == expected
    # the state carries the code alone; `fail` maps it back (a refusal's category comes from its call row)
    category = error.category if isinstance(error, Refused) else None
    assert outcome_for(error_code_of(error), refusal_category=category) == expected


def test_the_worker_s_own_fence_and_a_bug_have_no_score_outcome() -> None:
    with pytest.raises(TypeError):
        error_code_of(FencedOut("worker_epoch"))
    with pytest.raises(TypeError):
        error_code_of(ValueError("a bug"))
    with pytest.raises(ValueError, match="no score step"):
        outcome_for("not_a_code")
