from dataclasses import replace
from datetime import timedelta

from jobhunter.dedup.group_rules import (
    G4_TITLE,
    MAX_MEMBERS,
    GroupView,
    choose_group,
    choose_partner,
    compatible_location,
    confirms,
    pair_evidence,
    revalidation_departures,
)
from jobhunter.dedup.representative import (
    ALL_FAILED,
    ALL_FILTERED,
    ELIGIBLE_TARGET_STATES,
    NO_ACTIVE_MEMBER,
    MemberView,
    elect,
    eligible,
    unscorable_reason,
)

# job(**overrides) → JobView with Berlin, {senior}, full text, company "acme"; the Location fixtures
from tests.support.groups import LEIPZIG, POTSDAM_24KM, UNRESOLVED, ZURICH, T, job, varied


def test_decision_table() -> None:
    # "Sr." normalised earlier: identical title_norm
    assert pair_evidence(job(id=1), job(id=2, title_norm="senior software engineer"), 1.0) is not None
    board = frozenset({"greenhouse:acme"})
    assert pair_evidence(job(id=1, ats_boards=board), job(id=2, ats_boards=board), 1.0) is None  # G1
    # Berlin vs Zurich: G2
    assert (
        pair_evidence(
            job(id=1, requisition_ids=frozenset({"R1"})),
            job(id=2, locations=(ZURICH,), requisition_ids=frozenset({"R1"})),
            1.0,
        )
        is None
    )
    # Adzuna re-ingests may be grouped
    assert pair_evidence(job(id=1, source_rank=4), job(id=2, source_rank=4), 1.0) is not None
    assert pair_evidence(job(id=1), job(id=2, seniority_tokens=frozenset({"junior"})), 1.0) is None  # G3
    assert (
        pair_evidence(job(id=1), job(id=2), 0.84) is None
        and pair_evidence(job(id=1), job(id=2), 0.85) is not None
    )  # G4
    assert (
        pair_evidence(job(id=1, locations=(UNRESOLVED,)), job(id=2), 1.0) is None
        and pair_evidence(
            job(id=1, location_kind="remote", locations=()),
            job(id=2, location_kind="remote", locations=()),
            1.0,
        )
        is not None
    )
    assert (
        pair_evidence(job(id=1, locations=(POTSDAM_24KM,)), job(id=2), 1.0) is not None
        and pair_evidence(job(id=1, locations=(LEIPZIG,)), job(id=2), 1.0) is None
    )


def test_the_evidence_names_its_rules() -> None:
    evidence = pair_evidence(
        job(id=1, requisition_ids=frozenset({"R1"})), job(id=2, requisition_ids=frozenset({"R1", "R2"})), 0.9
    )
    assert evidence is not None
    assert (evidence.title_similarity, evidence.text_score, evidence.location_rule) == (
        0.9,
        1.0,
        "shared_place",
    )
    assert evidence.requisition_match  # extra evidence only: it never replaces G1-G5
    near = pair_evidence(job(id=1, locations=(POTSDAM_24KM,)), job(id=2), 1.0)
    assert near is not None and near.location_rule == "within_25km"
    # a different ATS board, or one job without a board, is no G1 obstacle
    assert pair_evidence(job(id=1, ats_boards=frozenset({"greenhouse:acme"})), job(id=2), 1.0) is not None
    assert (
        pair_evidence(
            job(id=1, ats_boards=frozenset({"greenhouse:acme"})),
            job(id=2, ats_boards=frozenset({"lever:eu:acme"})),
            1.0,
        )
        is not None
    )
    assert pair_evidence(job(id=1), job(id=2, description_text=varied(60, 100)), 1.0) is None  # G5
    assert G4_TITLE == 0.85


def test_compatible_location() -> None:
    assert compatible_location(
        job(id=1, location_kind="country_wide", locations=()),
        job(id=2, location_kind="country_wide", locations=()),
    )
    assert not compatible_location(job(id=1, location_kind="remote", locations=()), job(id=2))
    # an unresolved location gives no evidence; a resolved one beside it still can
    assert compatible_location(
        job(id=1, location_kind="multi", locations=(UNRESOLVED, job().locations[0])), job(id=2)
    )
    assert not compatible_location(job(id=1, locations=(UNRESOLVED,)), job(id=2, locations=(UNRESOLVED,)))
    # every candidate pair within 25 km: one far candidate is enough to fail
    assert not compatible_location(
        job(id=1, location_kind="multi", locations=(POTSDAM_24KM, LEIPZIG)), job(id=2)
    )


