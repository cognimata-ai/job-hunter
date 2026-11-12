"""A site of the compose test stack driven as a user drives it (plan M5 Tasks 26-28): the browser `Site`, the
stack modules' dummy user, the inputs of a collection on the fixtures, and the readers of what a command
printed and logged. tests/stack/test_prod_restore.py and tests/stack/test_prod_drills.py share it (Task 27's
carry T27-3 (1): no test module imports another).

- `Site(argv)` is a TLS client of the site's Caddy (`https://localhost:8443`, verified against the site's
  own internal root), logged in as `USERNAME`; it collects on the fixtures, confirms the synthetic profile
  from the recorded Claude streams and makes an export `ready`, through the API alone.
- `wait_for` polls a probe every `POLL_S` within a bound, `one` reads one value as the bootstrap role,
  `last_json` the JSON line a command printed last, `events` the JSON log events it wrote, `named` those of
  one name; `backup` runs a `jobhunter backup` command in the site's backup container and `in_worker` a script
  in its worker container, each returning what it printed.

No paid or external call: the worker is on the fixtures and the recorded Claude streams. The password below
is the stack modules' own dummy user's, no secret.
"""

import json
import ssl
import time
from collections.abc import Callable
from typing import Any, Final

import httpx
import pytest

from tests.support.prodstack import ORIGIN, PT, caddy_root_pem, logs_tail, psql, run
from tests.support.profile import SYNTHETIC_CV, minimal_pdf

USERNAME: Final = "restoretest"
PASSWORD: Final = "Messingglocke-Lindenblatt-9"  # the stack modules' own dummy user's
POLL_S: Final = 2.0
TERMINAL: Final = frozenset({"succeeded", "partial", "failed", "cancelled"})
# The inputs of a collection (smoke_m1's, plan Deviation 3): a query and the Greenhouse board on the fixtures,
# both enabled in the active settings version.
SEED_INPUTS: Final = """
WITH q AS (INSERT INTO queries (text, lang, origin) VALUES ('software engineer', 'en', 'user') RETURNING id),
c AS (INSERT INTO watchlist_companies (display_name, company_key, ats_type, board_slug, detection,
  validation_status, validated_at) VALUES ('getyourguide', 'getyourguide', 'greenhouse', 'getyourguide',
  'manual', 'ok', now()) RETURNING id),
i AS (INSERT INTO source_instances (source_id, instance_key, watchlist_company_id)
  SELECT 'greenhouse', 'getyourguide', c.id FROM c RETURNING id),
s AS (UPDATE settings_versions v SET payload = jsonb_set(jsonb_set(v.payload, '{queries}',
  v.payload->'queries' || jsonb_build_array(jsonb_build_object('query_id', (SELECT id FROM q), 'enabled',
  true))), '{watchlist}', v.payload->'watchlist' || jsonb_build_array(jsonb_build_object(
  'watchlist_company_id', (SELECT id FROM c),
  'enabled', true))) FROM active_config a WHERE a.id = 1 AND v.id = a.settings_version_id RETURNING v.id)
SELECT (SELECT id FROM q), (SELECT id FROM i), (SELECT id FROM s)
"""
RUN_STATUS: Final = "SELECT status FROM runs WHERE id = {run_id}"


def last_json(stdout: str) -> Any:
    """The JSON line a command printed last on stdout."""
    return json.loads([line for line in stdout.splitlines() if line.strip()][-1])


def events(stderr: str) -> list[dict[str, Any]]:
    """The JSON log events a command wrote to stderr (lines that are no JSON object are compose's own)."""
    found = []
    for line in stderr.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and "event" in event:
            found.append(event)
    return found


