"""Label mode's excerpt length (plan M4 Task 26, review Minor 1): the SPA's `EXCERPT_CHARS` is the queue's.

`frontend/src/jobs/LabelCard.tsx` ends an excerpt in an ellipsis when it is as long as the server cuts it
(`jobs.labelling.EXCERPT_CHARS` code points, the `left(description_text, :chars)` of the items read). The
two constants are held equal here, read from the TypeScript source, so a change of either fails this test
instead of silently misplacing the ellipsis.
"""

import re
from pathlib import Path
from typing import Final

from jobhunter.jobs import labelling

REPO: Final = Path(__file__).resolve().parents[2]
LABEL_CARD: Final = REPO / "frontend/src/jobs/LabelCard.tsx"
DECLARATION: Final = re.compile(r"^export const EXCERPT_CHARS = (\d+);$", re.MULTILINE)


def test_the_label_card_cuts_where_the_queue_does() -> None:
    found = DECLARATION.findall(LABEL_CARD.read_text(encoding="utf-8"))
    assert len(found) == 1, f"one `export const EXCERPT_CHARS = <n>;` in {LABEL_CARD.name}, found {found}"
    assert int(found[0]) == labelling.EXCERPT_CHARS
