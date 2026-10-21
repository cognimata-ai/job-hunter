"""The semantic limits of the profile (spec §12.4), as typed constants.

The structural parse (`llm/schemas/profile.py`) carries no limit; `profile/validate.py` checks these after it
and reports an excess as a `limit` issue, never by truncating. A pair is `(lowest, highest)` and both bounds
are inclusive; `None` is no bound on that side. `EDUCATION_YEAR`'s upper bound is the current year, which the
validator takes from the `today` it is given.

`LIMITS` is the §12.4 table as one read-only mapping from constant name to value. The user-entered block has
no row in that table; its two bounds are below it and are not part of `LIMITS`.
"""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

SUMMARY_MAX: Final = 1200
TARGET_TITLES: Final[tuple[int, int]] = (1, 12)
ROLES_MAX: Final = 30
HIGHLIGHTS_MAX: Final = 8
HIGHLIGHT_CHARS: Final = 300
SKILLS_MAX: Final = 120
SKILL_YEARS: Final[tuple[int, int]] = (0, 50)
LANGUAGES_MAX: Final = 10
CODE_OTHER_CHARS: Final = 40
EDUCATION_MAX: Final = 10
EDUCATION_YEAR: Final[tuple[int, int | None]] = (1950, None)
CERTIFICATIONS_MAX: Final = 30
DOMAINS_MAX: Final = 20
TOTAL_YEARS: Final[tuple[int, int]] = (0, 60)

LIMITS: Final[Mapping[str, int | tuple[int, int] | tuple[int, int | None]]] = MappingProxyType(
    {
        "SUMMARY_MAX": SUMMARY_MAX,
        "TARGET_TITLES": TARGET_TITLES,
        "ROLES_MAX": ROLES_MAX,
        "HIGHLIGHTS_MAX": HIGHLIGHTS_MAX,
        "HIGHLIGHT_CHARS": HIGHLIGHT_CHARS,
        "SKILLS_MAX": SKILLS_MAX,
        "SKILL_YEARS": SKILL_YEARS,
        "LANGUAGES_MAX": LANGUAGES_MAX,
        "CODE_OTHER_CHARS": CODE_OTHER_CHARS,
        "EDUCATION_MAX": EDUCATION_MAX,
        "EDUCATION_YEAR": EDUCATION_YEAR,
        "CERTIFICATIONS_MAX": CERTIFICATIONS_MAX,
        "DOMAINS_MAX": DOMAINS_MAX,
        "TOTAL_YEARS": TOTAL_YEARS,
    }
)

# `user_entered` (§12.4) has no limits in the spec. These bound what the review form can put into the profile
# JSON: a clearance name or level is as long as the ad side's `security_clearance.level` (§11.4: 60
# characters), and a profile holds as many clearances as it has languages.
CLEARANCES_MAX: Final = 10
CLEARANCE_CHARS: Final = 60
