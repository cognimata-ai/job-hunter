"""The routing of one score attempt (spec §9.4.2, L3546-3564; plan Task 9, Deviation 17): pure decisions of
which step comes next, from the attempt's counters and the outcome of the step that just ran. No I/O, no
clock. The `score_target` subgraph's nodes make each decision through these functions and go where it says
(`Next.route`); Task 19's eval runner drives the same steps through the same functions.

**The graph** (L3546-3564, with the settlements of Deviation 17):
- `load_context`: a lost claim ends the attempt (`END`, nothing written); an `llm_results` row for the
  5-tuple goes to `publish`, which returns it (`already_published`); an error (a prompt version that cannot
  score) goes to `fail`; a resume with a draft goes to `validate`; a fresh attempt to `generate`.
- `generate` and `revise` (`after_generation`): a stored draft goes to `validate`; a structurally invalid
  output goes to the same step again while the attempt's one structural repair is unused (`Next.repair`:
  `repair_used := true`); after the repair an invalid `generate` is `fail(invalid_output)` (L3553), an
  invalid `revise` publishes `contested` from the last validated draft (L4551, "never withheld"; A2 T3);
  every error goes to `fail` with its code (L3551, for `critic` and `revise` too: A2 T2).
- `validate` (`after_validate`): a draft that already has a critic review (only after a resume: the restored
  review of the current draft, Codex r2 B23) is routed by that review as the live path would have routed it
  after the critic (`after_review`), with no critic call; otherwise no trigger, no deterministic failure and
  no critic round yet publishes `accepted` (L3556), any trigger, failure or earlier critic round goes to the
  critic while fewer than 3 rounds ran (L3557: once a critic ran, every later draft goes back to it, T0,
  whatever its own triggers, L3568), and 3 rounds already (a resume) publish `contested` (L3558).
- `critic` (`after_critic`): an invalid output repairs once, then publishes `contested` (L3559-3560); a
  review goes through `after_review`: a derived `accept` with no unresolved deterministic failure publishes
  `accepted` (L3561), anything else is revised while fewer than 2 revisions ran and publishes `contested`
  after the second (L3562-3563, §11.9 item 4).

**The bounds** (L3567): at most 7 logical calls per attempt, 1 generate, at most 2 revisions
(`MAX_REVISIONS`), at most 3 critic rounds (`MAX_CRITIC_ROUNDS`) and one structural repair shared by every
step; at most 6 HTTP tries per logical call (§10.4), so at most 42. The decisions alone keep them: a revision
follows only a critic round with `round < 2`, a critic round only a validation with `critic_rounds < 3`, and
a repair only an unused repair.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Literal

from jobhunter.llm.gateway import INVALID_OUTPUT

Route = Literal["load_context", "generate", "validate", "critic", "revise", "publish", "fail", "__end__"]
PublishMode = Literal["accepted", "contested"]
DerivedOutcome = Literal["accept", "revise", "incomplete"]
Generation = Literal["generate", "revise"]

MAX_REVISIONS: Final = 2  # `round` is 0 for the generated draft, 1-2 for the revisions (§11.9 item 4)
MAX_CRITIC_ROUNDS: Final = 3
MAX_REPAIRS: Final = 1
MAX_LOGICAL_CALLS: Final = 1 + MAX_REVISIONS + MAX_CRITIC_ROUNDS + MAX_REPAIRS  # 7 (L3567)
TRIES_PER_LOGICAL_CALL: Final = 6  # 3 transport tries per stage, 2 stages (§10.4)
MAX_HTTP_CALLS: Final = MAX_LOGICAL_CALLS * TRIES_PER_LOGICAL_CALL  # 42
END: Final = "__end__"
# `load_context`'s outcomes that end the attempt there or publish what exists.
CLAIM_LOST: Final = "claim_lost"
ALREADY_PUBLISHED: Final = "already_published"


@dataclass(frozen=True)
class Next:
    """Where the attempt goes next: `publish` with its mode, `fail` with its error code, or a step; `repair`
    when the step is the attempt's one structural repair (the caller sets `repair_used`)."""

    route: Route
    publish_mode: PublishMode | None = None
    error_code: str | None = None
    repair: bool = False


def publish(mode: PublishMode) -> Next:
    return Next("publish", publish_mode=mode)


def fail(error_code: str) -> Next:
    return Next("fail", error_code=error_code)


def after_load_context(*, outcome: str | None, error_code: str | None, resumed: bool) -> Next:
    """The step after `load_context` (module docstring)."""
    if outcome == CLAIM_LOST:
        return Next(END)
    if outcome == ALREADY_PUBLISHED:
        return Next("publish")
    if error_code is not None:
        return fail(error_code)
    return Next("validate" if resumed else "generate")


def after_generation(step: Generation, *, stored: bool, repair_used: bool) -> Next:
    """The step after `generate` or `revise` stored a draft (`stored`) or met a structurally invalid output
    (module docstring); an error is `fail(code)`, which the caller decides itself."""
    if stored:
        return Next("validate")
    if not repair_used:
        return Next(step, repair=True)
    return fail(INVALID_OUTPUT) if step == "generate" else publish("contested")


def after_review(*, derived_outcome: DerivedOutcome, det_failures: Sequence[str], round: int) -> Next:
    """The step after a critic review of the current draft, live or restored (module docstring)."""
    if derived_outcome == "accept" and not det_failures:
        return publish("accepted")
    if round < MAX_REVISIONS:
        return Next("revise")
    return publish("contested")


def after_validate(
    *,
    triggers: Sequence[str],
    det_failures: Sequence[str],
    critic_rounds: int,
    round: int,
    review: DerivedOutcome | None,
) -> Next:
    """The step after `validate` (module docstring); `review` is the derived outcome of the current draft's
    critic review when it has one (a resumed attempt), else `None`."""
    if review is not None:
        return after_review(derived_outcome=review, det_failures=det_failures, round=round)
    if not triggers and not det_failures and critic_rounds == 0:
        return publish("accepted")
    if critic_rounds < MAX_CRITIC_ROUNDS:
        return Next("critic")
    return publish("contested")


def after_critic(
    *, review: DerivedOutcome | None, det_failures: Sequence[str], round: int, repair_used: bool
) -> Next:
    """The step after `critic`: `review` is the round's derived outcome, `None` for a structurally invalid
    output (module docstring); an error is `fail(code)`, which the caller decides itself."""
    if review is None:
        return Next("critic", repair=True) if not repair_used else publish("contested")
    return after_review(derived_outcome=review, det_failures=det_failures, round=round)
