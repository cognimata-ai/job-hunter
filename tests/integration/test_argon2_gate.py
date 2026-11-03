"""The login's Argon2 admission gate (ruling T21-e): 2 verifications at once, 8 waiting, then 429.

The verifications are held by the test (`Verifier`, patched over the route's `check_password` and
`dummy_verify`): each one counts itself and waits until the test releases it. The gate is the real one; it
also tells the test when a request has arrived at it (`ObservedGate`). The test waits for those signals,
never for time.
"""

import asyncio
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx
import pytest
import sqlalchemy as sa
from jobhunter.api.app import JobHunterApp
from jobhunter.api.argon2_gate import QUEUE, SLOTS, Argon2Gate

pytestmark = pytest.mark.integration
PW = "Tannenbaum-Ölfass-7"
# A safety net only: a broken gate fails the test after this long instead of hanging it.
WAIT_S = 20.0


class Progress:
    """Wakes the test whenever a verification starts or a request reaches the gate."""

    def __init__(self) -> None:
        self._changed = asyncio.Event()

    def changed(self) -> None:
        self._changed.set()

    async def until(self, condition: Callable[[], bool]) -> None:
        async with asyncio.timeout(WAIT_S):
            while not condition():
                await self._changed.wait()
                self._changed.clear()


class Verifier:
    """Stands in for one Argon2 verification: counts itself, then waits until the test releases it."""

    def __init__(self, loop: asyncio.AbstractEventLoop, changed: Callable[[], None]) -> None:
        self._lock = threading.Lock()
        self._loop = loop
        self._changed = changed
        self.inside = 0
        self.peak = 0
        self.entries = 0
        self.released = threading.Event()

    def _verify(self) -> None:
        with self._lock:
            self.inside += 1
            self.entries += 1
            self.peak = max(self.peak, self.inside)
        self._loop.call_soon_threadsafe(self._changed)
        try:
            if not self.released.wait(WAIT_S):
                raise TimeoutError("the test never released the verification")
        finally:
            with self._lock:
                self.inside -= 1

    def check_password(self, stored: str, p: str) -> tuple[bool, bool]:
        self._verify()
        return False, False

    def dummy_verify(self, p: str) -> None:
        self._verify()


class ObservedGate(Argon2Gate):
    """The real gate, which also counts and signals each request that arrives at it."""

    def __init__(self, changed: Callable[[], None]) -> None:
        super().__init__()
        self._changed = changed
        self.arrivals = 0

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        self.arrivals += 1
        self._changed()
        async with super().slot():
            yield


@dataclass
class Gated:
    app: JobHunterApp
    progress: Progress
    verifier: Verifier
    gate: ObservedGate

    async def login(self, ip: str, username: str = "ada", totp: str | None = None) -> httpx.Response:
        transport = httpx.ASGITransport(app=self.app, raise_app_exceptions=False, client=(ip, 50000))
        body = {"username": username, "password": "wrong-password-123"}
        if totp is not None:
            body["totp"] = totp
        async with httpx.AsyncClient(
            transport=transport, base_url="https://testserver", headers={"Origin": "https://testserver"}
        ) as client:
            return await client.post("/api/auth/login", json=body)

    def saturated(self, arrivals: int) -> bool:
        """`arrivals` requests reached the gate and two of them verify; or, wrongly, a third verifies."""
        return self.verifier.entries > SLOTS or (
            self.gate.arrivals == arrivals and self.verifier.entries == SLOTS
        )


@pytest.fixture
async def gated(api_app: JobHunterApp, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Gated]:
    import jobhunter.api.routes.auth as auth_routes

    progress = Progress()
    verifier = Verifier(asyncio.get_running_loop(), progress.changed)
    gate = ObservedGate(progress.changed)
    api_app.api.state.argon2_gate = gate
    monkeypatch.setattr(auth_routes, "check_password", verifier.check_password)
    monkeypatch.setattr(auth_routes, "dummy_verify", verifier.dummy_verify)
    try:
        yield Gated(api_app, progress, verifier, gate)
    finally:
        verifier.released.set()  # no verification thread outlives its test


async def _open_transactions(engine) -> int:
    async with engine.connect() as c:
        return int(
            (
                await c.execute(
                    sa.text(
                        "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                        "AND state LIKE 'idle in transaction%'"
                    )
                )
            ).scalar_one()
        )


async def test_concurrent_logins_never_run_more_than_two_verifications_at_once(
    gated, make_user, engine
) -> None:
    await make_user()
    n = SLOTS + QUEUE  # every one of them is admitted
    # Unknown users and wrong passwords alike, each from an address of its own (no lockout).
    tasks = [
        asyncio.create_task(gated.login(f"198.51.100.{100 + i}", "ada" if i % 2 else "nobody"))
        for i in range(n)
    ]
    try:
        await gated.progress.until(lambda: gated.saturated(n))
        assert (gated.verifier.peak, gated.gate.admitted) == (SLOTS, n)  # two verify, eight wait
        assert await _open_transactions(engine) == 0  # no transaction is held while hashing
        gated.verifier.released.set()
        answers = await asyncio.gather(*tasks)
    finally:
        gated.verifier.released.set()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert [r.status_code for r in answers] == [401] * n
    # Once admitted, an unknown user and a wrong password cost one verification each.
    assert (gated.verifier.entries, gated.verifier.peak, gated.gate.admitted) == (n, SLOTS, 0)


