"""The ranking generation (spec §7.1, §14.3): the committed counter that labels the pages of the Jobs list.

Every transaction that can change the order or the membership of the list (a job inserted, a job field that
a filter or sort reads, a current revision, an availability, a merge, a group, a target or user action, ...)
calls `bump` as its **last statement before commit**: the `ranking_state` row is the last lock of the §3.3.3
order and is held for microseconds. The list API reads the generation (`generation`) and the page rows in one
snapshot (`db.snapshot.read_snapshot`).

**The scoring-state buckets** (B30): a target state change bumps only when it moves the target to another
bucket of the Jobs list (`crosses_bucket`, by `STATE_BUCKET`); every other event of §7.1 L1255 bumps as
before. The unscored section lists a job without a target of the tuple (`None`) and every target still
waiting for its score, so a claim (`pending → in_progress`) changes nothing a page shows and does not bump;
a reclaim bumps only when a claim ends `failed_attempts_exhausted`. A writer whose moves stay within a
bucket may still bump: an extra bump only raises the banner.
"""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final, Literal

from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.db.sql import q

Bucket = Literal["unscored", "grouped", "scored", "filtered", "failed", "expired", "hidden"]

# Each state of the `score_targets.state` CHECK, and `None` (no target of the tuple), by its bucket of the
# Jobs list: `deferred_duplicate` is `grouped` because the row leaves the top level for its representative's.
STATE_BUCKET: Final[Mapping[str | None, Bucket]] = MappingProxyType(
    {
        None: "unscored",
        "pending": "unscored",
        "budget_deferred": "unscored",
        "in_progress": "unscored",
        "in_batch": "unscored",
        "deferred_duplicate": "grouped",
        "published_accepted": "scored",
        "published_contested": "scored",
        "filtered_out": "filtered",
        "refused": "failed",
        "failed_max_tokens": "failed",
        "failed_invalid_output": "failed",
        "failed_request": "failed",
        "failed_transient": "failed",
        "failed_attempts_exhausted": "failed",
        "expired_unscored": "expired",
        "cancelled": "hidden",
    }
)

_BUMP: Final = q("UPDATE ranking_state SET generation = generation + 1, updated_at = now() WHERE id = 1")
_GENERATION: Final = q("SELECT generation FROM ranking_state WHERE id = 1")
_NO_ROW: Final = "ranking_state has no row 1 (migrate seeds it)"


async def bump(conn: AsyncConnection) -> None:
    """Advance the ranking generation by one in the caller's transaction (its last statement).

    Raises `LookupError` when the `ranking_state` row is missing (`migrate` seeds it).
    """
    if (await conn.execute(_BUMP)).rowcount != 1:
        raise LookupError(_NO_ROW)


async def generation(conn: AsyncConnection) -> int:
    """The ranking generation as the caller's transaction sees it (a list page reads it in its snapshot).

    Raises `LookupError` when the `ranking_state` row is missing (`migrate` seeds it).
    """
    value = (await conn.execute(_GENERATION)).scalar_one_or_none()
    if value is None:
        raise LookupError(_NO_ROW)
    return int(value)


def crosses_bucket(from_state: str | None, to_state: str | None) -> bool:
    """Whether a target moving `from_state → to_state` changes its bucket (`STATE_BUCKET`), the only target
    state change that bumps (module docstring). Raises `KeyError` for a state outside the CHECK."""
    return STATE_BUCKET[from_state] != STATE_BUCKET[to_state]
