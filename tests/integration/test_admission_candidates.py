"""The admission on PostgreSQL (spec §9.6.2 L3605-3662, §7.11 L2219-2222; plan Task 12, Codex r1 #14, #21, r4
B2, Rulings P1, P12, P35, Deviation 24, Controller carry T11-d): the lane contract pin, the candidate queries,
the persistent lane credits, `admit` and `admit_urgent`.

- `test_lane_values_pin_both_checks`: both lane CHECKs of migration 0008 hold exactly `LANE_VALUES` (P1).
- The similarity lane excludes a `partial` revision and a non-representative member of an active group (a
  dissolved group's member is not excluded); oldest orders by creation and exploration by `md5(cycle:id)`,
  stable for one cycle id and different for another; null-similarity targets are oldest, exploration and
  always-include candidates.
- The four always-include reasons are found and ordered by similarity, NULL last; a follow-up's reason is
  `batch_followup` whatever else it qualifies for.
- `LaneCredits` creates its three rows at 0, locks them `FOR UPDATE`, round-trips fractions within 4 decimals
  (half-even) and is refused below -1.
- An exhausted (`charged_attempts = 4`), backed-off or purge-fenced target is in no candidate set and no round
  admits it.
- `admit` runs the four sweeps first, in order, then picks Score now first and fills `S` by the lanes, saves
  the debited credits, writes `admitted` under the cycle owner (a stale owner is fenced out and writes
  nothing), bumps the ranking when a sweep moved a target, and the claim records each admission's lane and
  always-include reason.
- `admit_urgent` returns every Score now target and at most `slots_left` follow-ups (lane `batch_followup`),
  touches no credit and adds to `admitted`.

Every datum is invented.
"""

import dataclasses
import hashlib
import re
from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from fractions import Fraction
from typing import Final

import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.db.sql import CHECK_VIOLATION, LOCK_NOT_AVAILABLE, pg_error
from jobhunter.errors import FencedOut
from jobhunter.graphs.score.nodes import claim as claim_node
from jobhunter.scoring import admission, cycles
from jobhunter.scoring.admission import (
    LANE_VALUES,
    CandidateSets,
    CreditedLane,
    LaneCredits,
    Picked,
    Sweeps,
    candidates,
)
from jobhunter.scoring.cycles import CycleRow
from jobhunter.sources.instances import instance_for_company
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from tests.support.m3 import active_ids, insert_cycle, insert_id
from tests.support.scoring import (
    PendingTarget,
    TargetSpec,
    candidate_targets,
    lane_credits,
    list_on_watchlist,
)

pytestmark = pytest.mark.integration

MATCHED_TITLE: Final = "Backend Engineer (m/w/d)"  # `score_profile` confirms "Backend Engineer"
_LANE_CHECK: Final = sa.text(
    "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid = CAST(:t AS regclass) "
    "AND contype = 'c' AND pg_get_constraintdef(oid) LIKE '%(lane = ANY%'"
)
_GROUP_MEMBER: Final = sa.text(
    "INSERT INTO duplicate_group_members (group_id, job_id, evidence) VALUES (:g, :j, '{}')"
)
_IN_GROUP: Final = sa.text("UPDATE jobs SET duplicate_group_id = :g WHERE id = :j")
_POSTING: Final = sa.text(
    "INSERT INTO postings (source_instance_id, native_id, job_id, first_seen_at, last_seen_at, availability, "
    "availability_changed_at) VALUES (:instance, :native, :job, now(), now(), 'active', now())"
)
_FENCE: Final = sa.text(
    "INSERT INTO purge_fences (kind, job_ids, manifest_id) VALUES ('jobs', CAST(:jobs AS bigint[]), 1)"
)
_PROFILE_VERSION: Final = sa.text("SELECT profile_version_id FROM llm_tuples WHERE id = :t")
_CYCLE_SIZE: Final = sa.text(
    "UPDATE score_cycles SET admission_size = :size, admitted = CAST(:admitted AS jsonb) WHERE id = :c"
)
_ADMITTED: Final = sa.text("SELECT admitted FROM score_cycles WHERE id = :c")
_GENERATION: Final = sa.text("SELECT generation FROM ranking_state WHERE id = 1")
_CREDITS_NOWAIT: Final = sa.text(
    "SELECT 1 FROM admission_lane_credits WHERE llm_tuple_id = :t ORDER BY lane FOR UPDATE NOWAIT"
)
_CLAIMED: Final = sa.text(
    "SELECT t.id, t.lane, t.always_include_reason, a.lane AS attempt_lane FROM score_targets t "
    "JOIN score_attempts a ON a.target_id = t.id WHERE t.id = ANY(CAST(:ids AS bigint[]))"
)


