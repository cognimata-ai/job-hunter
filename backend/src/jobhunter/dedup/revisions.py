"""A job's canonical fields and its current revision (spec §8.8, §7.9): `recompute_job`.

`recompute_job` runs in the caller's transaction, which holds the job row `FOR UPDATE` (the identity
transaction of §8.6, a merge, the M5 purge); it takes the lock again, a no-op then. It reads the latest
observation of every posting of the job and of the jobs merged into it (`merged_into_job_id`, transitively),
leaving out the observations of `fenced_source_ids`, and makes the §8.8 selection (`fields.select_canonical`,
with the job's `first_seen_at` and, when a posting came from a watchlist board, that company's display name
with the best-ranked such observation). Then:

1. **The revision.** The job's revision with the selection's `material_hash` is reused (`UNIQUE (job_id,
   material_hash)`), unless its lineage (`job_revision_sources`) names a fenced source. Such a tainted
   revision is never reused, and no twin is created either, because the unique key forbids a second row with
   that hash while it exists: the job keeps its current revision and the result says
   `tainted_reuse_refused` (plan Deviation 24). The M5 purge deletes the tainted revision first; the same
   recompute then creates it anew. Without a match a revision is inserted (`revision_no = max + 1` over the
   job's revisions, `embedding_status = 'awaiting_embedding'`) with its lineage: one row per `llm_input`
   field and source (`fields.CanonicalFields.contributors`), naming the posting and the observation.
2. **The job row**, in one statement: every selected column (with `structured_seniority`, Task 16 ruling
   T16-a), `field_provenance`, `det_facts_hash`, `current_revision_id`, and `updated_at = now()`. The same
   statement compares the `LIST_COLUMNS` before and after (`list_changed`).
3. **An occurrence** when the current revision changed: a `job_revision_occurrences` row at `now()`. Two
   changes of one job in one transaction share that time, and the row records the last.
4. **`dedup.field_conflict`** (info, subject `job:<id>:<field>`) follows the conflict: it is raised when a
   field conflicts and did not in the job's previous selection, and resolved when the conflict is gone. The
   previous selection's conflicts are read from the stored `field_provenance` (rule `fields.CONFLICT_RULE`):
   `raise_alert` counts an occurrence on every call, so an unchanged conflict must not call it, and the job
   row already says whether the conflict is new, with no query of `alerts`. An alert resolved by hand while
   the conflict persists is raised again only after the conflict has gone and come back.

Every timestamp written is the database's `now()`, the clock of `runs.started_at` that node 8 and the
grouping pass compare with (carry 3; Task 17 ruling T17-a dropped the caller clock). The ranking generation is
the caller's to bump, when `list_changed`: `ranking.bump` is the last statement of its transaction (§7.1),
after the hard filters it writes for the job.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.db.sql import q
from jobhunter.errors import InvariantViolation
from jobhunter.normalize.fields import (
    CONFLICT_FIELDS,
    CONFLICT_RULE,
    CanonicalFields,
    ObservationView,
    WatchlistDisplay,
    precedence_key,
    select_canonical,
)
from jobhunter.observability.alerts import raise_alert, resolve_alert
from jobhunter.observability.logging import get_logger
from jobhunter.sources.base import NormalizedPosting
from jobhunter.types import JSONValue

FIELD_CONFLICT_ALERT: Final = "dedup.field_conflict"
# The `jobs` columns this recompute writes that the Jobs list filters, sorts, searches or shows (§7.1: "a
# change of any job field that a filter or sort reads"): every canonical column, `source_ids`,
# `posted_at_effective` and `current_revision_id` (the list searches and scores the current revision). Not
# `field_provenance` (shown only in the drawer's provenance tab), `det_facts_hash` (a hash of columns listed
# here) or `updated_at`.
LIST_COLUMNS: Final = (
    "company_key",
    "company_display",
    "title",
    "title_norm",
    "seniority_tokens",
    "structured_seniority",
    "locations",
    "location_kind",
    "work_mode_possible",
    "work_mode_source",
    "remote_countries",
    "employment_types",
    "employment_source",
    "salary",
    "industry_structured",
    "ad_language",
    "completeness",
    "posted_at_effective",
    "posted_precision",
    "source_ids",
    "current_revision_id",
)

_JOB: Final = q(
    "SELECT first_seen_at, current_revision_id, det_facts_hash, field_provenance FROM jobs "
    "WHERE id = :job FOR UPDATE"
)
# The latest observation of each posting of the job and of every job merged into it, by posting id; a fenced
# source's are left out.
_OBSERVATIONS: Final = q(
    "WITH RECURSIVE family (id) AS ("
    "SELECT CAST(:job AS bigint) UNION SELECT j.id FROM jobs j JOIN family f ON j.merged_into_job_id = f.id) "
    "SELECT p.id AS posting_id, o.id AS observation_id, o.source_instance_id, i.source_id, o.observed_at, "
    "o.normalized, o.completeness, o.quality, w.display_name AS watchlist_display "
    "FROM postings p JOIN posting_observations o ON o.id = p.latest_observation_id "
    "JOIN source_instances i ON i.id = o.source_instance_id "
    "LEFT JOIN watchlist_companies w ON w.id = i.watchlist_company_id "
    "WHERE p.job_id IN (SELECT id FROM family) AND NOT (i.source_id = ANY (CAST(:fenced AS text[]))) "
    "ORDER BY p.id"
)
_REVISION: Final = q(
    "SELECT r.id, EXISTS (SELECT 1 FROM job_revision_sources s WHERE s.job_revision_id = r.id "
    "AND s.source_id = ANY (CAST(:fenced AS text[]))) AS tainted "
    "FROM job_revisions r WHERE r.job_id = :job AND r.material_hash = :hash"
)
_INSERT_REVISION: Final = q(
    "INSERT INTO job_revisions (job_id, revision_no, material_hash, llm_input, description_text, "
    "description_completeness, ad_language, embedding_status) "
    "SELECT :job, coalesce(max(revision_no), 0) + 1, :hash, :llm_input, :description, :completeness, "
    ":language, 'awaiting_embedding' FROM job_revisions WHERE job_id = :job RETURNING id",
    jsonb=["llm_input"],
)
_INSERT_LINEAGE: Final = q(
    "INSERT INTO job_revision_sources (job_revision_id, field, source_id, posting_id, observation_id) "
    "SELECT :revision, l.field, l.source_id, l.posting_id, l.observation_id "
    "FROM unnest(CAST(:fields AS text[]), CAST(:sources AS text[]), CAST(:postings AS bigint[]), "
    "CAST(:observations AS bigint[])) AS l(field, source_id, posting_id, observation_id)"
)
# `old` is the row before the update (the statement's snapshot), `j` the row after it: the RETURNING compares
# the list columns of both. The column lists are constants of this module, never input.
_UPDATE_JOB: Final = q(
    f"WITH old AS (SELECT {', '.join(LIST_COLUMNS)} FROM jobs WHERE id = :job) "  # noqa: S608
    "UPDATE jobs AS j SET company_key = :company_key, company_display = :company_display, title = :title, "
    "title_norm = :title_norm, seniority_tokens = CAST(:seniority_tokens AS text[]), "
    "structured_seniority = :structured_seniority, locations = :locations, location_kind = :location_kind, "
    "work_mode_possible = CAST(:work_mode_possible AS text[]), work_mode_source = :work_mode_source, "
    "remote_countries = CAST(:remote_countries AS text[]), "
    "employment_types = CAST(:employment_types AS text[]), "
    "employment_source = :employment_source, salary = NULLIF(:salary, 'null'::jsonb), "
    "industry_structured = :industry_structured, ad_language = :ad_language, completeness = :completeness, "
    "posted_at_effective = :posted_at_effective, posted_precision = :posted_precision, "
    "source_ids = CAST(:source_ids AS text[]), field_provenance = :field_provenance, "
    "det_facts_hash = :det_facts_hash, current_revision_id = :current_revision_id, updated_at = now() "
    "FROM old WHERE j.id = :job "
    f"RETURNING ROW({', '.join(f'old.{column}' for column in LIST_COLUMNS)}) IS DISTINCT FROM "
    f"ROW({', '.join(f'j.{column}' for column in LIST_COLUMNS)}) AS list_changed",
    jsonb=["locations", "salary", "field_provenance"],
)
_OCCURRENCE: Final = q(
    "INSERT INTO job_revision_occurrences (job_id, job_revision_id, became_current_at) "
    "VALUES (:job, :revision, now()) "
    "ON CONFLICT (job_id, became_current_at) DO UPDATE SET job_revision_id = EXCLUDED.job_revision_id"
)

log = get_logger("jobhunter.dedup.revisions")


@dataclass(frozen=True)
class RecomputeResult:
    job_id: int
    revision_id: int  # the job's current revision afterwards
    revision_created: bool
    current_changed: bool
    det_facts_changed: bool
    list_changed: bool  # a `LIST_COLUMNS` value changed: the caller bumps the ranking generation
    tainted_reuse_refused: bool
    conflicts: tuple[str, ...]


def _view(row: Row[tuple[object, ...]]) -> ObservationView:
    return ObservationView(
        posting_id=int(row.posting_id),
        observation_id=int(row.observation_id),
        source_instance_id=int(row.source_instance_id),
        source_id=row.source_id,
        observed_at=row.observed_at,
        normalized=NormalizedPosting.model_validate(row.normalized),
        completeness=row.completeness,
        quality=row.quality,
    )


def _watchlist_display(views: Sequence[ObservationView], names: Mapping[int, str]) -> WatchlistDisplay | None:
    """The watchlist display name, by posting id, of the best-ranked observation from a watchlist board, with
    that observation."""
    boards = [view for view in views if view.posting_id in names]
    if not boards:
        return None
    board = min(boards, key=precedence_key)
    return WatchlistDisplay(names[board.posting_id], board)


def _previous_conflicts(provenance: JSONValue) -> frozenset[str]:
    """The fields that conflicted in the selection the stored `field_provenance` records."""
    if not isinstance(provenance, dict):
        return frozenset()
    return frozenset(
        field
        for field in CONFLICT_FIELDS
        if isinstance(entry := provenance.get(field), dict) and entry.get("rule") == CONFLICT_RULE
    )


def _lineage(fields: CanonicalFields, source_of: dict[int, str]) -> dict[str, list[object]]:
    """The `job_revision_sources` rows, as columns: per `llm_input` field, the first supplier per source."""
    columns: dict[str, list[object]] = {"fields": [], "sources": [], "postings": [], "observations": []}
    for field, suppliers in sorted(fields.contributors.items()):
        named: set[str] = set()
        for supplier in suppliers:
            source = source_of[supplier.source_instance_id]
            if source in named:
                continue
            named.add(source)
            columns["fields"].append(field)
            columns["sources"].append(source)
            columns["postings"].append(supplier.posting_id)
            columns["observations"].append(supplier.observation_id)
    return columns


async def _insert_revision(
    conn: AsyncConnection, job_id: int, fields: CanonicalFields, source_of: dict[int, str]
) -> int:
    params = {
        "job": job_id,
        "hash": fields.material_hash,
        "llm_input": fields.llm_input,
        "description": fields.description_text,
        "completeness": fields.description_completeness,
        "language": fields.ad_language,
    }
    revision_id = int((await conn.execute(_INSERT_REVISION, params)).scalar_one())
    await conn.execute(_INSERT_LINEAGE, {"revision": revision_id, **_lineage(fields, source_of)})
    return revision_id


def _job_params(job_id: int, fields: CanonicalFields, current_revision_id: int) -> dict[str, object]:
    return {
        "job": job_id,
        "company_key": fields.company_key,
        "company_display": fields.company_display,
        "title": fields.title,
        "title_norm": fields.title_norm,
        "seniority_tokens": sorted(fields.seniority_tokens),
        "structured_seniority": fields.structured_seniority,
        "locations": [location.model_dump(mode="json") for location in fields.locations],
        "location_kind": fields.location_kind,
        "work_mode_possible": sorted(fields.work_mode_possible),
        "work_mode_source": fields.work_mode_source,
        "remote_countries": list(fields.remote_countries),
        "employment_types": list(fields.employment_types),
        "employment_source": fields.employment_source,
        "salary": None if fields.salary is None else fields.salary.model_dump(mode="json"),
        "industry_structured": fields.industry_structured,
        "ad_language": fields.ad_language,
        "completeness": fields.completeness,
        "posted_at_effective": fields.posted_at_effective,
        "posted_precision": fields.posted_precision,
        "source_ids": list(fields.source_ids),
        "field_provenance": {name: p.to_json() for name, p in fields.field_provenance.items()},
        "det_facts_hash": fields.det_facts_hash,
        "current_revision_id": current_revision_id,
    }


async def _follow_conflicts(
    conn: AsyncConnection, job_id: int, previous: frozenset[str], current: tuple[str, ...]
) -> None:
    """Raise `dedup.field_conflict` for each new conflict and resolve it for each conflict that is gone."""
    for field in sorted(set(current) - previous):
        log.info(FIELD_CONFLICT_ALERT, job_id=job_id, field=field)
        await raise_alert(
            conn,
            code=FIELD_CONFLICT_ALERT,
            severity="info",
            subject=f"job:{job_id}:{field}",
            message="the structured values of the job's postings disagree",
            context={"job_id": job_id, "field": field},
        )
    for field in sorted(previous - set(current)):
        log.info("field_conflict_resolved", job_id=job_id, field=field)
        await resolve_alert(conn, code=FIELD_CONFLICT_ALERT, subject=f"job:{job_id}:{field}")


async def recompute_job(
    conn: AsyncConnection, job_id: int, *, fenced_source_ids: frozenset[str] = frozenset()
) -> RecomputeResult:
    """Recompute the job's canonical fields and reuse or create its current revision (module docstring).

    Raises `LookupError` for a job that does not exist or has no observation outside `fenced_source_ids`, and
    `InvariantViolation` when a tainted revision blocks the reuse of a job that has no current revision (the
    purge must delete the tainted revision first).
    """
    job = (await conn.execute(_JOB, {"job": job_id})).first()
    if job is None:
        raise LookupError(f"job {job_id} does not exist")
    fenced = sorted(fenced_source_ids)
    rows = (await conn.execute(_OBSERVATIONS, {"job": job_id, "fenced": fenced})).all()
    if not rows:
        raise LookupError(f"job {job_id} has no observation outside the fenced sources {fenced}")
    views = [_view(row) for row in rows]
    names = {int(row.posting_id): row.watchlist_display for row in rows if row.watchlist_display is not None}
    fields = select_canonical(
        views, first_seen_at=job.first_seen_at, watchlist_display=_watchlist_display(views, names)
    )
    source_of = {view.source_instance_id: view.source_id for view in views}
    previous: int | None = job.current_revision_id

    match = (
        await conn.execute(_REVISION, {"job": job_id, "hash": fields.material_hash, "fenced": fenced})
    ).first()
    created = refused = False
    if match is None:
        current = await _insert_revision(conn, job_id, fields, source_of)
        created = True
    elif not match.tainted:
        current = int(match.id)
    elif previous is None:
        raise InvariantViolation(
            f"job {job_id}: revision {match.id} is tainted by {fenced} and the job has no current revision"
        )
    else:  # the reuse is refused, and the unique key forbids a twin: the job keeps its current revision
        current, refused = previous, True
        log.warning("tainted_revision_reuse_refused", job_id=job_id, revision_id=int(match.id), fenced=fenced)

    updated = await conn.execute(_UPDATE_JOB, _job_params(job_id, fields, current))
    list_changed = bool(updated.scalar_one())
    changed = current != previous
    if changed:
        await conn.execute(_OCCURRENCE, {"job": job_id, "revision": current})
    await _follow_conflicts(conn, job_id, _previous_conflicts(job.field_provenance), fields.conflicts)
    return RecomputeResult(
        job_id=job_id,
        revision_id=current,
        revision_created=created,
        current_changed=changed,
        det_facts_changed=bytes(job.det_facts_hash) != fields.det_facts_hash,
        list_changed=list_changed,
        tainted_reuse_refused=refused,
        conflicts=fields.conflicts,
    )
