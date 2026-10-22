"""A lone UTF-16 surrogate in the body of an idempotent POST is never a 500 (Task 17, fix round 2).

A JSON body may carry one as an escape (`"\\ud800"`); no UTF-8 text holds it. `request_fingerprint`, which
every idempotent route runs, encodes it instead of raising, so the request reaches the validation that refuses
it. The settings save, the one idempotent route whose fingerprint holds free text, is pinned in
`test_settings_save.py`; here the other idempotent routes with a body answer the same bytes without a 500.
"""

import json
from typing import Any, Final

import pytest

from tests.support.profile import idem
from tests.support.settings import fresh_key

pytestmark = pytest.mark.integration

JSON: Final = {"content-type": "application/json"}
SURROGATE: Final = "\ud800"


@pytest.mark.parametrize(
    ("path", "body", "status"),
    [
        ("/api/profile/drafts/1/retry", {"mode": f"llm{SURROGATE}"}, 422),
        ("/api/profile/drafts/1/retry", {"mode": "llm", f"note{SURROGATE}": "x"}, 422),  # extra="forbid"
        ("/api/runs", {"include_paid": SURROGATE}, 422),
        ("/api/runs", {"include_paid": False, f"x{SURROGATE}": 1}, 202),  # M0's body ignores unknown keys
    ],
)
async def test_a_lone_surrogate_is_never_a_500(
    logged_in_client, path: str, body: dict[str, Any], status: int
) -> None:
    r = await logged_in_client.post(path, content=json.dumps(body), headers={**JSON, **idem(fresh_key())})
    assert r.status_code == status, (r.status_code, r.text)
    if status == 422:
        assert r.json()["code"] == "validation_failed"