async def scalar(engine: AsyncEngine, statement: sa.TextClause, **params: object) -> object:
    async with engine.connect() as conn:
        return (await conn.execute(statement, params)).scalar_one()


async def generation(engine: AsyncEngine) -> int:
    """The ranking generation (`ranking_state`)."""
    return int(str(await scalar(engine, _GENERATION)))


async def group(
    engine: AsyncEngine,
    representative: PendingTarget | None,
    *members: PendingTarget,
    status: str = "active",
) -> int:
    """A duplicate group of `representative` (none: unscorable) and `members`, as dedup writes one."""
    async with engine.begin() as conn:
        group_id = await insert_id(
            conn,
            "duplicate_groups",
            {
                "representative_job_id": None if representative is None else representative.job_id,
                "status": status,
                "unscorable_reason": "all_failed" if representative is None else None,
                "election_reason": "initial",
            },
        )
        for member in (*([] if representative is None else [representative]), *members):
            await conn.execute(_GROUP_MEMBER, {"g": group_id, "j": member.job_id})
            await conn.execute(_IN_GROUP, {"g": group_id, "j": member.job_id})
    return group_id


async def on_watchlist(
    engine: AsyncEngine, *targets: PendingTarget, enabled: bool = True, listed: bool | None = True
) -> None:
    """Each target's job gets a posting from the board instance of a new watchlist company (`enabled` in
    `watchlist_companies`), listed in the active settings' `watchlist[]` as `listed` (`None`: not listed)."""
    async with engine.begin() as conn:
        companies: dict[int, bool] = {}
        for n, target in enumerate(targets):
            company = await insert_id(
                conn,
                "watchlist_companies",
                {
                    "display_name": f"Beispiel {n}",
                    "company_key": f"beispiel {n}",
                    "ats_type": "greenhouse",
                    "board_slug": f"beispiel-{target.target_id}",
                    "detection": "manual",
                    "enabled": enabled,
                },
            )
            instance = await instance_for_company(conn, company)
            await conn.execute(_POSTING, {"instance": instance, "native": f"n{n}", "job": target.job_id})
            if listed is not None:
                companies[company] = listed
        await list_on_watchlist(conn, companies)


async def candidate_sets(
    engine: AsyncEngine,
    tuple_id: int,
    *,
    cycle_id: int = 1,
    now: datetime | None = None,
    limit: int | None = None,
    always_limit: int | None = None,
) -> CandidateSets:
    async with engine.connect() as conn:
        profile_version = int((await conn.execute(_PROFILE_VERSION, {"t": tuple_id})).scalar_one())
        return await candidates(
            conn,
            llm_tuple_id=tuple_id,
            cycle_id=cycle_id,
            profile_version_id=profile_version,
            now=utcnow() if now is None else now,
            limit=limit,
            always_limit=always_limit,
        )


def ids(targets: Sequence[PendingTarget]) -> list[int]:
    return [t.target_id for t in targets]


async def a_cycle(engine: AsyncEngine, tuple_id: int, *, size: int, admitted: str = "{}") -> int:
    async with engine.begin() as conn:
        cycle_id = await insert_cycle(conn, tuple_id)
        await conn.execute(_CYCLE_SIZE, {"size": size, "admitted": admitted, "c": cycle_id})
    return cycle_id


async def owned_cycle(conn: AsyncConnection, cycle_id: int) -> CycleRow:
    """The cycle as the `admit` node holds it: `active_config FOR SHARE`, then the owner row (epoch 1)."""
    await cycles.lock_active_scoring(conn)
    cycle = await cycles.lock_cycle(conn, cycle_id, epoch=1)
    assert cycle is not None
    return cycle


