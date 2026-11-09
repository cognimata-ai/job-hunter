"""A job's outbound links (spec §8.8 L3362, §15.3 L5198; plan M4 Task 5, B10).

`outbound_links(conn, job_ids)` reads, in one statement for all of `job_ids`, each job's two links:

- `open_ad`, the link that opens the ad, by §8.8's precedence: the employer ATS link, then a publisher's
  primary link, then Adzuna's `redirect_url`. Each posting of the job offers one candidate: its latest
  observation's `primary_link` (`NormalizedPosting.primary_link`), or, for an Adzuna posting (whose
  observations carry none), its `outbound_url_private`. The candidates are ranked by their source's
  `precedence_rank` (the ATS boards 1, JSearch and SerpApi 2, Arbeitnow 3, Adzuna 4), then by the most recent
  observation, then by the lowest posting id (§8.8's ties);
- `adzuna`, the job's Adzuna `outbound_url_private` (the attribution badge links to it, §15.3), of the
  Adzuna posting observed last (then the lowest id).

Only an absolute `http`/`https` URL is a link: any other value a source sent (a `javascript:` URL, a relative
path) is passed over, as if the posting had none, so a page never renders it as an `href`. A job of
`job_ids` without a link has `OutboundLinks(None, None)`; an id that names no job too.

Both links are private-ish (an Adzuna redirect carries the app's tracking): neither is ever logged, put in a
metric or in an error body. `observability.redact` redacts the keys `open_ad`, `primary_link`,
`redirect_url` and `outbound_url_private` should one reach a log event.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.db.sql import q

# An absolute http(s) URL with a host (case-insensitive), the only kind of link a card offers
HTTP_URL: Final = "'^https?://[^/?#\\s]'"
# The candidate link of a posting `p` of the source instance `i`, `o` its latest observation (module
# docstring); a link only when it matches `HTTP_URL`. The drawer's postings (M4 Task 6) offer it as well.
POSTING_LINK: Final = (
    "coalesce(o.normalized ->> 'primary_link', "
    "CASE WHEN i.source_id = 'adzuna' THEN p.outbound_url_private END)"
)
_LINKS: Final = q(
    "/* jobs_cards.links */ SELECT x.job_id, ad.link AS open_ad, az.link AS adzuna "  # noqa: S608 (constants)
    "FROM unnest(CAST(:ids AS bigint[])) AS x(job_id) "
    "LEFT JOIN LATERAL (SELECT c.link FROM postings p "
    "JOIN source_instances i ON i.id = p.source_instance_id "
    "JOIN sources s ON s.id = i.source_id "
    "LEFT JOIN posting_observations o ON o.id = p.latest_observation_id "
    f"CROSS JOIN LATERAL (SELECT {POSTING_LINK} AS link) c "
    f"WHERE p.job_id = x.job_id AND c.link ~* {HTTP_URL} "
    "ORDER BY s.precedence_rank, o.observed_at DESC NULLS LAST, p.id LIMIT 1) ad ON true "
    "LEFT JOIN LATERAL (SELECT p.outbound_url_private AS link FROM postings p "
    "JOIN source_instances i ON i.id = p.source_instance_id "
    "LEFT JOIN posting_observations o ON o.id = p.latest_observation_id "
    f"WHERE p.job_id = x.job_id AND i.source_id = 'adzuna' AND p.outbound_url_private ~* {HTTP_URL} "
    "ORDER BY o.observed_at DESC NULLS LAST, p.id LIMIT 1) az ON true"
)


@dataclass(frozen=True)
class OutboundLinks:
    """The link that opens a job's ad and its Adzuna attribution link (module docstring); never logged."""

    open_ad: str | None
    adzuna: str | None


async def outbound_links(conn: AsyncConnection, job_ids: Sequence[int]) -> dict[int, OutboundLinks]:
    """The outbound links of every job of `job_ids`, by job id, in one statement (module docstring)."""
    ids = sorted(set(job_ids))
    if not ids:
        return {}
    rows = await conn.execute(_LINKS, {"ids": ids})
    return {int(row.job_id): OutboundLinks(open_ad=row.open_ad, adzuna=row.adzuna) for row in rows}
