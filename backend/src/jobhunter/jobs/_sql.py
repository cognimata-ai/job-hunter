"""The static SQL of the Jobs list engine (`jobs.listing`, which composes it; plan M4 Task 4, §16.7).

Every string here is a constant. A statement is a choice among them, joined in a fixed order: no value, sort
name or variant name is ever interpolated, and every value is a bound parameter. The aliases are those of a
card's score row: `j` the job, `t` its active-tuple target, `r` the published result, `d` the current
instance, `h` the hard-filter result, `u` the user's actions, and `m` a deferred group member.

**Plans at scale** (M4 Task 5, B29: the list's p95 on 50,000 cards). Five shapes keep the planner's estimates
honest, without which it chose nested loops over whole-table scans and JIT compilation:
- a walk from an instance or a target finds its job by a fenced lateral lookup (`OFFSET 0`): the job is one
  row per walked row, so the redundant `current_revision_id` check is no join selectivity that would multiply
  the walk's estimate down to one row (and make a full sort look cheaper than the LIMIT walk of its index);
- the deferred members that match (`DEFERRED_MEMBER`) are one uncorrelated set, an InitPlan run once per
  statement, not an EXISTS costed (and planned around) for every row it would rarely run for;
- the location filter decides most rows by two jsonpath steps before `job_within_km` (`NEAR_INSIDE`), once
  per request: its matches are read in the read snapshot (`NEAR_MATCHES`), as the keyword's are below, and
  every statement binds them (plan `2026-11-16-jobs-list-perf` Task P2). Decided in every statement, the
  steps ran for every job of the posted window in each count of a first page;
- a card under a purge fence (`NOT_FENCED`, §20.2, M5 Task 8) is filtered only while a fence is open: the list
  and its facets read `privacy.fences.any_fence_open` once per request, in their read snapshot, and add
  `NOT_FENCED` only when it holds (plan `2026-11-16-jobs-list-perf` Task P1, Ruling JP-1). With none open, a
  statement names neither `is_fenced` nor `purge_fences`: M4's SQL. `is_fenced` is SQL the planner cannot see
  into, and as a conjunct of every statement, even behind D21's uncorrelated "no fence is open", it skewed the
  row estimates: `count_unscored` then joined `user_job_actions` by a scan per row;
- the keyword's matches (`KEYWORD_MATCHES`) are read once per request, in the read snapshot, and every
  statement binds them as arrays, each a hashed set as the sub-selects were (plan `2026-11-16-jobs-list-perf`
  Task P2). As a sub-select of every statement, the title branch was a sequential scan that computed a word
  similarity per title, about 120 ms on 50,000 cards, in the statement and again in its deferred-member set:
  five times on a first page. The read runs with sequential scans off (`SET_SEQSCAN`), so that it takes the
  two text indexes, 30 ms against 140: the planner prices a GIN index's pending list (after a bulk load, and
  between two vacuums) as random reads, and `<%` as one operator call, and so prefers the scans.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from sqlalchemy.sql.elements import TextClause

from jobhunter.db.sql import q as sql
from jobhunter.jobs.query import ScoringBucket, UserStatus

ACTIVE: Final = sql(
    "SELECT a.llm_tuple_id, a.scope_version_id, s.payload AS scope FROM active_config a "
    "JOIN scope_versions s ON s.id = a.scope_version_id WHERE a.id = 1"
)
DISPLAY_TIMEZONE: Final = sql(
    "SELECT v.payload #>> '{display,timezone}' FROM active_config a "
    "JOIN settings_versions v ON v.id = a.settings_version_id WHERE a.id = 1"
)

# The card's score row, from the job (R8)
CARD_FROM: Final = (
    " FROM jobs j"
    " LEFT JOIN score_targets t ON t.job_revision_id = j.current_revision_id AND t.llm_tuple_id = :tuple"
    " LEFT JOIN llm_results r ON r.id = t.published_result_id"
    " LEFT JOIN det_results d ON d.llm_result_id = r.id AND d.scope_version_id = :scope AND d.is_current"
    " LEFT JOIN hard_filter_results h ON h.job_id = j.id AND h.scope_version_id = :scope"
    " LEFT JOIN user_job_actions u ON u.job_id = j.id"
)
CARD: Final = (
    " WHERE j.merged_into_job_id IS NULL AND NOT j.synthetic_eval AND j.current_revision_id IS NOT NULL"
    " AND t.state IS DISTINCT FROM 'deferred_duplicate'"
)
# ... from the current instance (the scored section and the not-a-job-ad cards); its job is a fenced lateral
# lookup (module docstring, Plans at scale)
INSTANCE_FROM: Final = (
    " FROM det_results d"
    " JOIN llm_results r ON r.id = d.llm_result_id"
    " JOIN score_targets t ON t.published_result_id = r.id AND t.llm_tuple_id = :tuple"
    " CROSS JOIN LATERAL (SELECT * FROM jobs cj WHERE cj.id = d.job_id"
    " AND cj.current_revision_id = t.job_revision_id OFFSET 0) j"
    " LEFT JOIN hard_filter_results h ON h.job_id = j.id AND h.scope_version_id = :scope"
    " LEFT JOIN user_job_actions u ON u.job_id = j.id"
)
# ... from an unscored target (`score_targets_unscored_rank`: its predicate, verbatim); its job as above
TARGET_FROM: Final = (
    " FROM score_targets t"
    " CROSS JOIN LATERAL (SELECT * FROM jobs cj WHERE cj.id = t.job_id"
    " AND cj.current_revision_id = t.job_revision_id OFFSET 0) j"
    " LEFT JOIN llm_results r ON r.id = t.published_result_id"
    " LEFT JOIN det_results d ON d.llm_result_id = r.id AND d.scope_version_id = :scope AND d.is_current"
    " LEFT JOIN hard_filter_results h ON h.job_id = j.id AND h.scope_version_id = :scope"
    " LEFT JOIN user_job_actions u ON u.job_id = j.id"
    " WHERE t.llm_tuple_id = :tuple"
    " AND t.state NOT IN ('published_accepted','published_contested','deferred_duplicate','cancelled')"
)
LIVE: Final = " AND j.merged_into_job_id IS NULL AND NOT j.synthetic_eval"
INSTANCE: Final = " WHERE d.scope_version_id = :scope AND d.is_current"


@dataclass(frozen=True)
class Walk:
    """One keyset walk: `head` (SELECT … WHERE …), the bound `after` a position adds, and `order`."""

    head: str
    after: str
    order: str


SIMILARITY_AFTER: Final = (
    " AND ROW(t.similarity, t.job_id) < ROW(CAST(:after_value AS real), CAST(:after_id AS bigint))"
)
SIMILARITY_ORDER: Final = " ORDER BY t.similarity DESC NULLS LAST, t.job_id DESC LIMIT :limit"

SCORED: Final[Mapping[str, Walk]] = MappingProxyType(
    {
        "score_mid": Walk(
            "SELECT d.job_id AS job_id, d.score_mid AS sort_value"
            + INSTANCE_FROM
            + INSTANCE
            + " AND d.score_mid IS NOT NULL"
            + LIVE,
            " AND ROW(d.score_mid, d.job_id) < ROW(CAST(:after_value AS numeric), CAST(:after_id AS bigint))",
            " ORDER BY d.score_mid DESC NULLS LAST, d.job_id DESC LIMIT :limit",
        ),
        "score_low": Walk(
            "SELECT d.job_id AS job_id, d.score_low AS sort_value"
            + INSTANCE_FROM
            + INSTANCE
            + " AND d.score_low IS NOT NULL"
            + LIVE,
            " AND ROW(d.score_low, d.job_id) < ROW(CAST(:after_value AS numeric), CAST(:after_id AS bigint))",
            " ORDER BY d.score_low DESC NULLS LAST, d.job_id DESC LIMIT :limit",
        ),
    }
)
# The unscored section: `N` (similarity known) and `Z` (no similarity, job id only), by its three sources
TARGETS_RANKED: Final = Walk(
    "SELECT t.job_id AS job_id, t.similarity AS sort_value"
    + TARGET_FROM
    + " AND t.similarity IS NOT NULL"
    + LIVE,
    SIMILARITY_AFTER,
    SIMILARITY_ORDER,
)
TARGETS_UNRANKED: Final = Walk(
    "SELECT t.job_id AS job_id, t.similarity AS sort_value"
    + TARGET_FROM
    + " AND t.similarity IS NULL"
    + LIVE,
    " AND t.job_id < CAST(:after_id AS bigint)",
    SIMILARITY_ORDER,  # the index's order: every similarity here is NULL
)
NOT_AN_AD_RANKED: Final = Walk(
    "SELECT t.job_id AS job_id, t.similarity AS sort_value"
    + INSTANCE_FROM
    + INSTANCE
    + " AND d.score_mid IS NULL AND t.similarity IS NOT NULL"
    + LIVE,
    SIMILARITY_AFTER,
    SIMILARITY_ORDER,
)
NOT_AN_AD_UNRANKED: Final = Walk(
    "SELECT t.job_id AS job_id, t.similarity AS sort_value"
    + INSTANCE_FROM
    + INSTANCE
    + " AND d.score_mid IS NULL AND t.similarity IS NULL"
    + LIVE,
    " AND t.job_id < CAST(:after_id AS bigint)",
    " ORDER BY t.job_id DESC LIMIT :limit",
)
UNTARGETED: Final = Walk(
    "SELECT j.id AS job_id, CAST(NULL AS real) AS sort_value" + CARD_FROM + CARD + " AND t.id IS NULL",
    " AND j.id < CAST(:after_id AS bigint)",
    " ORDER BY j.id DESC LIMIT :limit",
)
# The date sorts
POSTED_ORDER: Final = " ORDER BY j.posted_at_effective DESC NULLS LAST, j.id DESC LIMIT :limit"
POSTED: Final = Walk(
    "SELECT j.id AS job_id, j.posted_at_effective AS sort_value"
    + CARD_FROM
    + CARD
    + " AND j.posted_at_effective IS NOT NULL",
    " AND ROW(j.posted_at_effective, j.id)"
    " < ROW(CAST(:after_value AS timestamptz), CAST(:after_id AS bigint))",
    POSTED_ORDER,
)
POSTED_UNKNOWN: Final = Walk(
    "SELECT j.id AS job_id, j.posted_at_effective AS sort_value"
    + CARD_FROM
    + CARD
    + " AND j.posted_at_effective IS NULL",
    " AND j.id < CAST(:after_id AS bigint)",
    POSTED_ORDER,  # the index's order: every date here is NULL
)
FIRST_SEEN: Final = Walk(
    "SELECT j.id AS job_id, j.first_seen_at AS sort_value" + CARD_FROM + CARD,
    " AND ROW(j.first_seen_at, j.id) < ROW(CAST(:after_value AS timestamptz), CAST(:after_id AS bigint))",
    " ORDER BY j.first_seen_at DESC, j.id DESC LIMIT :limit",
)
# The sources of one branch merged by its order, each bounded by the page's limit
MERGED_HEAD: Final = "SELECT s.job_id, s.sort_value FROM ("
MERGED_ORDER: Final[Mapping[str, str]] = MappingProxyType(
    {
        "ranked": ") s ORDER BY s.sort_value DESC NULLS LAST, s.job_id DESC LIMIT :limit",
        "unranked": ") s ORDER BY s.job_id DESC LIMIT :limit",
    }
)
COUNT_HEAD: Final = "SELECT count(*) FROM (SELECT 1" + CARD_FROM + CARD  # noqa: S608 (constants)
UNSCORED_SECTION: Final = " AND d.score_mid IS NULL"
COUNT_TAIL: Final = " LIMIT :cap) s"
# The tag a statement starts with, so a log line or a plan names its segment
TAG: Final[Mapping[str, str]] = MappingProxyType(
    {
        "scored": "/* jobs_list.scored */ ",
        "unscored_ranked": "/* jobs_list.unscored_ranked */ ",
        "unscored_unranked": "/* jobs_list.unscored_unranked */ ",
        "posted": "/* jobs_list.posted */ ",
        "posted_unknown": "/* jobs_list.posted_unknown */ ",
        "first_seen": "/* jobs_list.first_seen */ ",
        "count": "/* jobs_list.count */ ",
        "count_unscored": "/* jobs_list.count_unscored */ ",
        "keyword": "/* jobs_list.keyword */ ",
        "near": "/* jobs_list.near */ ",
    }
)

# The non-score predicates P(x) of a job `x`: the card's representative `j` or a deferred member `m`. The
# keyword matches the full text of the job's current revision in both configurations or its title by word
# similarity (B4: `<%` at `TRGM_WORD_THRESHOLD`): two indexed branches, each its own set. The full-text
# branch is a set of revision ids that the current revision is looked up in: joined to `jobs` inside the
# set, the mergeable `current_revision_id = kr.id` would make the planner read the revisions in
# `job_revisions_pkey` order instead of through `job_revisions_tsv` (P6).
TEXT_REVISIONS: Final = (
    "SELECT kr.id FROM job_revisions kr WHERE kr.tsv @@ websearch_to_tsquery('english', :q) "
    "OR kr.tsv @@ websearch_to_tsquery('german_unaccent', :q)"
)
TITLE_JOBS: Final = "SELECT kt.id FROM jobs kt WHERE :qf <% kt.title_norm"
# The keyword's matches, both sets in one statement, read once per request (module docstring, Plans at
# scale) under a planner setting of its own: sequential scans off (`SET_SEQSCAN`), then the caller's setting
# (`SEQSCAN`) again
KEYWORD_MATCHES: Final = (
    "SELECT ARRAY(" + TEXT_REVISIONS + ") AS revisions, ARRAY(" + TITLE_JOBS + ") AS jobs"
)
SEQSCAN: Final = sql("SELECT current_setting('enable_seqscan')")
SET_SEQSCAN: Final = sql("SELECT set_config('enable_seqscan', :setting, true)")
# The location filter is `job_within_km` (B5), decided in two jsonpath steps first (M4 Task 5, B29), since
# the function is SQL, not inlinable, and costs ~11 µs a call. `:near_box` holds their bounds
# (`listing._near_box`), a JSONB bind (`clause`), so the variables are one constant, not a document built per
# row:
# - NEAR_INSIDE: a candidate of a resolved entry lies in the box `lo_in..hi_in` by `west..east`, every point
#   of which lies within the radius on `geo_km`'s sphere (a subset of the function's matches): a match, with
#   no call. Gazetteer candidates sit on place centroids, so most matches are decided here;
# - NEAR_BAND: else the function runs only when some candidate passes the bounds it checks first, |Δlat| <=
#   radius / 110.5 and, where it applies one, its |Δlon| bound, each widened by `NEAR_BAND_WIDENING` degrees
#   (0.1 m) so that the bounds' conversion to jsonb numbers never narrows them (a superset of its matches;
#   `listing._near_box`).
# Both compare the candidates' coordinates as the JSON numbers `sources.parsing.locations.Location` stores,
# their longitudes in [-180, 180].
NEAR_INSIDE: Final = (
    '\'$[*] ? (@.resolution == "unique" || @.resolution == "ambiguous").candidates[*] '
    "? (@.lat >= $lo_in && @.lat <= $hi_in && @.lon >= $west && @.lon <= $east)'"
)
NEAR_BAND: Final = (
    "'$[*].candidates[*] ? (@.lat >= $lo && @.lat <= $hi && @.lon >= $lo_lon && @.lon <= $hi_lon)'"
)
NEAR_BAND_WIDENING: Final = 1e-6
JSONB_BINDS: Final = ("near_box",)
# The location's matches, read once per request (module docstring, Plans at scale): `NEAR_MATCHES`, then the
# near term and the posted window's terms rendered for `j` and joined by AND, then `NEAR_MATCHES_END`. Every
# job the set is checked for is in the same window, a conjunct of the same predicate, so the window changes
# no match: it spares the jsonpath steps for the jobs outside it, half of them under the default 30 days.
NEAR_MATCHES: Final = "SELECT ARRAY(SELECT j.id FROM jobs j WHERE "
NEAR_MATCHES_END: Final = ")"
# B4: the word similarity `<%` requires, pg_trgm's `word_similarity_threshold`. The list sets nothing: it runs
# under the server's value, and `test_jobs_listing.py` checks that value is this one for the app's role.
TRGM_WORD_THRESHOLD: Final = 0.6
ROW_TEMPLATES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "posted_after": "coalesce({x}.posted_at_effective, {x}.first_seen_at) >= :posted_after",
        "posted_before": "coalesce({x}.posted_at_effective, {x}.first_seen_at) < :posted_before",
        # the keyword's matches, as `KEYWORD_MATCHES` read them (`listing.Matches`)
        "keyword": "({x}.current_revision_id IN (SELECT unnest(CAST(:keyword_revisions AS bigint[]))) "
        "OR {x}.id IN (SELECT unnest(CAST(:keyword_jobs AS bigint[]))))",
        # the location's predicate, which `NEAR_MATCHES` reads; a statement binds its matches, `near_matched`
        "near": "((jsonb_path_exists({x}.locations, " + NEAR_INSIDE + ", :near_box, true) "
        "OR (jsonb_path_exists({x}.locations, " + NEAR_BAND + ", :near_box, true) "
        "AND job_within_km({x}.locations, CAST(:lat AS float8), CAST(:lon AS float8), "
        "CAST(:radius_km AS float8)))) AND {x}.location_kind <> 'country_wide')",
        "near_matched": "{x}.id IN (SELECT unnest(CAST(:near_jobs AS bigint[])))",
        # B5: an unknown work mode is never remote
        "remote": "({x}.location_kind = 'remote' OR ({x}.work_mode_source <> 'none' "
        "AND 'remote' = ANY({x}.work_mode_possible)))",
        "known_mode": "({x}.work_mode_source <> 'none' "
        "AND {x}.work_mode_possible && CAST(:work_modes AS text[]))",
        "unknown_mode": "{x}.work_mode_source = 'none'",
        "company": "{x}.company_key = ANY(CAST(:companies AS text[]))",
        "source": "{x}.source_ids && CAST(:sources AS text[])",
        "lang": "{x}.ad_language = ANY(CAST(:langs AS text[]))",
        "employment": "{x}.employment_types && CAST(:employment AS text[])",
        "no_employment": "cardinality({x}.employment_types) = 0",
        "seniority": "({x}.seniority_tokens && CAST(:levels AS text[]) "
        "OR {x}.structured_seniority = ANY(CAST(:levels AS text[])))",
        # a job without a token and without a structured level is `mid` (`normalize/titles.py`)
        "no_seniority": "(cardinality({x}.seniority_tokens) = 0 AND {x}.structured_seniority IS NULL)",
        "availability": "{x}.availability = ANY(CAST(:availability AS text[]))",
        "partial": "{x}.completeness = 'partial'",
        "full": "{x}.completeness = 'full'",
    }
)
ROW_SQL: Final[Mapping[str, Mapping[str, str]]] = MappingProxyType(
    {
        alias: MappingProxyType({name: template.format(x=alias) for name, template in ROW_TEMPLATES.items()})
        for alias in ("j", "m")
    }
)
# A deferred member `m` of the representative `j`'s active group that matches P(m): `DEFERRED_MEMBER`, then
# P(m) rendered for `m`, then `DEFERRED_MEMBER_END`, which closes the set and the term it opened. The set is
# the representatives (joined as members of their own group, `rj`) of the active groups with such a member:
# uncorrelated, so an InitPlan computed once per statement (module docstring, Plans at scale).
DEFERRED_MEMBER: Final = (
    "(j.duplicate_group_id IS NOT NULL AND j.id = ANY(ARRAY(SELECT g.representative_job_id "
    "FROM duplicate_groups g "
    "JOIN jobs rj ON rj.id = g.representative_job_id AND rj.duplicate_group_id = g.id "
    "JOIN jobs m ON m.duplicate_group_id = g.id "
    "JOIN score_targets tm ON tm.job_revision_id = m.current_revision_id AND tm.llm_tuple_id = :tuple "
    "AND tm.state = 'deferred_duplicate' "
    "WHERE g.status = 'active' AND m.id <> g.representative_job_id AND m.merged_into_job_id IS NULL AND "
)
DEFERRED_MEMBER_END: Final = ")))"

# The representative's own predicates
STATUS_ORDER: Final[tuple[UserStatus, ...]] = ("unseen", "seen", "saved", "applied", "hidden")
STATUS_SQL: Final[Mapping[str, str]] = MappingProxyType(
    {
        "unseen": "u.seen_at IS NULL",
        "seen": "u.seen_at IS NOT NULL",
        "saved": "u.saved_at IS NOT NULL",
        "applied": "u.applied_at IS NOT NULL",
        "hidden": "u.hidden_at IS NOT NULL",
    }
)
NOT_HIDDEN: Final = "u.hidden_at IS NULL"
# No unreleased purge fence matches the card's job: a fenced job is no card. Added only while the request's
# snapshot shows a fence open, so it needs no "no fence is open" term of its own (module docstring).
NOT_FENCED: Final = "NOT is_fenced(j.id)"
BUCKET_ORDER: Final[tuple[ScoringBucket, ...]] = (
    "scored",
    "filtered",
    "unscored",
    "failed",
    "expired",
    "not_a_job_ad",
)
BUCKET_SQL: Final[Mapping[str, str]] = MappingProxyType(  # B8: exclusive
    {
        "scored": "(d.score_mid IS NOT NULL AND h.passed IS NOT FALSE)",
        "filtered": "(t.state = 'filtered_out' OR (d.score_mid IS NOT NULL AND h.passed = false))",
        "unscored": "(t.id IS NULL OR t.state IN ('pending','budget_deferred','in_progress','in_batch'))",
        "failed": "(t.state = 'refused' OR t.state LIKE 'failed\\_%')",
        "expired": "t.state = 'expired_unscored'",
        "not_a_job_ad": "(d.id IS NOT NULL AND d.score_mid IS NULL)",
    }
)
SCORE_PRESENT: Final = "d.score_mid IS NOT NULL"
SCORE_RANGE: Final[Mapping[str, str]] = MappingProxyType(
    {
        "score_mid": "d.score_mid BETWEEN :score_min AND :score_max",
        "score_low": "d.score_low BETWEEN :score_min AND :score_max",
    }
)
COVERAGE: Final = "d.coverage >= CAST(:coverage_min AS numeric) / 100"
CONFIDENCE: Final[Mapping[str, str]] = MappingProxyType(
    {"medium": "d.confidence IN ('medium','high')", "high": "d.confidence = 'high'"}
)
CONTESTED: Final[Mapping[str, str]] = MappingProxyType(
    {"only": "r.status = 'contested'", "exclude": "r.status IS DISTINCT FROM 'contested'"}
)


def clause(statement: str) -> TextClause:
    """`statement` as a text clause whose JSONB binds (`JSONB_BINDS`, those it holds) are typed JSONB."""
    return sql(statement, jsonb=[name for name in JSONB_BINDS if f":{name}" in statement])
