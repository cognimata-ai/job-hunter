"""The backup container's cron expressions (spec §19.2, §19.5; plan M5 Tasks 24 and 26, R15, Ruling T26-1 (a)
T24-4 M7): the one validator that `env_valid` (`config.load_config`) and the scheduler (`backup.scheduler`)
share for every backup cron, and a job's next tick.

A backup cron is five fields that croniter reads (six would be read as seconds). The restore test's ticks
count only on a day of the month of at most `FIRST_WEEK_LAST_DAY` (its weekly default, `30 3 * * 0`, runs on
the first Sunday of each month), so its cron must also fire on such a day: `next_tick` searches at most
`SEARCH_MONTHS` months for one (each step jumps to the next month's first week), and a cron that never fires
there (`30 3 15 * *`, a yearly `0 0 29 2 *`) is refused at the start of the container rather than by a
scheduler that would stop at its first job. `cron_refusal` says why an expression is no backup cron; the
configuration turns it into `env_valid`, the scheduler into `ValueError`.
"""

import calendar
from datetime import UTC, datetime, timedelta
from typing import Final

from croniter import croniter

CRON_FIELDS: Final = 5
# R15: the restore test runs on the first Sunday of the month, the day of its weekly cron that is at most 7.
FIRST_WEEK_LAST_DAY: Final = 7
# How far `next_tick` looks for a tick in a first week before it gives up on the cron.
SEARCH_MONTHS: Final = 24
# Where `cron_refusal` starts its search: a cron of five fields repeats within a few years.
_REFERENCE: Final = datetime(2001, 1, 1, tzinfo=UTC)


def _first_of_next_month(moment: datetime) -> datetime:
    days = calendar.monthrange(moment.year, moment.month)[1]
    last_day = moment.replace(day=days, hour=0, minute=0, second=0, microsecond=0)
    return last_day + timedelta(days=1)


def next_tick(expression: str, *, after: datetime, first_week: bool) -> datetime:
    """The first tick of `expression` strictly after `after` (UTC); with `first_week`, the first on a day of
    the month of at most `FIRST_WEEK_LAST_DAY`. `ValueError` when it has none in `SEARCH_MONTHS` months."""
    start = after
    for _ in range(SEARCH_MONTHS):
        fire: datetime = croniter(expression, start).get_next(datetime)
        if not first_week or fire.day <= FIRST_WEEK_LAST_DAY:
            return fire
        start = _first_of_next_month(fire) - timedelta(seconds=1)  # the next month's first week
    raise ValueError("the cron never fires in the first seven days of a month")


def cron_refusal(expression: str, *, first_week: bool) -> str | None:
    """Why `expression` is no backup cron (the reason, to follow the variable's or the job's name), or `None`
    for one that is (module docstring)."""
    if len(expression.split()) != CRON_FIELDS or not croniter.is_valid(expression):
        return "must be a cron expression of five fields"
    if first_week:
        try:
            next_tick(expression, after=_REFERENCE, first_week=True)
        except ValueError:
            return "must fire in the first seven days of a month"
    return None
