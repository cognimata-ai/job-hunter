"""The facets of the Jobs list's Company and Source multi-selects (spec §15.3 L5182-5183; plan M4 Task 5,
B20).

`facets(conn, q, *, window, company_q, limit)` counts, in the caller's read snapshot, the cards the list
lists for `q` (`listing.card_set`: every filter of `q`, the scoring state and the score filters included, a
group card counted once, as its representative) by company and by source, each **under every filter of `q`
but its own**: the companies ignore `q.company` and the sources ignore `q.source`, so a multi-select keeps
offering the values it can add, with the number of cards each would bring in.

- `companies`: one entry per `company_key`, its `display_name` the cards' most frequent `company_display`
  (then the first in order), the number of cards, by that number descending, then by name and key; with a
  `company_q`, only the companies whose key, or the folded display name (`lower(immutable_unaccent(…))`, the
  database's `normalize.text.fold`) of any of whose cards, starts with `fold(company_q)`. The prefix picks
  companies, not cards: a company it matches is counted, and named, over all its listed cards (Ruling
  T5-e). An empty `company_q` filters nothing.
- `sources`: one entry per source id of the cards' `source_ids`, with the number of cards that have it, by
  that number descending, then by id.

Each list holds at most `limit` entries. Both statements are static SQL composed from `jobs._sql` constants,
every value a bound parameter, and start with their tag, `/* jobs_facets.<facet> */`.
"""

from datetime import datetime
from typing import Final

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.jobs import _sql
from jobhunter.jobs.listing import card_set, read_active
from jobhunter.jobs.query import JobsQuery
from jobhunter.normalize.text import fold
from jobhunter.privacy.fences import any_fence_open

_COMPANIES_HEAD: Final = (
    "/* jobs_facets.companies */ SELECT c.company_key, "
    "mode() WITHIN GROUP (ORDER BY c.company_display) AS display_name, count(*) AS n "
    "FROM (SELECT j.company_key, j.company_display"
)
_COMPANIES_GROUP: Final = ") c GROUP BY c.company_key"
# A company matches a prefix by its key or by any of its cards' spellings, and is then counted whole
_COMPANY_PREFIX: Final = (
    " HAVING starts_with(c.company_key, :prefix) "
    "OR bool_or(starts_with(lower(immutable_unaccent(c.company_display)), :prefix))"
)
_COMPANIES_ORDER: Final = " ORDER BY n DESC, display_name, c.company_key LIMIT :limit"
_SOURCES_HEAD: Final = "/* jobs_facets.sources */ SELECT s.source_id, count(*) AS n FROM (SELECT j.source_ids"
_SOURCES_TAIL: Final = (
    ") c CROSS JOIN LATERAL unnest(c.source_ids) AS s(source_id) "
    "GROUP BY s.source_id ORDER BY n DESC, s.source_id LIMIT :limit"
)


class CompanyFacetOut(BaseModel):
    company_key: str
    display_name: str
    count: int


class SourceFacetOut(BaseModel):
    source_id: str
    count: int


# What they count: the module docstring.
class FacetsOut(BaseModel):
    """The Company and Source options of the listed cards, with their counts."""

    companies: list[CompanyFacetOut]
    sources: list[SourceFacetOut]


async def facets(
    conn: AsyncConnection,
    q: JobsQuery,
    *,
    window: tuple[datetime | None, datetime | None],
    company_q: str,
    limit: int,
) -> FacetsOut:
    """The facets of the cards of `q` (module docstring); `window` is the posted window's absolute bounds
    (`query.resolve_window`), `limit` at least 1."""
    if limit < 1:
        raise ValueError("the limit must be at least 1")
    active = await read_active(conn)
    fence_open = await any_fence_open(conn)  # once, for both sets (`listing`'s module docstring, Cards)
    by_company = card_set(
        q.model_copy(update={"company": frozenset()}), window=window, active=active, fence_open=fence_open
    )
    by_source = card_set(
        q.model_copy(update={"source": frozenset()}), window=window, active=active, fence_open=fence_open
    )
    prefix = fold(company_q)
    having = _COMPANY_PREFIX if prefix else ""
    company_sql = _COMPANIES_HEAD + by_company.sql + _COMPANIES_GROUP + having + _COMPANIES_ORDER
    companies = await conn.execute(
        _sql.clause(company_sql), {**by_company.binds, "prefix": prefix, "limit": limit}
    )
    company_out = [
        CompanyFacetOut(company_key=row.company_key, display_name=row.display_name, count=row.n)
        for row in companies
    ]
    sources = await conn.execute(
        _sql.clause(_SOURCES_HEAD + by_source.sql + _SOURCES_TAIL), {**by_source.binds, "limit": limit}
    )
    return FacetsOut(
        companies=company_out,
        sources=[SourceFacetOut(source_id=row.source_id, count=row.n) for row in sources],
    )
