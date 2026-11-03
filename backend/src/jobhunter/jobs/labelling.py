"""The week-1 labelling queue (spec §15.6, §11.13; plan M4 Task 9, B16).

`labelling_queue(conn, *, limit)` reads, in the caller's read snapshot, up to `limit` ads for the user to
label, stratified over the score bands `BANDS`, and the progress towards `LABEL_TARGET` labels.

**Candidates** are the live jobs (not merged, not synthetic, with a current revision, not fenced by a purge:
`is_fenced(job_id)`, as `jobs.detail.ensure_job` checks it) whose **own** current-revision target of the
active tuple is published (`published_accepted` or `published_contested`; a deferred member, which its
representative's result scores, is none) with a current instance under the active scope that has a score
(`det_results.score_mid` not NULL), and whose result is not a verified not-a-job-ad
(`llm_results.not_a_job_ad` false; such a result's instance usually has no score anyway). A candidate is
**labelled** when a golden case exists for its current revision and the active profile, active or not and of
any source: the pair is unique, so a new label upserts that case.

**The bands** are `[75, 100]`, `[60, 75)`, `[40, 60)` and `[0, 40)` of `score_mid`. Each band is one read
(`/* labelling.band */`) over `det_rank_mid`, the scope and the score range in its index condition: the
band's candidates, the unlabelled ones first, each part by job id descending, at most `limit` of them. No
index has that order, so the read takes the whole band and sorts out its first `limit`. That is enough for
both phases of the merge (`queue_order`):
- the unlabelled candidates are taken round-robin over the bands from the top band (band 1's first, band 2's
  first, …, then band 1's second, …), an exhausted band skipped, within a band by job id descending;
- only when every band's unlabelled candidates ran out before `limit`, the labelled ones fill the rest, taken
  the same way.

**The items** (`/* labelling.items */`) are the chosen jobs in that order, each with its title, company,
location labels (`jobs.groups.location_labels`), ad language and the first `EXCERPT_CHARS` code points of its
current revision's description (`""` for an empty one). No item carries a score or a band: the user labels
without an anchor. **The progress** (`/* labelling.progress */`) counts the active `week1` golden cases of
the active profile, against `LABEL_TARGET`.

Without an active profile (and so without an active tuple) or without the `active_config` row, the queue is
empty and the progress `0 / LABEL_TARGET`. Every statement is static SQL, every value a bound parameter.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.db.sql import q
from jobhunter.jobs.groups import location_labels

# Each band is `[low, high)`, except the first, `[75, 100]` (B16)
BANDS: Final = ((75, 100), (60, 75), (40, 60), (0, 40))
LABEL_TARGET: Final = 50  # §15.6
EXCERPT_CHARS: Final = 600

_ACTIVE: Final = q(
    "/* labelling.active */ SELECT a.llm_tuple_id, a.scope_version_id, a.profile_version_id "
    "FROM active_config a WHERE a.id = 1"
)
# A band's candidates (module docstring), the unlabelled first, each part by job id descending. Two fences
# (`OFFSET 0`) keep the plan on the band (the shape of `jobs._sql`, Plans at scale): the band's current
# instances are a set of their own, read by the scope and score range of `det_rank_mid`, never along a whole
# index that a merge join would rather walk; and each one's job is a lookup of its own, so the redundant
# `current_revision_id` check is no join selectivity that would multiply the band's estimate down to one row.
_BAND_HEAD: Final = (
    "/* labelling.band */ SELECT b.job_id, EXISTS (SELECT 1 FROM golden_cases g "
    "WHERE g.job_revision_id = j.current_revision_id AND g.profile_version_id = :profile) AS labelled "
    "FROM (SELECT d.job_id, d.llm_result_id FROM det_results d "
    "WHERE d.scope_version_id = :scope AND d.is_current AND d.score_mid >= CAST(:low AS numeric)"
)
_BAND_TAIL: Final = (
    " OFFSET 0) b"
    " JOIN llm_results r ON r.id = b.llm_result_id"
    " JOIN score_targets t ON t.published_result_id = r.id AND t.llm_tuple_id = :tuple"
    " CROSS JOIN LATERAL (SELECT cj.id, cj.current_revision_id, cj.merged_into_job_id, cj.synthetic_eval"
    " FROM jobs cj WHERE cj.id = b.job_id AND cj.current_revision_id = t.job_revision_id OFFSET 0) j"
    " WHERE t.state IN ('published_accepted','published_contested') AND NOT r.not_a_job_ad"
    " AND j.merged_into_job_id IS NULL AND NOT j.synthetic_eval AND NOT is_fenced(j.id)"
    " ORDER BY labelled, b.job_id DESC LIMIT :limit"
)
_TOP_BAND: Final = q(_BAND_HEAD + " AND d.score_mid <= CAST(:high AS numeric)" + _BAND_TAIL)
_LOWER_BAND: Final = q(_BAND_HEAD + " AND d.score_mid < CAST(:high AS numeric)" + _BAND_TAIL)
_ITEMS: Final = q(
    "/* labelling.items */ SELECT j.id AS job_id, j.title, j.company_display, j.locations, j.ad_language, "
    "left(v.description_text, CAST(:chars AS integer)) AS excerpt "
    "FROM jobs j JOIN job_revisions v ON v.id = j.current_revision_id "
    "WHERE j.id = ANY(CAST(:ids AS bigint[]))"
)
_PROGRESS: Final = q(
    "/* labelling.progress */ SELECT count(*) FROM golden_cases g "
    "WHERE g.profile_version_id = :profile AND g.source = 'week1' AND g.active"
)


class LabelItemOut(BaseModel):
    """An ad to label: its job, title, company, location labels, ad language and the first 600 characters
    (code points) of its description. It carries no score."""

    job_id: int
    title: str
    company: str
    locations: list[str]
    ad_language: str
    excerpt: str


class LabelProgressOut(BaseModel):
    """The active week-1 golden cases of the active profile, and the number to reach."""

    labelled: int
    target: int


class LabellingQueueOut(BaseModel):
    """The ads to label next, spread over the score bands, those without a label for the active profile
    first, and the labelling progress. Neither carries a score or a band."""

    items: list[LabelItemOut]
    progress: LabelProgressOut


@dataclass(frozen=True)
class Candidate:
    """A candidate of a band and whether it is labelled (module docstring)."""

    job_id: int
    labelled: bool


def _round_robin(bands: Sequence[Sequence[int]], limit: int) -> list[int]:
    """Up to `limit` ids, the first of each band in band order, then the second of each, …; an exhausted
    band is skipped."""
    taken: list[int] = []
    depth = 0
    while len(taken) < limit:
        row = [band[depth] for band in bands if depth < len(band)]
        if not row:
            break
        taken.extend(row[: limit - len(taken)])
        depth += 1
    return taken


def queue_order(bands: Sequence[Sequence[Candidate]], limit: int) -> list[int]:
    """The queue's job ids from each band's candidates in band order, each band's in its order: the
    unlabelled ones round-robin, then, when they ran out before `limit`, the labelled ones the same way."""
    unlabelled = [[c.job_id for c in band if not c.labelled] for band in bands]
    labelled = [[c.job_id for c in band if c.labelled] for band in bands]
    order = _round_robin(unlabelled, limit)
    return order + _round_robin(labelled, limit - len(order))


async def _items(conn: AsyncConnection, order: Sequence[int]) -> list[LabelItemOut]:
    if not order:
        return []
    rows = await conn.execute(_ITEMS, {"ids": list(order), "chars": EXCERPT_CHARS})
    by_id = {
        int(row.job_id): LabelItemOut(
            job_id=int(row.job_id),
            title=row.title,
            company=row.company_display,
            locations=location_labels(row.locations),
            ad_language=row.ad_language,
            excerpt=row.excerpt,
        )
        for row in rows
    }
    return [by_id[job_id] for job_id in order if job_id in by_id]


async def labelling_queue(conn: AsyncConnection, *, limit: int) -> LabellingQueueOut:
    """Up to `limit` ads to label and the progress (module docstring); `limit` at least 1."""
    if limit < 1:
        raise ValueError("the limit must be at least 1")
    active = (await conn.execute(_ACTIVE)).one_or_none()
    if active is None or active.profile_version_id is None:
        return LabellingQueueOut(items=[], progress=LabelProgressOut(labelled=0, target=LABEL_TARGET))
    profile = int(active.profile_version_id)
    context = {
        "tuple": int(active.llm_tuple_id),
        "scope": int(active.scope_version_id),
        "profile": profile,
        "limit": limit,
    }
    bands: list[list[Candidate]] = []
    for index, (low, high) in enumerate(BANDS):
        statement = _TOP_BAND if index == 0 else _LOWER_BAND
        rows = await conn.execute(statement, {**context, "low": low, "high": high})
        bands.append([Candidate(int(row.job_id), bool(row.labelled)) for row in rows])
    items = await _items(conn, queue_order(bands, limit))
    labelled = int((await conn.execute(_PROGRESS, {"profile": profile})).scalar_one())
    return LabellingQueueOut(items=items, progress=LabelProgressOut(labelled=labelled, target=LABEL_TARGET))