class RecordingSweeps:
    """Four sweeps that record their calls; `expiry` runs `on_expiry` and reports `moved` targets."""

    def __init__(self, on_expiry: sa.TextClause | None = None, moved: int = 0) -> None:
        self.calls: list[tuple[str, int, datetime]] = []
        self._on_expiry = on_expiry
        self._moved = moved

    def sweep(self, name: str) -> admission.Sweep:
        async def run(conn: AsyncConnection, cycle: CycleRow, /, *, now: datetime) -> int:
            self.calls.append((name, cycle.id, now))
            if name == "expiry" and self._on_expiry is not None:
                await conn.execute(self._on_expiry)
                return self._moved
            return 0

        return run

    @property
    def sweeps(self) -> Sweeps:
        return Sweeps(
            expiry=self.sweep("expiry"),
            refilter=self.sweep("refilter"),
            regroup=self.sweep("regroup"),
            exhaustion=self.sweep("exhaustion"),
        )


# ---- the lane contract (Ruling P1, Codex r4 B2) -------------------------------------------------------


async def test_lane_values_pin_both_checks(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        for table in ("score_targets", "score_attempts"):
            definition = (await conn.execute(_LANE_CHECK, {"t": table})).scalar_one()
            members = re.findall(r"'([^']*)'::text", definition)
            assert set(members) == set(LANE_VALUES), table
            assert len(members) == len(LANE_VALUES) == len(set(LANE_VALUES)) == 7


# ---- the candidate queries --------------------------------------------------------------------------------


async def test_the_similarity_lane_excludes_a_partial_revision_and_a_non_representative_member(
    engine: AsyncEngine,
) -> None:
    a, partial, member, rep, unembedded, dissolved, unscorable = await candidate_targets(
        engine,
        TargetSpec(similarity=0.9),
        TargetSpec(similarity=0.85, completeness="partial"),
        TargetSpec(similarity=0.8),
        TargetSpec(similarity=0.7),
        TargetSpec(similarity=None),
        TargetSpec(similarity=0.6),
        TargetSpec(similarity=0.5),
    )
    await group(engine, rep, member)
    await group(engine, None, dissolved, status="dissolved")
    await group(engine, None, unscorable)  # an active group without a representative: every member is one
    sets = await candidate_sets(engine, a.llm_tuple_id)
    assert sets.similarity == ids([a, rep, dissolved])
    everyone = ids([a, partial, member, rep, unembedded, dissolved, unscorable])
    assert sets.oldest == everyone  # one creation time: by id
    assert sorted(sets.exploration) == everyone  # null similarity included, as in oldest
    assert sets.always_include == []


async def test_oldest_orders_by_creation_and_exploration_by_the_cycle_seed(engine: AsyncEngine) -> None:
    start = utcnow() - timedelta(hours=1)
    minutes = (3, 1, 4, 0, 6, 2, 7, 5)
    made = await candidate_targets(
        engine, *(TargetSpec(created_at=start + timedelta(minutes=m)) for m in minutes)
    )
    tuple_id = made[0].llm_tuple_id
    by_creation = [made[i].target_id for i in sorted(range(len(made)), key=lambda i: minutes[i])]
    assert by_creation != ids(made)
    assert (await candidate_sets(engine, tuple_id)).oldest == by_creation

    def seeded(cycle_id: int) -> list[int]:
        return sorted(ids(made), key=lambda t: hashlib.md5(f"{cycle_id}:{t}".encode()).hexdigest())  # noqa: S324

    first = (await candidate_sets(engine, tuple_id, cycle_id=101)).exploration
    assert first == seeded(101)
    assert (await candidate_sets(engine, tuple_id, cycle_id=101)).exploration == first  # reproducible
    other = (await candidate_sets(engine, tuple_id, cycle_id=102)).exploration
    assert other == seeded(102) and other != first


async def test_the_four_always_include_reasons_are_found_and_ordered_by_similarity_null_last(
    engine: AsyncEngine,
) -> None:
    (
        followup,
        elected_twin,
        followup_on_watchlist,
        watchlist,
        twin,
        title,
        plain,
        partial_rep,
        partial_member,
    ) = await candidate_targets(
        engine,
        TargetSpec(similarity=0.8, lane="batch_followup"),
        TargetSpec(similarity=0.6),
        TargetSpec(similarity=0.5, lane="batch_followup"),
        TargetSpec(similarity=0.4),
        TargetSpec(similarity=0.2),
        TargetSpec(similarity=None, title=MATCHED_TITLE),
        TargetSpec(similarity=0.95),
        TargetSpec(similarity=0.3, completeness="partial"),
        TargetSpec(similarity=0.7, completeness="partial"),
    )
    await group(engine, partial_rep, twin)  # a full member of a group whose representative is partial
    await group(engine, elected_twin, partial_member)  # the full twin after §8.7's re-election
    await on_watchlist(engine, watchlist, followup_on_watchlist)
    sets = await candidate_sets(engine, followup.llm_tuple_id)
    assert sets.always_include == [
        (followup.target_id, "batch_followup"),
        (elected_twin.target_id, "full_text_twin"),
        (followup_on_watchlist.target_id, "batch_followup"),
        (watchlist.target_id, "watchlist"),
        (twin.target_id, "full_text_twin"),
        (title.target_id, "title_match"),
    ]
    assert plain.target_id in sets.similarity and title.target_id in sets.oldest


async def test_only_an_enabled_watchlist_company_is_watched(engine: AsyncEngine) -> None:
    # Controller Ruling T12-b: enabled in `watchlist_companies` and in the active settings' `watchlist[]`
    watched, disabled, disabled_in_settings, not_listed = await candidate_targets(
        engine, *(TargetSpec(similarity=0.5) for _ in range(4))
    )
    await on_watchlist(engine, watched)
    await on_watchlist(engine, disabled, enabled=False)
    await on_watchlist(engine, disabled_in_settings, listed=False)
    await on_watchlist(engine, not_listed, listed=None)
    sets = await candidate_sets(engine, watched.llm_tuple_id)
    assert sets.always_include == [(watched.target_id, "watchlist")]
    async with engine.begin() as conn:  # the user disables the watched company in the settings
        company = (
            await conn.execute(
                sa.text(
                    "SELECT s.watchlist_company_id FROM postings p JOIN source_instances s "
                    "ON s.id = p.source_instance_id WHERE p.job_id = :j"
                ),
                {"j": watched.job_id},
            )
        ).scalar_one()
        await list_on_watchlist(conn, {int(company): False})
    assert (await candidate_sets(engine, watched.llm_tuple_id)).always_include == []


async def test_the_always_include_read_stops_at_its_limit_and_the_lane_lists_flag_the_followups(
    engine: AsyncEngine,
) -> None:
    deep, top, second, plain = await candidate_targets(
        engine,
        TargetSpec(lane="batch_followup", similarity=0.1),
        TargetSpec(similarity=0.9, title=MATCHED_TITLE),
        TargetSpec(similarity=0.8, title=MATCHED_TITLE),
        TargetSpec(similarity=0.7),
    )
    sets = await candidate_sets(engine, deep.llm_tuple_id, limit=2, always_limit=2)
    assert sets.always_include == [(top.target_id, "title_match"), (second.target_id, "title_match")]
    assert sets.oldest == ids([deep, top])
    assert sets.followups == {deep.target_id}  # flagged by the oldest list it is in
    whole = await candidate_sets(engine, deep.llm_tuple_id)
    assert whole.always_include[-1] == (deep.target_id, "batch_followup")
    assert whole.followups == {deep.target_id} and plain.target_id in whole.similarity


async def test_an_exhausted_backed_off_or_fenced_target_is_in_no_candidate_set(engine: AsyncEngine) -> None:
    now = utcnow()
    every_lane = TargetSpec(similarity=0.9, title=MATCHED_TITLE, lane="batch_followup")  # in all four sets
    ok, exhausted, backed_off, fenced, now_ok, now_exhausted = await candidate_targets(
        engine,
        dataclasses.replace(every_lane, charged_attempts=3),
        dataclasses.replace(every_lane, charged_attempts=4),
        dataclasses.replace(every_lane, not_before=now + timedelta(hours=1)),
        every_lane,
        TargetSpec(lane="score_now"),
        TargetSpec(lane="score_now", charged_attempts=4),
    )
    async with engine.begin() as conn:
        await conn.execute(_FENCE, {"jobs": [fenced.job_id]})
    sets = await candidate_sets(engine, ok.llm_tuple_id, now=now)
    assert sets.similarity == [ok.target_id]
    assert sets.oldest == ids([ok, now_ok])
    assert sorted(sets.exploration) == ids([ok, now_ok])
    assert sets.always_include == [(ok.target_id, "batch_followup")]
    cycle_id = await a_cycle(engine, ok.llm_tuple_id, size=10)
    async with engine.begin() as conn:
        cycle = await owned_cycle(conn, cycle_id)
        urgent = await admission.admit_urgent(conn, cycle, now=now, slots_left=10)
    assert urgent == [
        Picked(now_ok.target_id, "score_now"),
        Picked(ok.target_id, "batch_followup", "batch_followup"),
    ]
    assert {exhausted.target_id, backed_off.target_id, fenced.target_id, now_exhausted.target_id}.isdisjoint(
        p.target_id for p in urgent
    )


# ---- the lane credits ------------------------------------------------------------------------------------


async def test_lane_credits_round_trip_fractions_within_four_decimals_and_refuse_below_minus_one(
    engine: AsyncEngine,
) -> None:
    (target,) = await candidate_targets(engine, TargetSpec())
    tuple_id = target.llm_tuple_id
    assert await lane_credits(engine, tuple_id) == {}
    async with engine.begin() as conn:
        credits = await LaneCredits.lock(conn, tuple_id)  # the three rows, created at 0
        assert credits.credits == {"similarity": 0, "oldest": 0, "exploration": 0}
        written: dict[CreditedLane, Fraction] = {
            "similarity": Fraction(1, 3),
            "oldest": Fraction(-1),
            "exploration": Fraction(1, 4000),
        }
        credits.credits = dict(written)
        await credits.save(conn)
    # numeric(10,4), half-even: 1/4000 = 0.00025 is stored 0.0002 (half-up would give 0.0003)
    assert await lane_credits(engine, tuple_id) == {
        "similarity": Decimal("0.3333"),
        "oldest": Decimal("-1.0000"),
        "exploration": Decimal("0.0002"),
    }
    async with engine.begin() as conn:
        again = await LaneCredits.lock(conn, tuple_id)
        async with engine.connect() as other:  # FOR UPDATE: another transaction cannot take them meanwhile
            with pytest.raises(sa.exc.OperationalError) as busy:
                await other.execute(_CREDITS_NOWAIT, {"t": tuple_id})
            assert pg_error(busy.value).sqlstate == LOCK_NOT_AVAILABLE
        assert again.credits == {
            "similarity": Fraction(3333, 10000),
            "oldest": Fraction(-1),
            "exploration": Fraction(2, 10000),
        }
        assert all(abs(again.credits[lane] - written[lane]) <= Fraction(1, 20000) for lane in written)
        again.credits["oldest"] = Fraction(-10001, 10000)
        with pytest.raises(sa.exc.IntegrityError) as refused:
            async with conn.begin_nested():
                await again.save(conn)
        assert pg_error(refused.value).sqlstate == CHECK_VIOLATION
    assert len(await lane_credits(engine, tuple_id)) == 3


# ---- admit and admit_urgent -------------------------------------------------------------------------------


async def test_admit_sweeps_first_fills_s_by_the_lanes_and_writes_admitted_under_the_cycle_owner(
    engine: AsyncEngine,
) -> None:
    now = utcnow()
    earlier, earliest = now - timedelta(hours=1), now - timedelta(hours=3)
    score_now, followup, watchlist, high, second, unembedded, swept = await candidate_targets(
        engine,
        TargetSpec(similarity=0.1, lane="score_now", created_at=earlier),
        TargetSpec(similarity=0.3, lane="batch_followup", created_at=earlier),
        TargetSpec(similarity=0.5, created_at=earlier),
        TargetSpec(similarity=0.9, created_at=earlier),
        TargetSpec(similarity=0.8, created_at=earlier),
        TargetSpec(similarity=None, created_at=earliest),
        TargetSpec(similarity=0.95, created_at=earlier),
    )
    await on_watchlist(engine, watchlist)
    tuple_id = score_now.llm_tuple_id
    cycle_id = await a_cycle(engine, tuple_id, size=4)
    backoff = sa.text(
        f"UPDATE score_targets SET not_before = now() + interval '1 hour' WHERE id = {swept.target_id}"  # noqa: S608
    )
    # a stale owner: the same cycle under an older epoch is fenced out, and its transaction leaves nothing
    with pytest.raises(FencedOut):
        async with engine.begin() as conn:
            stale = dataclasses.replace(await owned_cycle(conn, cycle_id), worker_epoch=0)
            await admission.admit(conn, stale, now=now, sweeps=RecordingSweeps().sweeps)
    assert await lane_credits(engine, tuple_id) == {}
    assert await scalar(engine, _ADMITTED, c=cycle_id) == {}

    before = await generation(engine)
    recording = RecordingSweeps(on_expiry=backoff, moved=1)
    async with engine.begin() as conn:
        cycle = await owned_cycle(conn, cycle_id)
        picked = await admission.admit(conn, cycle, now=now, sweeps=recording.sweeps)
    assert [name for name, _, _ in recording.calls] == ["expiry", "refilter", "regroup", "exhaustion"]
    assert {(c, at) for _, c, at in recording.calls} == {(cycle_id, now)}
    # S = 4 from zero credits: slots 3 / 1 / 0; always-include takes min(3, 4 // 2) = 2 of the similarity
    # slots; the swept target (backed off by the expiry sweep, before the candidates were read) is not
    # admitted
    assert picked == [
        Picked(score_now.target_id, "score_now"),
        Picked(watchlist.target_id, "always_include", "watchlist"),
        Picked(followup.target_id, "batch_followup", "batch_followup"),
        Picked(high.target_id, "similarity"),
        Picked(unembedded.target_id, "oldest"),
    ]
    assert second.target_id not in {p.target_id for p in picked}
    assert await scalar(engine, _ADMITTED, c=cycle_id) == {
        "score_now": 1,
        "always_include": 1,
        "batch_followup": 1,
        "similarity": 1,
        "oldest": 1,
    }
    # accrued 2.8 / 0.8 / 0.4, debited by the slot-backed takes 3 / 1 / 0
    assert await lane_credits(engine, tuple_id) == {
        "similarity": Decimal("-0.2000"),
        "oldest": Decimal("-0.2000"),
        "exploration": Decimal("0.4000"),
    }
    assert await generation(engine) == before + 1

    async with engine.begin() as conn:  # the claim records the lane and the reason (Deviation 24, T11-d)
        active = await active_ids(conn)
        claimed = await claim_node.claim_round(
            conn,
            picked,
            epoch=1,
            cycle_id=cycle_id,
            execution_config_id=active.execution_config_id,
            evaluation_policy_id=active.evaluation_policy_id,
        )
        assert len(claimed) == 5
        rows = await conn.execute(_CLAIMED, {"ids": [p.target_id for p in picked]})
        recorded = {int(r.id): (r.lane, r.always_include_reason, r.attempt_lane) for r in rows}
    assert recorded == {
        score_now.target_id: ("score_now", None, "score_now"),
        watchlist.target_id: ("always_include", "watchlist", "always_include"),
        followup.target_id: ("batch_followup", "batch_followup", "batch_followup"),
        high.target_id: ("similarity", None, "similarity"),
        unembedded.target_id: ("oldest", None, "oldest"),
    }


async def test_admit_without_a_sweep_move_leaves_the_ranking_alone(engine: AsyncEngine) -> None:
    (target,) = await candidate_targets(engine, TargetSpec(similarity=0.5))
    cycle_id = await a_cycle(engine, target.llm_tuple_id, size=1)
    before = await generation(engine)
    async with engine.begin() as conn:
        cycle = await owned_cycle(conn, cycle_id)
        picked = await admission.admit(conn, cycle, now=utcnow(), sweeps=admission.CYCLE_SWEEPS)
    assert picked == [Picked(target.target_id, "similarity")]
    assert await generation(engine) == before


async def test_admit_urgent_returns_every_score_now_and_at_most_slots_left_followups(
    engine: AsyncEngine,
) -> None:
    made = await candidate_targets(
        engine,
        TargetSpec(lane="score_now"),
        TargetSpec(lane="batch_followup", similarity=0.2),
        TargetSpec(lane="score_now"),
        TargetSpec(lane="batch_followup", similarity=None),
        TargetSpec(lane="batch_followup", similarity=0.7),
        TargetSpec(lane="score_now"),
        TargetSpec(lane="batch_followup", similarity=0.4),
        TargetSpec(similarity=0.99),  # an ordinary target: never a later round's
    )
    first_now, low, second_now, unembedded, high, third_now, middle, _ = made
    tuple_id = first_now.llm_tuple_id
    async with engine.begin() as conn:  # credits a first round left
        credits = await LaneCredits.lock(conn, tuple_id)
        credits.credits = {
            "similarity": Fraction(-1, 5),
            "oldest": Fraction(3, 10),
            "exploration": Fraction(0),
        }
        await credits.save(conn)
    before = await lane_credits(engine, tuple_id)
    cycle_id = await a_cycle(engine, tuple_id, size=4, admitted='{"oldest": 1, "score_now": 2}')
    ranked = await generation(engine)
    async with engine.begin() as conn:
        cycle = await owned_cycle(conn, cycle_id)
        picked = await admission.admit_urgent(conn, cycle, now=utcnow(), slots_left=2)
    assert picked == [
        Picked(first_now.target_id, "score_now"),
        Picked(second_now.target_id, "score_now"),
        Picked(third_now.target_id, "score_now"),
        Picked(high.target_id, "batch_followup", "batch_followup"),
        Picked(middle.target_id, "batch_followup", "batch_followup"),
    ]
    assert await scalar(engine, _ADMITTED, c=cycle_id) == {"oldest": 1, "score_now": 5, "batch_followup": 2}
    assert await lane_credits(engine, tuple_id) == before
    assert await generation(engine) == ranked
    async with engine.begin() as conn:  # no slot left: Score now only, NULL similarity last otherwise
        cycle = await owned_cycle(conn, cycle_id)
        assert [p.lane for p in await admission.admit_urgent(conn, cycle, now=utcnow(), slots_left=0)] == [
            "score_now"
        ] * 3
        everything = await admission.admit_urgent(conn, cycle, now=utcnow(), slots_left=10)
    assert [p.target_id for p in everything][3:] == ids([high, middle, low, unembedded])


async def test_a_followup_past_the_lane_lists_cut_keeps_its_lane(engine: AsyncEngine) -> None:
    # S = 2: slots 1 / 1 / 0, always-include takes 1, and `admit` reads len(score_now) + S = 2 ids of a lane;
    # the follow-up is third in always-include (NULL similarity last) and first in oldest
    now = utcnow()
    first, second, followup = await candidate_targets(
        engine,
        TargetSpec(similarity=0.9, title=MATCHED_TITLE),
        TargetSpec(similarity=0.8, title=MATCHED_TITLE),
        TargetSpec(lane="batch_followup", created_at=now - timedelta(hours=2)),
    )
    cycle_id = await a_cycle(engine, first.llm_tuple_id, size=2)
    async with engine.begin() as conn:
        cycle = await owned_cycle(conn, cycle_id)
        picked = await admission.admit(conn, cycle, now=now, sweeps=admission.CYCLE_SWEEPS)
    assert picked == [
        Picked(first.target_id, "always_include", "title_match"),
        Picked(followup.target_id, "batch_followup", "batch_followup"),
    ]
    assert second.target_id not in {p.target_id for p in picked}