def named(found: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    return [event for event in found if event["event"] == name]


def wait_for(probe: Callable[[], Any], *, within_s: float, what: str, argv: list[str] = PT) -> Any:
    """`probe()` until it returns a truthy value, polled every `POLL_S` for at most `within_s`."""
    deadline = time.monotonic() + within_s
    while not (found := probe()):
        if time.monotonic() >= deadline:
            pytest.fail(f"{what} not within {within_s:g} s:\n{logs_tail(argv, 'worker', 'backup')}")
        time.sleep(POLL_S)
    return found


def one(argv: list[str], sql: str) -> str:
    return psql(argv, sql).strip()


def backup(argv: list[str], command: str) -> Any:
    """`jobhunter backup <command>` in the site's running backup container; what it printed."""
    return last_json(run([*argv, "exec", "-T", "backup", "jobhunter", "backup", command], timeout=900).stdout)


def in_worker(argv: list[str], script: str, stdin: str | None = None) -> Any:
    """`script` run by the app's python in the site's worker container; the JSON it printed."""
    done = run([*argv, "exec", "-T", "worker", "python", "-I", "-c", script], stdin=stdin, timeout=300)
    return last_json(done.stdout)


class Site:
    """A browser of a site: a TLS client of its Caddy, logged in as `USERNAME`."""

    def __init__(self, argv: list[str]) -> None:
        self.argv = argv
        context = ssl.create_default_context(cadata=caddy_root_pem(argv))
        self.http = httpx.Client(base_url=ORIGIN, verify=context, timeout=30)
        wait_for(self._healthy, within_s=120, what="Caddy and the api", argv=argv)
        login = self.http.post(
            "/api/auth/login", json={"username": USERNAME, "password": PASSWORD}, headers={"Origin": ORIGIN}
        )
        assert login.status_code == 204, login.text

    def _healthy(self) -> bool:
        try:
            return self.http.get("/healthz").status_code == 200
        except httpx.TransportError:
            return False

    def headers(self) -> dict[str, str]:
        return {"Origin": ORIGIN, "X-JH-Request": "1", "Idempotency-Key": f"restore-test-{time.time_ns()}"}

    def post(self, path: str, expected: int, **kwargs: Any) -> Any:
        answer = self.http.post(path, headers=self.headers(), **kwargs)
        assert answer.status_code == expected, (path, answer.status_code, answer.text)
        return answer.json()

    def get(self, path: str) -> Any:
        answer = self.http.get(path)
        assert answer.status_code == 200, (path, answer.status_code)
        return answer.json()

    def export_download(self, export_id: int) -> tuple[int, str | None]:
        answer = self.http.get(f"/api/privacy/exports/{export_id}", params={"download": "1"})
        body = answer.json() if answer.headers.get("content-type", "").startswith("application/") else {}
        return answer.status_code, body.get("code")

    def ready_export(self) -> int:
        export_id = int(self.post("/api/privacy/export", 202, json={})["export_id"])
        wait_for(
            lambda: self.get(f"/api/privacy/exports/{export_id}")["status"] == "ready",
            within_s=180,
            what=f"export {export_id} ready",
            argv=self.argv,
        )
        return export_id

    def collect(self, *, include_paid: bool) -> int:
        """A manual run, to its end; its id (it must have succeeded)."""
        run_id = int(self.post("/api/runs", 202, json={"include_paid": include_paid})["run"]["id"])
        status = wait_for(
            lambda: (s := one(self.argv, RUN_STATUS.format(run_id=run_id))) in TERMINAL and s,
            within_s=900,
            what=f"run {run_id}",
            argv=self.argv,
        )
        assert status == "succeeded", (run_id, status)
        return run_id

    def confirmed_profile(self) -> int:
        """The synthetic CV uploaded, reviewed, extracted on the recorded streams and confirmed; the
        version."""
        pdf = minimal_pdf(SYNTHETIC_CV.splitlines())
        draft = int(
            self.post("/api/profile/cv", 202, files={"file": ("Lebenslauf.pdf", pdf, "application/pdf")})[
                "draft_id"
            ]
        )
        path = f"/api/profile/drafts/{draft}"
        self.wait_draft(path, "text_review")
        self.post(f"{path}/text-review", 202)
        self.wait_draft(path, "ready")
        return int(
            self.post(f"{path}/confirm", 201, json={"expected_rescore_ack": True})["profile_version_id"]
        )

    def wait_draft(self, path: str, status: str) -> None:
        def reached() -> bool:
            found = self.get(path)["status"]
            assert found != "failed", path
            return bool(found == status)

        wait_for(reached, within_s=180, what=f"draft {status}", argv=self.argv)
