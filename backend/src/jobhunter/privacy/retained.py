"""What §20.1 keeps, as `privacy.retention`'s rules and the profile purge (`privacy.kinds.profile_purge`) both
read it (plan M5 Task 14): the score history's window and the states it ends in, and the profile versions
kept whole. Retention imports the purge kinds, so these live apart from both.

- `months_before(t, months)`: the cutoff of `retention.score_history_months`, calendar months back;
- `ENDED_TARGET_STATES`: §20.1's "ended terminally". The `score_history` rule deletes the targets of an
  inactive tuple that ended so before the cutoff, and a profile version is eligible for its purge once every
  target of its tuples did;
- `NEWEST_AND_PREVIOUS`: the user's newest confirmed version and the previous one, whose CV files the
  `cv_files` rule keeps and which the profile purge never takes (D11).
"""

import calendar
from datetime import datetime
from typing import Final

from jobhunter.scoring import targets

ENDED_TARGET_STATES: Final = (
    "published_accepted",
    "published_contested",
    *targets.RETRYABLE_STATES,
    "expired_unscored",
    "cancelled",
)
# The user's newest confirmed draft's version (`newest.version`) and the previous one (`previous.version`):
# the newest confirmed draft before it that names another version. The draft ids are the confirm order; the
# adversarial seed's drafts name synthetic versions and never count (Ruling T19-a). Common table expressions
# for a `WITH`.
NEWEST_AND_PREVIOUS: Final = (
    "newest AS (SELECT d.id, d.confirmed_profile_version_id AS version FROM profile_drafts d "
    "JOIN profile_versions v ON v.id = d.confirmed_profile_version_id AND NOT v.synthetic_eval "
    "WHERE d.status = 'confirmed' ORDER BY d.id DESC LIMIT 1), "
    "previous AS (SELECT d.confirmed_profile_version_id AS version FROM profile_drafts d JOIN newest n "
    "ON d.id < n.id AND d.confirmed_profile_version_id <> n.version "
    "JOIN profile_versions v ON v.id = d.confirmed_profile_version_id AND NOT v.synthetic_eval "
    "WHERE d.status = 'confirmed' ORDER BY d.id DESC LIMIT 1)"
)


def months_before(t: datetime, months: int) -> datetime:
    """`t` moved back `months` calendar months, its day clamped to the last of a shorter month (31 March less
    one month is the last day of February)."""
    year, month = divmod(t.year * 12 + t.month - 1 - months, 12)
    month += 1
    return t.replace(year=year, month=month, day=min(t.day, calendar.monthrange(year, month)[1]))
