"""Set the dev stack's UI language, for the visual checks only (plan M4 Task 20, Global Constraints).

    uv run python scripts/dev_ui_language.py de
    uv run python scripts/dev_ui_language.py en

It signs in as the smoke user (`smoke_m0`'s login, through the canonical dev origin `http://localhost:8080`),
reads the active settings, and saves them with `display.ui_language` changed and nothing else, as the UI
saves: the dry-run first, then the save over the version it read, with every warning code of the dry-run
acknowledged and a fresh `Idempotency-Key` (`smoke_m2.save_settings`). It prints one JSON line,
`{"ui_language": <lang>, "version_no": <the saved version>}`, and never the password or the session cookie;
a failure prints a line to stderr and exits 1, a wrong argument exits 2. The precondition is a dev stack on
which `scripts/smoke_m3.py` has passed (the smoke user, a confirmed profile).
"""

import copy
import json
import sys
import uuid
from typing import Final

import httpx
from smoke_m0 import (  # scripts/ is the script's directory, the first entry of sys.path
    HTTP_TIMEOUT_S,
    ORIGIN,
    CheckFailed,
    Context,
    check_login,
    pick,
)
from smoke_m2 import json_object, save_settings

LANGUAGES: Final = ("en", "de")
USAGE: Final = "usage: dev_ui_language.py <en|de>"


def set_language(client: httpx.Client, lang: str) -> int:
    """Sign in on `client` and save the active settings with `display.ui_language` set to `lang`; the saved
    version's `version_no`."""
    if lang not in LANGUAGES:
        raise ValueError(f"{lang!r} is not one of {LANGUAGES}")
    ctx = Context(client)
    check_login(ctx)
    active = json_object(client.get(f"{ORIGIN}/api/settings", headers=ctx.auth()), 200)
    payload, base = copy.deepcopy(pick(active, "payload")), pick(active, "version", "id")
    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get("display"), dict)
        or not isinstance(base, int)
    ):
        raise CheckFailed("the active settings have no display group or version id")
    payload["display"]["ui_language"] = lang
    saved, _ = save_settings(ctx, payload, base, str(uuid.uuid4()), what=f"ui_language {lang}")
    version_no = pick(saved, "version", "version_no")
    if not isinstance(version_no, int):
        raise CheckFailed("the save answered no version_no")
    return version_no


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in LANGUAGES:
        print(USAGE, file=sys.stderr)
        return 2
    lang = sys.argv[1]
    # trust_env=False: the target is the loopback devproxy; no proxy variable of the calling shell applies.
    with httpx.Client(timeout=HTTP_TIMEOUT_S, trust_env=False) as client:
        try:
            version_no = set_language(client, lang)
        except CheckFailed as exc:
            print(f"dev_ui_language: {exc}", file=sys.stderr)
            return 1
        except (httpx.HTTPError, ValueError) as exc:  # transport, bad JSON
            print(f"dev_ui_language: {type(exc).__name__}", file=sys.stderr)
            return 1
    print(json.dumps({"ui_language": lang, "version_no": version_no}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
