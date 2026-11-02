"""The static SQL of the Jobs list engine (`jobs.listing`, which composes it; plan M4 Task 4, §16.7).

Every string here is a constant. A statement is a choice among them, joined in a fixed order: no value, sort
name or variant name is ever interpolated, and every value is a bound parameter. The aliases are those of a
card's score row: `j` the job, `t` its active-tuple target, `r` the published result, `d` the current
instance, `h` the hard-filter result, `u` the user's actions, and `m` a deferred group member.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from jobhunter.db.sql import q as sql
from jobhunter.jobs.query import ScoringBucket, UserStatus

ACTIVE: Final = sql("SELECT llm_tuple_id, scope_version_id FROM active_config WHERE id = 1")

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
# ... from the current instance (the scored section and the not-a-job-ad cards)
INSTANCE_FROM: Final = (
    " FROM det_results d"
    " JOIN llm_results r ON r.id = d.llm_result_id"
    " JOIN score_targets t ON t.published_result_id = r.id AND t.llm_tuple_id = :tuple"
    " JOIN jobs j ON j.id = d.job_id AND j.current_revision_id = t.job_revision_id"
    " LEFT JOIN hard_filter_results h ON h.job_id = j.id AND h.scope_version_id = :scope"
    " LEFT JOIN user_job_actions u ON u.job_id = j.id"
)
# ... from an unscored target (`score_targets_unscored_rank`: its predicate, verbatim)
TARGET_FROM: Final = (
    " FROM score_targets t"
    " JOIN jobs j ON j.id = t.job_id AND j.current_revision_id = t.job_revision_id"
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
    }
)

# The non-score predicates P(x) of a job `x`: the card's representative `j` or a deferred member `m`. The
# keyword matches the full text of the job's current revision in both configurations or its title by word
# similarity (B4: `<%`, the threshold 0.6): two indexed branches, each its own set. The full-text branch is a
# set of revision ids that the current revision is looked up in: joined to `jobs` inside the set, the
# mergeable `current_revision_id = kr.id` would make the planner read the revisions in `job_revisions_pkey`
# order instead of through `job_revisions_tsv` (P6).
TEXT_REVISIONS: Final = (
    "SELECT kr.id FROM job_revisions kr WHERE kr.tsv @@ websearch_to_tsquery('english', :q) "
    "OR kr.tsv @@ websearch_to_tsquery('german_unaccent', :q)"
)
TITLE_JOBS: Final = "SELECT kt.id FROM jobs kt WHERE :qf <% kt.title_norm"
ROW_TEMPLATES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "posted_after": "coalesce({x}.posted_at_effective, {x}.first_seen_at) >= :posted_after",
        "posted_before": "coalesce({x}.posted_at_effective, {x}.first_seen_at) < :posted_before",
        "keyword": "({x}.current_revision_id IN (" + TEXT_REVISIONS + ") OR {x}.id IN (" + TITLE_JOBS + "))",
        "near": "(job_within_km({x}.locations, CAST(:lat AS float8), CAST(:lon AS float8), "
        "CAST(:radius_km AS float8)) AND {x}.location_kind <> 'country_wide')",
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
DEFERRED_MEMBER: Final = (
    "(j.duplicate_group_id IS NOT NULL AND EXISTS (SELECT 1 FROM duplicate_groups g "
    "JOIN jobs m ON m.duplicate_group_id = g.id "
    "JOIN score_targets tm ON tm.job_revision_id = m.current_revision_id AND tm.llm_tuple_id = :tuple "
    "AND tm.state = 'deferred_duplicate' "
    "WHERE g.id = j.duplicate_group_id AND g.status = 'active' AND g.representative_job_id = j.id "
    "AND m.id <> j.id AND m.merged_into_job_id IS NULL AND "
)

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
