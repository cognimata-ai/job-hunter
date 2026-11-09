"""Needs attention's "Can be released from <time>" (plan M4 Task 31, review Minor 2): the SPA's
`RELEASE_AFTER_EXPIRY_MS` is the release route's wait past a lost batch's expiry.

`frontend/src/system/NeedsAttention.tsx` shows a `retrieve_failing` batch that is not releasable yet as
releasable from its `expires_at` plus that wait, the `scoring.batches.RETRIEVE_FAILING_AFTER` that
`RELEASABLE_SQL` binds. The two are held equal here, the TypeScript constant read from its source, so a change
of either fails this test instead of showing a wrong time (a backend `releasable_from` would make the constant
unneeded: carried to the final review).
"""

import re
from pathlib import Path
from typing import Final

from jobhunter.scoring import batches

REPO: Final = Path(__file__).resolve().parents[2]
ATTENTION: Final = REPO / "frontend/src/system/NeedsAttention.tsx"
DECLARATION: Final = re.compile(r"^const RELEASE_AFTER_EXPIRY_MS = ([\d_]+);$", re.MULTILINE)


def test_the_release_time_waits_as_the_route_does() -> None:
    found = DECLARATION.findall(ATTENTION.read_text(encoding="utf-8"))
    assert len(found) == 1, f"one `const RELEASE_AFTER_EXPIRY_MS = <ms>;` in {ATTENTION.name}, found {found}"
    assert int(found[0].replace("_", "")) == batches.RETRIEVE_FAILING_AFTER.total_seconds() * 1000