async def test_a_login_beyond_the_queue_gets_429_without_a_verification(gated, make_user, engine) -> None:
    await make_user()
    n = SLOTS + QUEUE
    tasks = [asyncio.create_task(gated.login(f"198.51.100.{100 + i}")) for i in range(n)]
    try:
        await gated.progress.until(lambda: gated.saturated(n))
        assert gated.verifier.entries == SLOTS
        beyond = asyncio.create_task(gated.login("198.51.100.200"))  # it would be the ninth waiter
        await gated.progress.until(lambda: beyond.done() or gated.verifier.entries > SLOTS)
        assert beyond.done(), "the login beyond the queue reached Argon2"
        r = beyond.result()
        gated.verifier.released.set()
        answers = await asyncio.gather(*tasks)
    finally:
        gated.verifier.released.set()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert r.status_code == 429 and r.headers["retry-after"] == "1"
    assert (r.json()["code"], r.json()["retry_after_s"]) == ("throttled", 1)
    assert r.headers["content-type"] == "application/problem+json"
    assert [a.status_code for a in answers] == [401] * n and gated.verifier.entries == n
    async with engine.connect() as c:  # the refused login was not an attempt
        ips = (await c.execute(sa.text("SELECT host(ip) FROM login_attempts"))).scalars().all()
    assert len(ips) == n and "198.51.100.200" not in ips


async def test_a_queued_login_rechecks_the_lockout_once_it_has_a_slot(gated, make_user, engine) -> None:
    await make_user()
    holders = [asyncio.create_task(gated.login(ip)) for ip in ("198.51.100.101", "198.51.100.102")]
    queued: asyncio.Task[httpx.Response] | None = None
    try:
        await gated.progress.until(lambda: gated.verifier.entries == SLOTS)  # both hold a slot and verify
        queued = asyncio.create_task(gated.login("198.51.100.103"))
        await gated.progress.until(lambda: gated.gate.arrivals == SLOTS + 1 or gated.verifier.entries > SLOTS)
        assert gated.verifier.entries == SLOTS, "the queued login did not wait for a slot"
        async with engine.begin() as c:  # the burst ahead of it has locked its address out meanwhile
            await c.execute(
                sa.text(
                    "INSERT INTO login_lockouts "
                    "VALUES ('ip:198.51.100.103', now() + interval '10 minutes', 'ip_failures')"
                )
            )
        gated.verifier.released.set()
        r = await queued
        answers = await asyncio.gather(*holders)
    finally:
        gated.verifier.released.set()
        await asyncio.gather(*holders, *([queued] if queued else []), return_exceptions=True)
    assert r.status_code == 429 and r.json()["title"] == "Too many failed logins"
    assert 0 < r.json()["retry_after_s"] <= 900  # the ten minutes of that lockout, from the login's own clock
    assert gated.verifier.entries == SLOTS  # the queued login never verified
    assert [a.status_code for a in answers] == [401, 401]


async def _enable_totp(engine, make_config) -> None:
    from jobhunter.auth import totp, users

    async with engine.begin() as c:
        await users.enable_totp(c, totp.new_secret(), data_key=make_config("api").secret_b64("data_key"))


async def test_a_login_without_a_totp_code_never_reaches_the_gate(
    gated, make_user, engine, make_config
) -> None:
    await make_user()
    await _enable_totp(engine, make_config)
    gated.verifier.released.set()  # nothing is held here: a verification that runs ends at once
    r = await gated.login("198.51.100.101")
    assert (r.status_code, r.json()["code"]) == (401, "totp_required")
    assert (gated.gate.arrivals, gated.verifier.entries) == (0, 0)
    coded = await gated.login("198.51.100.101", totp="000000")  # the same login with a code goes through it
    assert (coded.status_code, coded.json()["code"]) == (401, "unauthenticated")
    assert (gated.gate.arrivals, gated.verifier.entries) == (1, 1)


async def test_a_saturated_gate_still_answers_a_login_without_a_code_with_totp_required(
    gated, make_user, engine, make_config
) -> None:
    await make_user()
    await _enable_totp(engine, make_config)
    n = SLOTS + QUEUE
    tasks = [asyncio.create_task(gated.login(f"198.51.100.{100 + i}", totp="000000")) for i in range(n)]
    try:
        await gated.progress.until(lambda: gated.saturated(n))  # two verify, eight wait: the gate is full
        async with asyncio.timeout(WAIT_S):
            r = await gated.login("198.51.100.200")  # no code: past the gate it would get 429 at once
        arrivals = gated.gate.arrivals
        gated.verifier.released.set()
        answers = await asyncio.gather(*tasks)
    finally:
        gated.verifier.released.set()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert (r.status_code, r.json()["code"]) == (401, "totp_required")
    assert arrivals == n  # the code-less login was not one of the gate's arrivals
    assert [a.status_code for a in answers] == [401] * n and gated.verifier.entries == n
