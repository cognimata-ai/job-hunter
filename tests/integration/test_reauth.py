"""Re-authentication of the destructive privacy requests (spec §20.4, §16.3; plan M5 Task 18, M5-U4, D13,
Deviations 3 and 21): `api.reauth.verify_reauth` behind `POST /api/privacy/delete-everything` and `POST
/api/privacy/purge-source`.

Each needs its phrase, the password and, with TOTP on, a code of an unused step. A wrong phrase is 422
`confirmation_mismatch` and records nothing; a wrong password or code is 403 `reauth_failed` and one failed
attempt of the §16.3 throttle, committed whatever the request does next, so five of them lock the address out
(the next answer is the login's 429 `throttled`); a success records no attempt. The request's fingerprint
leaves the credentials out, and a completed request's stored response is returned before any
re-authentication, so a lost response can be retried after its TOTP step was used, with the same code or a
new one; another action under the key is 422 `idempotency_key_reused`, before re-authentication too.

The route's clock (`jobhunter.api.routes.privacy.utcnow`) is pinned to one second into a TOTP step, so a code
is that step's. Nothing runs the purges here: A0 is the route's work. Every datum is invented.
"""

import json
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import httpx
import pyotp
import pytest
import sqlalchemy as sa
from jobhunter.api.routes import privacy as privacy_routes
from jobhunter.auth import totp, users
from jobhunter.clock import utcnow
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.fixtures import TEST_PASSWORD, ConfigFactory
from tests.support.profile import idem
from tests.support.settings import fresh_key

pytestmark = pytest.mark.integration

DELETE_EVERYTHING: Final = "/api/privacy/delete-everything"
PURGE_SOURCE: Final = "/api/privacy/purge-source"
CLIENT_IP: Final = "198.51.100.20"  # `logged_in_client`'s address
JSON: Final = {"content-type": "application/json"}
_ATTEMPTS: Final = sa.text(
    "SELECT count(*) FILTER (WHERE NOT success) AS failed, count(*) AS total FROM login_attempts "
    "WHERE host(ip) = :ip"
)
_LOCKED: Final = sa.text("SELECT count(*) FROM login_lockouts WHERE scope = :scope AND locked_until > now()")
_TRACES: Final = sa.text(
    "SELECT (SELECT count(*) FROM deletion_manifests) AS manifests, "
    "(SELECT count(*) FROM api_idempotency) AS keys, (SELECT count(*) FROM purge_fences) AS fences"
)
_LAST_STEP: Final = sa.text("SELECT totp_last_step FROM users")


def delete_everything(**overrides: Any) -> dict[str, Any]:
    return {"confirm": "DELETE EVERYTHING", "password": TEST_PASSWORD, **overrides}


def purge_source(**overrides: Any) -> dict[str, Any]:
    return {"source": "adzuna", "confirm": "PURGE ADZUNA", "password": TEST_PASSWORD, **overrides}


async def post(client: httpx.AsyncClient, path: str, body: dict[str, Any], key: str) -> httpx.Response:
    return await client.post(path, json=body, headers=idem(key))


def code_of(r: httpx.Response) -> tuple[int, str]:
    return r.status_code, r.json()["code"]


async def attempts(engine: AsyncEngine) -> tuple[int, int]:
    """The failed and all login attempts of the client's address."""
    async with engine.connect() as conn:
        row = (await conn.execute(_ATTEMPTS, {"ip": CLIENT_IP})).one()
    return int(row.failed), int(row.total)


async def traces(engine: AsyncEngine) -> tuple[int, int, int]:
    """The manifests, idempotency rows and fences there are."""
    async with engine.connect() as conn:
        row = (await conn.execute(_TRACES)).one()
    return int(row.manifests), int(row.keys), int(row.fences)


async def last_step(engine: AsyncEngine) -> int | None:
    async with engine.connect() as conn:
        step = (await conn.execute(_LAST_STEP)).scalar_one()
    return None if step is None else int(step)


class TotpClock:
    """TOTP turned on for the user, and the route's clock one second into the current step."""

    def __init__(self, secret: str, start: datetime, clock: list[datetime]) -> None:
        self.secret = secret
        self.start = start
        self._clock = clock

    def code(self, steps: int = 0) -> str:
        """The code of the step `steps` after the clock's."""
        return pyotp.TOTP(self.secret).at(self._clock[0] + timedelta(seconds=30 * steps))

    def wrong_code(self) -> str:
        """A six-digit code that no step of the window accepts."""
        valid = {self.code(n) for n in range(-2, 3)}
        return next(c for c in ("000000", "111111", "222222", "333333", "444444", "555555") if c not in valid)

    def advance(self, steps: int) -> None:
        self._clock[0] += timedelta(seconds=30 * steps)

    @property
    def step(self) -> int:
        return int(self._clock[0].timestamp()) // 30


