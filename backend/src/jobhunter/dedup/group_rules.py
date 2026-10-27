"""The pure duplicate-group rules (spec §8.7): G1-G5, the evidence of a pair, the revalidation against a
representative and the choice of a group or a partner (M1 Carry-forward 15: moved verbatim out of `groups`,
which imports them and keeps the reading, the locks and the writes).

A `JobView` is what they read of a job: its canonical columns, its current revision's description, its best
source precedence rank, the `<type>:<board>` of its `ats:` keys and the Greenhouse requisition ids of its
postings' latest observations. `pair_evidence(a, b, title_similarity)` applies:

- **G1**: two jobs that carry a key of one ATS board are never grouped (Adzuna re-ingests may be);
- **G2** (`compatible_location`): both `remote`; both `country_wide`; or a gazetteer candidate id the two
  share (`shared_place`); or every candidate pair within `G2_KM` (`within_25km`). An unresolved location
  gives no evidence, so a job with no resolved location is compatible only by the two kinds (an
  interpretation: a resolved location beside an unresolved one still counts);
- **G3**: identical seniority-token sets;
- **G4**: `similarity(title_norm)` (pg_trgm, computed by the caller) ≥ `G4_TITLE`;
- **G5**: `shingles.text_confirms` of the two descriptions and their completeness.

It returns the `GroupEvidence` (with a requisition id match as extra evidence, never in place of G1-G5), or
`None` when a rule fails. `confirms(a, b, …)` is G2-G5 alone.

**Formation** (`choose_group`, `choose_partner`): a job joins an active group whose representative it
confirms (G2-G5), against whose every member it passes G1 and has no `different` decision, and that has fewer
than `MAX_MEMBERS` members; a group without a representative cannot be validated and is not joined. Of
several, the one with the best text score, then the lowest id. Otherwise the ungrouped candidate with the
best pair evidence (text score, then lowest id) forms a new group with it.

**Revalidation** (`revalidation_departures`; final review I2) is G2-G5 of every member against the
representative, then G1 for every pair: the join rule replayed in the order the members joined, the
representative first, so a member stays only when it carries no board key of the representative or of a
member that stays before it. Without a representative, G1 alone.

`member_view(view)` is the election's view of a job (`representative.member_view`): its eligibility reads
the job's availability, whether it is merged and its active-tuple target (`target_state`, `has_tuple`: M3
Task 15, M1 Deviation 9 closed).
"""

from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from jobhunter.dedup import representative
from jobhunter.dedup.representative import MemberView
from jobhunter.dedup.shingles import TextSide, text_confirms
from jobhunter.geo.gazetteer import distance_km
from jobhunter.sources.parsing.locations import Candidate, Location
from jobhunter.types import JSONValue

G4_TITLE: Final = 0.85
MAX_MEMBERS: Final = 10
G2_KM: Final = 25.0
_DIGITS: Final = 4  # the precision of a score in a member's evidence


@dataclass(frozen=True)
class JobView:
    """What the grouping rules read of a job (module docstring)."""

    id: int
    company_key: str
    title_norm: str
    seniority_tokens: frozenset[str]
    locations: tuple[Location, ...]
    location_kind: str
    completeness: str
    description_text: str
    first_seen_at: datetime
    availability: str
    source_rank: int
    ats_boards: frozenset[str]  # `<type>:<board>` of its `ats:` keys
    requisition_ids: frozenset[str]
    merged: bool
    # The election's eligibility (`member_view`), not a rule: its active-tuple target's state and whether a
    # tuple is active (M3 Task 15).
    target_state: str | None = None
    has_tuple: bool = False


@dataclass(frozen=True)
class GroupEvidence:
    """Why a pair satisfies G1-G5: the title similarity, the G5 score, the G2 rule and a requisition match."""

    title_similarity: float
    text_score: float
    location_rule: str
    requisition_match: bool

    def as_json(self, against_job_id: int, *, ambiguous: Sequence[int] = ()) -> dict[str, JSONValue]:
        """The membership's `evidence`: these values, validated against `against_job_id`."""
        evidence: dict[str, JSONValue] = {
            "against_job_id": against_job_id,
            "title_similarity": round(self.title_similarity, _DIGITS),
            "text_score": round(self.text_score, _DIGITS),
            "location_rule": self.location_rule,
            "requisition_match": self.requisition_match,
        }
        if ambiguous:
            evidence["ambiguous_group_ids"] = list[JSONValue](ambiguous)
        return evidence


def _places(view: JobView) -> list[Candidate]:
    return [
        c for location in view.locations if location.resolution != "unresolved" for c in location.candidates
    ]


def location_rule(a: JobView, b: JobView) -> str | None:
    """The G2 rule the pair satisfies (module docstring), or `None`."""
    if a.location_kind == b.location_kind == "remote":
        return "both_remote"
    if a.location_kind == b.location_kind == "country_wide":
        return "both_country_wide"
    places_a, places_b = _places(a), _places(b)
    if not places_a or not places_b:
        return None
    if {c.gazetteer_id for c in places_a} & {c.gazetteer_id for c in places_b}:
        return "shared_place"
    if all(distance_km(x.lat, x.lon, y.lat, y.lon) <= G2_KM for x in places_a for y in places_b):
        return "within_25km"
    return None


