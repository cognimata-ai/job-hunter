"""The CV paste's limits (plan M4 Task 27, fix round 1, Ruling T27-g): the SPA's are the server's.

`frontend/src/settings/cv/CvUpload.tsx` counts a pasted CV as `profile.upload.check_paste` does (code points,
NUL characters dropped) and refuses one outside `PASTE_MIN` to `PASTE_MAX` before sending it. The two
constants of `frontend/src/settings/cv/api.ts` are held equal to the server's here, read from the TypeScript
source, so a change of either fails this test instead of letting the meter promise what the server refuses.
"""

import re
from pathlib import Path
from typing import Final

import pytest
from jobhunter.profile import upload

REPO: Final = Path(__file__).resolve().parents[2]
CV_API: Final = REPO / "frontend/src/settings/cv/api.ts"


def declared(name: str) -> list[int]:
    """Each `export const <name> = <n>;` of the module (digits with or without `_` separators)."""
    pattern = re.compile(rf"^export const {name} = (\d[\d_]*);$", re.MULTILINE)
    return [int(value.replace("_", "")) for value in pattern.findall(CV_API.read_text(encoding="utf-8"))]


@pytest.mark.parametrize(
    ("name", "server"), [("PASTE_MIN", upload.PASTE_MIN), ("PASTE_MAX", upload.PASTE_MAX)]
)
def test_the_paste_limits_are_the_server_s(name: str, server: int) -> None:
    found = declared(name)
    assert len(found) == 1, f"one `export const {name} = <n>;` in {CV_API.name}, found {found}"
    assert found[0] == server