def test_a_job_joins_the_qualifying_group_with_the_best_text_score() -> None:
    a = job(id=10)
    views = {
        1: job(id=1, completeness="partial", description_text=varied(30) + "…"),  # 26/26 of its text in A's
        2: job(id=2),
        3: job(id=3, completeness="partial", description_text=varied(40, 30) + "…"),  # 26 of its 36 in A's
        4: job(id=4),
    }
    similarity = dict.fromkeys(views, 1.0)
    first, second = GroupView(100, 1, (1, 2)), GroupView(200, 3, (3, 4))
    choice = choose_group(a, [second, first], views, different=frozenset(), similarity=similarity)
    assert choice is not None and (choice.group_id, choice.qualifying) == (100, (100, 200))
    assert choice.evidence.text_score == 1.0
    # a `different` decision against any member, or a member of A's ATS board, rules a group out (G1)
    choice = choose_group(a, [first, second], views, different=frozenset({2}), similarity=similarity)
    assert choice is not None and (choice.group_id, choice.qualifying) == (200, (200,))
    boards = {**views, 4: replace(views[4], ats_boards=frozenset({"greenhouse:acme"}))}
    on_board = replace(a, ats_boards=frozenset({"greenhouse:acme"}))
    choice = choose_group(on_board, [first, second], boards, different=frozenset(), similarity=similarity)
    assert choice is not None and choice.qualifying == (100,)
    # G2-G5 are checked against the representative only, and a full group takes no member
    far = {**views, 2: replace(views[2], locations=(LEIPZIG,))}
    choice = choose_group(a, [first], far, different=frozenset(), similarity=similarity)
    assert choice is not None and choice.group_id == 100
    full = GroupView(100, 21, tuple(range(21, 21 + MAX_MEMBERS)))
    crowd = {i: job(id=i) for i in full.members}
    assert choose_group(a, [full], crowd, different=frozenset(), similarity=dict.fromkeys(crowd, 1.0)) is None
    nine = GroupView(100, 21, full.members[:-1])  # nine members: there is room for one more
    assert (
        choose_group(a, [nine], crowd, different=frozenset(), similarity=dict.fromkeys(crowd, 1.0))
        is not None
    )
    # without a representative a group cannot be validated, so it is not joined
    assert (
        choose_group(a, [GroupView(100, None, (1, 2))], views, different=frozenset(), similarity=similarity)
        is None
    )


def test_a_partner_is_the_best_ungrouped_candidate() -> None:
    a = job(id=10)
    candidates = [
        job(id=3, completeness="partial", description_text=varied(40, 30) + "…"),
        job(id=5),
        job(id=4),
        job(id=6, seniority_tokens=frozenset()),
    ]
    similarity = {3: 1.0, 4: 0.9, 5: 1.0, 6: 1.0}
    partner = choose_partner(a, candidates, different=frozenset(), similarity=similarity)
    assert partner is not None and partner[0].id == 4  # the best text score, then the lowest id
    partner = choose_partner(a, candidates, different=frozenset({4, 5}), similarity=similarity)
    assert partner is not None and partner[0].id == 3
    assert choose_partner(a, candidates[3:], different=frozenset(), similarity=similarity) is None


def member(job_id: int, **overrides: object) -> MemberView:
    fields: dict[str, object] = {
        "job_id": job_id,
        "availability": "active",
        "completeness": "full",
        "source_rank": 2,
        "description_length": 1000,
        "first_seen_at": T,
        "eligible": True,
        **overrides,
    }
    return MemberView(**fields)  # type: ignore[arg-type]


def test_the_representative_order() -> None:
    later = T + timedelta(hours=1)
    assert elect([]) is None and elect([member(1, eligible=False)]) is None
    keys: list[tuple[dict[str, object], dict[str, object]]] = [
        ({"availability": "active"}, {"availability": "unknown"}),  # 1. availability
        ({"availability": "unknown"}, {"availability": "stale"}),
        ({"completeness": "full"}, {"completeness": "partial"}),  # 2. completeness
        ({"source_rank": 1}, {"source_rank": 4}),  # 3. source precedence
        ({"description_length": 2000}, {"description_length": 1999}),  # 4. longer text
        ({"first_seen_at": T}, {"first_seen_at": later}),  # 5. earliest first seen
    ]
    for better, worse in keys:  # the better one wins from either id
        assert elect([member(2, **worse), member(1, **better)]) == member(1, **better)
        assert elect([member(1, **worse), member(2, **better)]) == member(2, **better)
    assert elect([member(2), member(1)]) == member(1)  # 6. lowest job id
    assert elect(
        [member(1, availability="unknown", eligible=False), member(2, availability="stale")]
    ) == member(2, availability="stale")