def compatible_location(a: JobView, b: JobView) -> bool:
    """G2: the two jobs' locations are compatible."""
    return location_rule(a, b) is not None


def same_board(a: JobView, b: JobView) -> bool:
    """G1 fails: both jobs carry a key of one ATS board."""
    return bool(a.ats_boards & b.ats_boards)


def _side(view: JobView) -> TextSide:
    return TextSide(view.description_text, "full" if view.completeness == "full" else "partial")


def confirms(a: JobView, b: JobView, title_similarity: float) -> GroupEvidence | None:
    """G2-G5 of the pair (the test against a representative); `None` when one fails."""
    rule = location_rule(a, b)
    if rule is None or a.seniority_tokens != b.seniority_tokens or title_similarity < G4_TITLE:
        return None
    confirmed, score = text_confirms(_side(a), _side(b))
    if not confirmed:
        return None
    return GroupEvidence(title_similarity, score, rule, bool(a.requisition_ids & b.requisition_ids))


def pair_evidence(a: JobView, b: JobView, title_similarity: float) -> GroupEvidence | None:
    """G1-G5 of the pair (module docstring); `None` when one fails."""
    return None if same_board(a, b) else confirms(a, b, title_similarity)


def revalidation_departures(
    rep: JobView | None, others: Sequence[JobView], similarity: Mapping[int, float]
) -> tuple[int, ...]:
    """The members that leave a revalidation (module docstring): of `others` (every member but the
    representative `rep`, in the order they joined; `similarity` of each title to `rep`'s), those that fail
    G2-G5 against `rep`, then those that fail G1 against `rep` or a member that stays before them. Without a
    representative, G1 alone."""
    confirmed = [
        view for view in others if rep is None or confirms(view, rep, similarity[view.id]) is not None
    ]
    staying: list[JobView] = [] if rep is None else [rep]
    on_a_board: set[int] = set()
    for view in confirmed:
        if any(same_board(view, kept) for kept in staying):
            on_a_board.add(view.id)
        else:
            staying.append(view)
    confirmed_ids = {view.id for view in confirmed}
    return tuple(view.id for view in others if view.id not in confirmed_ids or view.id in on_a_board)


@dataclass(frozen=True)
class GroupView:
    """An active group as formation reads it: its representative (or none) and its members."""

    group_id: int
    representative: int | None
    members: tuple[int, ...]


@dataclass(frozen=True)
class Choice:
    """The group a job joins, its evidence against the representative, and every group it qualified for
    (the chosen one first)."""

    group_id: int
    evidence: GroupEvidence
    qualifying: tuple[int, ...]


def join_evidence(
    a: JobView,
    group: GroupView,
    views: Mapping[int, JobView],
    *,
    different: Collection[int],
    similarity: Mapping[int, float],
) -> GroupEvidence | None:
    """Whether `a` may be in `group` (module docstring): G1 and no `different` decision against every other
    member, fewer than `MAX_MEMBERS` others, and G2-G5 against the representative; its evidence or `None`."""
    others = [member for member in group.members if member != a.id]
    if group.representative is None or group.representative == a.id or len(others) >= MAX_MEMBERS:
        return None
    if any(member in different or same_board(a, views[member]) for member in others):
        return None
    return confirms(a, views[group.representative], similarity[group.representative])


def choose_group(
    a: JobView,
    groups: Iterable[GroupView],
    views: Mapping[int, JobView],
    *,
    different: Collection[int],
    similarity: Mapping[int, float],
) -> Choice | None:
    """The group `a` joins: of those it qualifies for, the best text score, then the lowest id."""
    qualifying = [
        (evidence, group.group_id)
        for group in groups
        if (evidence := join_evidence(a, group, views, different=different, similarity=similarity))
        is not None
    ]
    if not qualifying:
        return None
    qualifying.sort(key=lambda found: (-found[0].text_score, found[1]))
    return Choice(qualifying[0][1], qualifying[0][0], tuple(group_id for _, group_id in qualifying))


def choose_partner(
    a: JobView,
    candidates: Iterable[JobView],
    *,
    different: Collection[int],
    similarity: Mapping[int, float],
) -> tuple[JobView, GroupEvidence] | None:
    """The ungrouped candidate `a` forms a new group with: G1-G5, no `different` decision, the best text
    score, then the lowest id."""
    found = [
        (evidence, candidate)
        for candidate in candidates
        if candidate.id != a.id
        and candidate.id not in different
        and (evidence := pair_evidence(a, candidate, similarity[candidate.id])) is not None
    ]
    if not found:
        return None
    evidence, partner = min(found, key=lambda pair: (-pair[0].text_score, pair[1].id))
    return partner, evidence


def member_view(view: JobView) -> MemberView:
    """The election's view of a job (`representative.member_view`)."""
    return representative.member_view(
        job_id=view.id,
        availability=view.availability,
        completeness=view.completeness,
        source_rank=view.source_rank,
        description_length=len(view.description_text),
        first_seen_at=view.first_seen_at,
        merged=view.merged,
        target_state=view.target_state,
        has_tuple=view.has_tuple,
    )