@pytest.fixture
async def totp_on(
    logged_in_client: httpx.AsyncClient,
    engine: AsyncEngine,
    make_config: ConfigFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> TotpClock:
    """TOTP enabled after the login (the session stays), the route's clock pinned into the current step."""
    secret = totp.new_secret()
    async with engine.begin() as conn:
        await users.enable_totp(conn, secret, data_key=make_config("api").secret_b64("data_key"))
    start = datetime.fromtimestamp(int(utcnow().timestamp()) // 30 * 30, UTC)
    clock = [start + timedelta(seconds=1)]
    monkeypatch.setattr(privacy_routes, "utcnow", lambda: clock[0])
    return TotpClock(secret, start, clock)


async def test_a_wrong_password_is_403_and_one_failed_attempt(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    before = await attempts(engine)
    for path, body in ((DELETE_EVERYTHING, delete_everything), (PURGE_SOURCE, purge_source)):
        r = await post(logged_in_client, path, body(password="Falsches-Passwort-42"), fresh_key("reauth"))
        assert code_of(r) == (403, "reauth_failed"), r.text
    assert await attempts(engine) == (before[0] + 2, before[1] + 2)
    assert await traces(engine) == (0, 0, 0)  # nothing was requested


async def test_a_missing_or_wrong_totp_code_when_enabled_is_403_and_counts(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, totp_on: TotpClock
) -> None:
    failed, _ = await attempts(engine)
    wrong = totp_on.wrong_code()
    for body in (delete_everything(), delete_everything(totp=""), delete_everything(totp=wrong)):
        r = await post(logged_in_client, DELETE_EVERYTHING, body, fresh_key("reauth"))
        assert code_of(r) == (403, "reauth_failed"), r.text
    assert (await attempts(engine))[0] == failed + 3
    assert await last_step(engine) is None  # no step was used
    # the right password with a right code passes
    r = await post(
        logged_in_client, DELETE_EVERYTHING, delete_everything(totp=totp_on.code()), fresh_key("reauth")
    )
    assert r.status_code == 202, r.text
    assert await last_step(engine) == totp_on.step


async def test_wrong_totp_codes_lock_the_address_out(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, totp_on: TotpClock
) -> None:
    """Each failure is committed in a transaction of its own, so it counts although its request fails: the
    fifth locks the address out, and the next request is refused before any check, the right code included,
    as the login refuses it."""
    for _ in range(5):
        r = await post(
            logged_in_client, PURGE_SOURCE, purge_source(totp=totp_on.wrong_code()), fresh_key("reauth")
        )
        assert code_of(r) == (403, "reauth_failed"), r.text
    async with engine.connect() as conn:
        assert (await conn.execute(_LOCKED, {"scope": f"ip:{CLIENT_IP}"})).scalar_one() == 1
    failed, total = await attempts(engine)
    r = await post(logged_in_client, PURGE_SOURCE, purge_source(totp=totp_on.code()), fresh_key("reauth"))
    assert code_of(r) == (429, "throttled"), r.text
    assert 880 <= r.json()["retry_after_s"] <= 900 and r.headers["retry-after"] == str(
        r.json()["retry_after_s"]
    )
    assert await attempts(engine) == (failed, total)  # the refusal recorded nothing
    assert await last_step(engine) is None and await traces(engine) == (0, 0, 0)


async def test_a_reused_totp_step_is_refused(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, totp_on: TotpClock
) -> None:
    """A code is accepted once (RFC 6238 §5.2), as at the login: a request with a new key and the step the
    first one used fails, as does an earlier step inside the window."""
    code = totp_on.code()
    r = await post(logged_in_client, PURGE_SOURCE, purge_source(totp=code), fresh_key("reauth"))
    assert r.status_code == 202, r.text
    failed, _ = await attempts(engine)
    for reused in (code, totp_on.code(-1)):
        r = await post(
            logged_in_client, DELETE_EVERYTHING, delete_everything(totp=reused), fresh_key("reauth")
        )
        assert code_of(r) == (403, "reauth_failed"), r.text
    assert (await attempts(engine))[0] == failed + 2
    assert await last_step(engine) == totp_on.step


async def test_success_records_no_attempt(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, totp_on: TotpClock
) -> None:
    """A re-authentication is no login: a success records no attempt (only the TOTP step it used)."""
    before = await attempts(engine)
    r = await post(logged_in_client, PURGE_SOURCE, purge_source(totp=totp_on.code()), fresh_key("reauth"))
    assert r.status_code == 202, r.text
    totp_on.advance(1)
    r = await post(
        logged_in_client, DELETE_EVERYTHING, delete_everything(totp=totp_on.code()), fresh_key("reauth")
    )
    assert r.status_code == 202, r.text
    assert await attempts(engine) == before


@pytest.mark.parametrize(
    ("path", "body"),
    [
        (DELETE_EVERYTHING, delete_everything(confirm="delete everything")),
        (DELETE_EVERYTHING, delete_everything(confirm="DELETE EVERYTHING ")),
        (DELETE_EVERYTHING, delete_everything(confirm="PURGE ADZUNA")),
        (PURGE_SOURCE, purge_source(confirm="PURGE  ADZUNA")),
        (PURGE_SOURCE, purge_source(confirm="DELETE EVERYTHING")),
    ],
)
async def test_a_wrong_confirm_is_422_and_records_nothing(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, path: str, body: dict[str, Any]
) -> None:
    """The phrase is checked before anything else: with a wrong password too, nothing is recorded."""
    before = await attempts(engine)
    for password in (TEST_PASSWORD, "Falsches-Passwort-42"):
        r = await post(logged_in_client, path, {**body, "password": password}, fresh_key("reauth"))
        assert code_of(r) == (422, "confirmation_mismatch"), r.text
    assert await attempts(engine) == before
    assert await traces(engine) == (0, 0, 0)


async def test_a_lost_response_retry_with_totp_returns_the_stored_202(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, totp_on: TotpClock
) -> None:
    """The response was lost; the client sends the same request again with its key, the code's step now used.
    The stored 202 comes back without a re-authentication: no failed attempt, no second purge."""
    key, body = fresh_key("reauth"), delete_everything(totp=totp_on.code())
    first = await post(logged_in_client, DELETE_EVERYTHING, body, key)
    assert first.status_code == 202, first.text
    before = await attempts(engine)
    again = await post(logged_in_client, DELETE_EVERYTHING, body, key)
    assert (again.status_code, again.json()) == (202, first.json())
    assert await attempts(engine) == before
    assert (await traces(engine))[0] == 1


async def test_a_retry_with_a_new_code_is_the_same_action(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, totp_on: TotpClock
) -> None:
    """A credential is no part of the action: the same key with a new code, or a wrong password, is the same
    request and gets the stored 202 (the new code's step is not used)."""
    key = fresh_key("reauth")
    first = await post(logged_in_client, PURGE_SOURCE, purge_source(totp=totp_on.code()), key)
    assert first.status_code == 202, first.text
    step = await last_step(engine)
    totp_on.advance(1)
    for body in (purge_source(totp=totp_on.code()), purge_source(password="Falsches-Passwort-42")):
        again = await post(logged_in_client, PURGE_SOURCE, body, key)
        assert (again.status_code, again.json()) == (202, first.json())
    assert await last_step(engine) == step
    assert (await traces(engine))[0] == 1


async def test_a_different_action_under_the_key_is_422_reused(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    """The keyed privacy requests share one key space: a key is one user action. A key that answered one of
    them, sent with another, is 422 `idempotency_key_reused` before any re-authentication, so a wrong password
    is not counted, and nothing runs."""
    key = fresh_key("reauth")
    first = await post(logged_in_client, PURGE_SOURCE, purge_source(), key)
    assert first.status_code == 202, first.text
    before = await attempts(engine)
    for password in (TEST_PASSWORD, "Falsches-Passwort-42"):
        r = await post(logged_in_client, DELETE_EVERYTHING, delete_everything(password=password), key)
        assert code_of(r) == (422, "idempotency_key_reused"), r.text
    r = await logged_in_client.post("/api/privacy/export", headers=idem(key))
    assert code_of(r) == (422, "idempotency_key_reused"), r.text
    assert await attempts(engine) == before
    assert await traces(engine) == (1, 1, 1)  # the source purge's manifest, key and fence alone


async def test_a_lone_surrogate_or_a_malformed_code_is_422_before_anything(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    """A password that is no valid Unicode text, a code that is not six digits, another source or an unknown
    field is refused by the body's validation: no Argon2 work, nothing recorded."""
    before = await attempts(engine)
    refused: list[tuple[str, dict[str, Any]]] = [
        (DELETE_EVERYTHING, delete_everything(password="Tannenbaum\ud800")),
        (DELETE_EVERYTHING, delete_everything(totp="12345")),
        (DELETE_EVERYTHING, delete_everything(totp="12345a")),
        (DELETE_EVERYTHING, {**delete_everything(), "note": "x"}),
        (PURGE_SOURCE, purge_source(source="jsearch")),
        (PURGE_SOURCE, purge_source(password="Tannenbaum\ud800")),
    ]
    for path, body in refused:
        r = await logged_in_client.post(
            path, content=json.dumps(body), headers={**JSON, **idem(fresh_key("reauth"))}
        )
        assert code_of(r) == (422, "validation_failed"), r.text
    assert await attempts(engine) == before
    assert await traces(engine) == (0, 0, 0)