def test_eligibility_reads_the_active_tuple_target() -> None:
    """§8.7 (L3251, M1 Deviation 9 closed): without an active tuple M1's rule (unmerged, not closed); with
    one, the member's active-tuple target must also be live or published: no target, or one that failed, was
    refused, expired, was filtered out or cancelled, is not eligible."""
    assert eligible("active", False, None, has_tuple=False) and eligible(
        "stale", False, None, has_tuple=False
    )
    assert not eligible("closed", False, None, has_tuple=False)
    assert not eligible("active", True, None, has_tuple=False)
    assert ELIGIBLE_TARGET_STATES == (
        "pending",
        "budget_deferred",
        "in_progress",
        "in_batch",
        "deferred_duplicate",
        "published_accepted",
        "published_contested",
    )
    for state in ELIGIBLE_TARGET_STATES:
        assert eligible("unknown", False, state, has_tuple=True), state
        assert not eligible("closed", False, state, has_tuple=True), state
        assert not eligible("active", True, state, has_tuple=True), state
    assert not eligible("active", False, None, has_tuple=True)  # no target of the active tuple
    for state in ("failed_request", "failed_attempts_exhausted", "refused", "expired_unscored"):
        assert not eligible("active", False, state, has_tuple=True), state
    assert not eligible("active", False, "filtered_out", has_tuple=True)
    assert not eligible("active", False, "cancelled", has_tuple=True)


def test_the_unscorable_reason_of_a_group_without_an_eligible_member() -> None:
    """L3259: `all_failed` when every member's target failed, was refused or expired; `all_filtered` when
    every one was filtered out; `no_active_member` otherwise (a mix, a member without a target, no active
    tuple)."""
    failed = [
        member(1, eligible=False, target_state="failed_request"),
        member(2, eligible=False, target_state="refused"),
    ]
    assert (
        unscorable_reason([*failed, member(3, eligible=False, target_state="expired_unscored")]) == ALL_FAILED
    )
    assert unscorable_reason(failed[:1]) == ALL_FAILED
    filtered = [
        member(1, eligible=False, target_state="filtered_out"),
        member(2, eligible=False, target_state="filtered_out"),
    ]
    assert unscorable_reason(filtered) == ALL_FILTERED
    assert unscorable_reason([failed[0], filtered[1]]) == NO_ACTIVE_MEMBER  # a mix
    assert unscorable_reason([failed[0], member(2, eligible=False)]) == NO_ACTIVE_MEMBER  # one has no target
    closed = member(2, availability="closed", eligible=False, target_state="pending")
    assert unscorable_reason([failed[0], closed]) == NO_ACTIVE_MEMBER  # a closed job's live target
    assert unscorable_reason([member(1, eligible=False), member(2, eligible=False)]) == NO_ACTIVE_MEMBER
    assert unscorable_reason([]) == NO_ACTIVE_MEMBER


def test_a_revalidation_rejects_every_g1_pair_the_representative_first() -> None:
    """Final review I2: the revalidation against a representative checks G1 for every pair of members, not
    only G2-G5. A member on the representative's board leaves; of two members on one board, the one that
    joined later leaves (the members are given in the order they joined); a member failing G2-G5 leaves
    and takes no other member with it."""
    x, y = frozenset({"greenhouse:x"}), frozenset({"lever:eu:y"})
    rep = job(id=1, ats_boards=x)
    same_board_as_rep = job(id=2, ats_boards=x)
    fine = job(id=3)
    assert revalidation_departures(rep, [same_board_as_rep, fine], {2: 1.0, 3: 1.0}) == (2,)
    # G2-G5 alone accept the pair: only G1 rejects it
    assert confirms(same_board_as_rep, rep, 1.0) is not None
    assert pair_evidence(same_board_as_rep, rep, 1.0) is None and pair_evidence(fine, rep, 1.0) is not None
    first_on_y, later_on_y = job(id=4, ats_boards=y), job(id=5, ats_boards=y)
    assert revalidation_departures(job(id=1), [later_on_y, first_on_y], {4: 1.0, 5: 1.0}) == (4,)
    assert revalidation_departures(job(id=1), [first_on_y, later_on_y], {4: 1.0, 5: 1.0}) == (5,)
    # one that fails G4 leaves for it, and the member on its board stays
    failing = job(id=6, ats_boards=y)
    assert revalidation_departures(job(id=1), [failing, later_on_y], {6: 0.5, 5: 1.0}) == (6,)
    assert revalidation_departures(job(id=1), [fine], {3: 1.0}) == ()
