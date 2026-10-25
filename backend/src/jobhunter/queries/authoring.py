"""Query authoring (spec §5.5.1, §13.3, §13.5; plan Task 20, Deviations 3 and 15): the user adds a query, or
accepts a proposal's item as one, and edits a query by retiring its row and creating a new one.

**`create_query`** inserts a `queries` row, in the caller's transaction. The text is trimmed and must have 2
to 120 characters, no NUL and no lone surrogate (neither reaches PostgreSQL); otherwise 422
`validation_failed` on `body.text`. `origin` is `llm_proposal` exactly when `proposal_id` is given, and that
proposal must exist (422 on `body.proposal_id`) and be `ready` (409 `conflict_state`); its row is read
`FOR SHARE`, so it cannot be purged under the insert. `exploration` is the caller's (an accepted item's flag,
or the user's). A live query whose `lower(text)` is the same is 409 `conflict_state` with its id
(`query_id`): the insert is `ON CONFLICT DO NOTHING` on the partial unique index `queries_text_live`, so two
concurrent creates of one text cannot both succeed. A new query is **not** enabled: enabling it is a settings
save (`queries[]`), with its quota preview (§13.5).

**`edit_query`** (§5.5.1, "retire + create"), in the caller's transaction:
1. `active_config` `FOR UPDATE`, the first lock (§9.1), and the active document in a statement of its own
   (`settings.active.lock_active`); a caller that already holds the lock (the PATCH route) makes it a no-op;
2. the query's row `FOR UPDATE`: a missing or retired one is 404 `not_found`. The new text (trimmed, checked
   as above) and language default to the row's; an edit that changes neither returns the row as it is, so its
   id and its coverage stay. Otherwise the row is retired (`retired_at := now`) and a new one is created with
   the old row's `origin`, `exploration` and `proposal_id`. Retiring first frees the text for a case-only
   edit; a text another live query holds is 409 with that query's id, and the caller's rollback undoes the
   retire;
3. when the active settings' `queries[]` names the old id, that entry is given the new id, its `enabled` flag
   kept, through `settings.save.save_settings(..., internal=True)` over the active version (Deviation 15): one
   new settings version of class `collection`, stored in its normal form, whose cells exist for the new id
   when it is enabled (`ensure_cells`). Its own `active_config FOR UPDATE` is then a no-op. A query that no
   settings version names needs no save.

Neither function logs: the route logs ids after the commit, never a text.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal, cast

from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.api.errors import FieldError, Problem
from jobhunter.db.sql import q
from jobhunter.queries import proposals
from jobhunter.settings.active import lock_active
from jobhunter.settings.save import SaveRequest, save_settings
from jobhunter.types import JSONValue
from jobhunter.worker.tasks import Deferrer

Lang = Literal["de", "en"]
Origin = Literal["llm_proposal", "user"]

TEXT_FIELD: Final = "body.text"
PROPOSAL_FIELD: Final = "body.proposal_id"
# How often an insert that met a live text retries when that row was retired before it could be read.
_INSERT_TRIES: Final = 3
_SHARE_PROPOSAL: Final = q("SELECT status FROM query_proposals WHERE id = :p FOR SHARE")
# The conflict target repeats the partial unique index `queries_text_live` exactly.
_INSERT: Final = q(
    "INSERT INTO queries (text, lang, origin, exploration, proposal_id, created_at) "
    "VALUES (:text, :lang, :origin, :exploration, :proposal_id, :now) "
    "ON CONFLICT (lower(text)) WHERE retired_at IS NULL DO NOTHING "
    "RETURNING id, text, lang, origin, exploration, proposal_id"
)
_LIVE_TEXT: Final = q("SELECT id FROM queries WHERE lower(text) = lower(:text) AND retired_at IS NULL")
_LOCK_QUERY: Final = q(
    "SELECT id, text, lang, origin, exploration, proposal_id, retired_at FROM queries "
    "WHERE id = :id FOR UPDATE"
)
_RETIRE: Final = q("UPDATE queries SET retired_at = :now WHERE id = :id AND retired_at IS NULL")


@dataclass(frozen=True)
class QueryRow:
    """A `queries` row as the api shows it (`QueryOut`)."""

    id: int
    text: str
    lang: Lang
    origin: Origin
    exploration: bool
    proposal_id: int | None

    @classmethod
    def of(cls, row: Row[tuple[object, ...]]) -> "QueryRow":
        proposal_id = row.proposal_id
        return cls(
            id=int(row.id),
            text=str(row.text),
            lang=cast(Lang, row.lang),  # the table's CHECKs hold both to these values
            origin=cast(Origin, row.origin),
            exploration=bool(row.exploration),
            proposal_id=None if proposal_id is None else int(proposal_id),
        )


def _invalid(field: str, code: str, message: str) -> Problem:
    return Problem(
        422,
        "validation_failed",
        "Request validation failed",
        errors=[FieldError(field=field, code=code, message=message)],
    )


def checked_text(text: str) -> str:
    """`text` trimmed, or 422 `validation_failed` on `body.text` (module docstring)."""
    trimmed = text.strip()
    low, high = proposals.TEXT_CHARS
    if "\x00" in trimmed:
        raise _invalid(TEXT_FIELD, "invalid", "A query text cannot contain a NUL character")
    try:
        trimmed.encode("utf-8")
    except UnicodeEncodeError:
        raise _invalid(TEXT_FIELD, "invalid", "A query text must be valid Unicode") from None
    if not low <= len(trimmed) <= high:
        raise _invalid(TEXT_FIELD, "text_length", f"A query text has {low} to {high} characters")
    return trimmed


def _duplicate(existing: int) -> Problem:
    return Problem(
        409,
        "conflict_state",
        "A live query has this text",
        detail="Another query has the same text; edit or enable that one instead.",
        extra={"query_id": existing},
    )


async def _insert(
    conn: AsyncConnection,
    *,
    text: str,
    lang: Lang,
    origin: Origin,
    exploration: bool,
    proposal_id: int | None,
    now: datetime,
) -> QueryRow:
    """The new live row, or 409 with the live query that holds its text."""
    params = {
        "text": text,
        "lang": lang,
        "origin": origin,
        "exploration": exploration,
        "proposal_id": proposal_id,
        "now": now,
    }
    for _ in range(_INSERT_TRIES):
        inserted = (await conn.execute(_INSERT, params)).first()
        if inserted is not None:
            return QueryRow.of(inserted)
        # The insert met a live row; a statement later it may have been retired, and the next insert succeeds.
        existing = (await conn.execute(_LIVE_TEXT, {"text": text})).scalar_one_or_none()
        if existing is not None:
            raise _duplicate(int(existing))
    raise RuntimeError("queries: the live row of a text kept being retired during an insert")


async def create_query(
    conn: AsyncConnection,
    *,
    text: str,
    lang: Lang,
    proposal_id: int | None = None,
    exploration: bool = False,
    now: datetime,
) -> QueryRow:
    """A new query, not enabled (module docstring)."""
    trimmed = checked_text(text)
    if proposal_id is not None:
        status = (await conn.execute(_SHARE_PROPOSAL, {"p": proposal_id})).scalar_one_or_none()
        if status is None:
            raise _invalid(PROPOSAL_FIELD, "invalid", "no such query proposal")
        if status != proposals.READY:
            raise Problem(
                409,
                "conflict_state",
                "The proposal is not ready",
                detail=f"The proposal is {status}; only the items of a ready proposal can be accepted.",
            )
    return await _insert(
        conn,
        text=trimmed,
        lang=lang,
        origin="user" if proposal_id is None else "llm_proposal",
        exploration=exploration,
        proposal_id=proposal_id,
        now=now,
    )


def _swapped(payload: JSONValue, old_id: int, new_id: int) -> dict[str, JSONValue] | None:
    """`payload` with the `queries[]` entry of `old_id` given `new_id`, or `None` when none names it."""
    if not isinstance(payload, dict) or not isinstance(entries := payload.get("queries"), list):
        raise TypeError("a settings payload holds a queries list")
    swapped: list[JSONValue] = []
    found = False
    for entry in entries:
        if isinstance(entry, dict) and entry.get("query_id") == old_id:
            entry = {**entry, "query_id": new_id}
            found = True
        swapped.append(entry)
    return {**payload, "queries": swapped} if found else None


async def edit_query(
    conn: AsyncConnection,
    query_id: int,
    *,
    text: str | None = None,
    lang: Lang | None = None,
    now: datetime,
    defer: Deferrer,
) -> QueryRow:
    """Retire `query_id` and create its successor, the row returned (module docstring); the row as it is when
    the edit changes nothing."""
    active = await lock_active(conn)  # the first lock (§9.1), then the active document
    row = (await conn.execute(_LOCK_QUERY, {"id": query_id})).first()
    if row is None or row.retired_at is not None:
        raise Problem(404, "not_found", "Query not found")
    old = QueryRow.of(row)
    new_text = old.text if text is None else checked_text(text)
    new_lang = old.lang if lang is None else lang
    if (new_text, new_lang) == (old.text, old.lang):
        return old
    await conn.execute(_RETIRE, {"id": old.id, "now": now})
    new = await _insert(
        conn,
        text=new_text,
        lang=new_lang,
        origin=old.origin,
        exploration=old.exploration,
        proposal_id=old.proposal_id,
        now=now,
    )
    swapped = _swapped(active.payload, old.id, new.id)
    if swapped is not None:
        request = SaveRequest(
            payload=swapped, base_version_id=active.settings_version_id, acknowledged_warnings=frozenset()
        )
        await save_settings(conn, request, now=now, defer=defer, internal=True)
    return new
