"""The budgeted live recorder of the Task 6 spikes (spec §22 V3, V8, V9, V10, V18; plan Global Constraints).

    uv run python scripts/spikes/record_sources.py <jsearch|serpapi|adzuna|arbeitnow|ats> [--phase N]

It sends the exact requests of one spike phase (`plan`) with the user's keys and writes each response under
`.fixtures-raw/<source>/` (gitignored: raw recordings carry recruiter personal data and are never committed):
`<case>.json` (or `.xml`/`.html`/`.txt`: the body as received, except that the value of every key variable is
replaced by `[secret:<VAR>]`, because a key is never written to any file), `<case>.headers.json` (`{"status",
"headers"}`, the §5.2 header allowlist only) and `<case>.meta.json` (the URL without its query, the non-secret
parameters, `fetched_at`). The keys come from `RAPIDAPI_KEY`, `SERPAPI_API_KEY`, `ADZUNA_APP_ID` and
`ADZUNA_APP_KEY` only; they are sent as the §5.3 header (JSearch) or query parameters, never printed or
logged, and an exception is printed as its class only (an `httpx` error renders the URL with its key).

The accounting is crash-safe and conservative. The whole invocation holds an exclusive `flock` on
`.fixtures-raw/budget.lock` (a second invocation fails at once). Before every send,
`.fixtures-raw/budget.json`
(`{provider: {"sent": n, "requests": [{case, phase, state, status, ...}], "phases": {...}}}`) is rewritten by
atomic rename with the request counted as `sending`; after the response the entry becomes `done` with its
status. A crash or a transport failure leaves the request counted. A request is refused when `sent + 1` would
exceed `BUDGET`, and a phase that has any entry is refused (a phase never runs twice). SerpApi `account.json`
reads are free by name: recorded with `"free": true`, never counted. A phase that depends on another one
waits for it (`NOT_BEFORE`: JSearch phase 2 two hours and phase 3 a day after phase 1, the Arbeitnow phases at
20 to 120 minutes). A paid phase stops at its first unexpected outcome (a transport error or a status it does
not expect) and sends nothing more; a free phase records every board and reports the failures.

Each request prints one line: `<source> <case> <status> units_remaining=<n> budget_left=<n>`.
"""

import argparse
import asyncio
import fcntl
import hashlib
import json
import logging
import os
import re
import sys
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final, TextIO
from urllib.parse import urlsplit

import httpx
from jobhunter.observability.logging import configure_logging
from jobhunter.sources import http
from jobhunter.sources.base import RequestSpec

BUDGET: Final = {"jsearch": 8, "serpapi": 1, "adzuna": 5, "arbeitnow": 6, "ats": 12}
KEY_VARS: Final = ("RAPIDAPI_KEY", "SERPAPI_API_KEY", "ADZUNA_APP_ID", "ADZUNA_APP_KEY")
# The repository's raw directory, whatever the cwd: one budget.json for the whole milestone (there is no
# --root flag; `main(root=...)` is for tests only).
DEFAULT_ROOT: Final = Path(__file__).resolve().parents[2] / ".fixtures-raw"
LOCKED, REFUSED, STOPPED, FAILED = 3, 4, 5, 1

JSEARCH: Final = "https://jsearch.p.rapidapi.com"
JSEARCH_PARAMS: Final = (
    ("query", "software engineer in Berlin, Germany"),
    ("country", "de"),
    ("num_pages", "1"),
    ("date_posted", "week"),
)
SERPAPI: Final = "https://serpapi.com"
ADZUNA: Final = "https://api.adzuna.com/v1/api/jobs/de/search/1"
ADZUNA_BASE: Final = (("what", "software engineer"), ("where", "Berlin"), ("sort_by", "date"))
ARBEITNOW: Final = "https://www.arbeitnow.com/api/job-board-api"
SMARTRECRUITERS: Final = "https://api.smartrecruiters.com/v1/companies/DeliveryHero/postings"
# SerpApi's account counters lag its searches (§6 `SERPAPI_USAGE_LAG`, 120 s): the closing read waits longer.
SERPAPI_USAGE_LAG_S: Final = 130.0
# The phases that must wait for phase 1 of their provider, by how long after its end.
NOT_BEFORE: Final[Mapping[tuple[str, int], timedelta]] = {
    ("jsearch", 2): timedelta(hours=2),
    ("jsearch", 3): timedelta(hours=24),
    ("arbeitnow", 2): timedelta(minutes=20),
    ("arbeitnow", 3): timedelta(minutes=40),
    ("arbeitnow", 4): timedelta(minutes=60),
    ("arbeitnow", 5): timedelta(minutes=90),
    ("arbeitnow", 6): timedelta(minutes=120),
}
# The source id of `http.make_client` for each ATS host.
ATS_SOURCE: Final = {
    "boards-api.greenhouse.io": "greenhouse",
    "api.lever.co": "lever",
    "api.eu.lever.co": "lever",
    "api.ashbyhq.com": "ashby",
    "everphone.jobs.personio.de": "personio",
    "bunq.recruitee.com": "recruitee",
    "api.smartrecruiters.com": "smartrecruiters",
}
PAID: Final = frozenset({"jsearch", "serpapi", "adzuna"})
_REMAINING_HEADERS: Final = ("x-ratelimit-requests-remaining", "x-ratelimit-remaining")


@dataclass(frozen=True)
class Planned:
    """One request of a phase. `url` has no query; `params` hold no secret (keys are named by variable)."""

    case: str
    url: str
    params: tuple[tuple[str, str], ...] = ()
    secret_params: tuple[tuple[str, str], ...] = ()  # (parameter, key variable)
    secret_headers: tuple[tuple[str, str], ...] = ()  # (header, key variable)
    headers: tuple[tuple[str, str], ...] = ()
    expect: frozenset[int] = frozenset({200})
    body: bool = True  # False: the headers and the meta only (the Arbeitnow rate-limit reads)
    gap_s: float = 1.5  # the pause before this request; 0 sends it at once after the previous one
    cursor_from: str | None = None  # the earlier case whose body holds the `cursor` to send (JSearch)
    detail_of: tuple[str, int] | None = None  # (list case, item index): a SmartRecruiters detail call
    renamed: Mapping[int, str] = field(default_factory=dict)  # a status that changes the case name


def _jsearch(case: str, *, gap_s: float = 1.5, cursor: bool = False, **params: str) -> Planned:
    path = params.pop("path", "/search-v2")
    merged = tuple((k, params.get(k, v)) for k, v in JSEARCH_PARAMS)
    return Planned(
        case,
        JSEARCH + path,
        merged,
        secret_headers=(("x-rapidapi-key", "RAPIDAPI_KEY"),),
        headers=(("x-rapidapi-host", "jsearch.p.rapidapi.com"),),
        expect=frozenset({404}) if path == "/search" else frozenset({200}),
        gap_s=gap_s,
        cursor_from="search_v2_page1" if cursor else None,
    )


def _serpapi(
    case: str, path: str, params: tuple[tuple[str, str], ...] = (), *, gap_s: float = 1.5
) -> Planned:
    return Planned(case, SERPAPI + path, params, secret_params=(("api_key", "SERPAPI_API_KEY"),), gap_s=gap_s)


def _adzuna(case: str, *params: tuple[str, str]) -> Planned:
    return Planned(
        case,
        ADZUNA,
        (*ADZUNA_BASE, *params),
        secret_params=(("app_id", "ADZUNA_APP_ID"), ("app_key", "ADZUNA_APP_KEY")),
    )


def plan(source: str, phase: int) -> list[Planned]:
    """The exact requests of a phase, in order (Task 6 step 4); `ValueError` for a phase that is none."""
    if source == "jsearch" and phase == 1:
        return [
            _jsearch("search_legacy_404", path="/search", gap_s=0),
            _jsearch("search_v2_page1"),
            _jsearch("search_v2_today", date_posted="today"),
            _jsearch("search_v2_page2", cursor=True, gap_s=0),  # at once after (3): two requests within 1 s
        ]
    if source == "jsearch" and phase == 2:
        return [_jsearch("search_v2_page2_reused", cursor=True, gap_s=0)]
    if source == "jsearch" and phase == 3:
        return [_jsearch("search_v2_page1_day2", gap_s=0), _jsearch("search_v2_page2_day2", cursor=True)]
    if source == "serpapi" and phase == 1:
        search = (("engine", "google_jobs"), ("q", "software engineer Berlin"), ("gl", "de"), ("hl", "en"))
        return [
            _serpapi("account_before", "/account.json", gap_s=0),
            _serpapi("google_jobs_hl_en", "/search.json", search),
            _serpapi("account_after", "/account.json", gap_s=SERPAPI_USAGE_LAG_S),
        ]
    if source == "adzuna" and phase == 1:
        return [
            replace(_adzuna("search_p1_rpp100", ("results_per_page", "100")), gap_s=0),
            _adzuna("search_p1_days1", ("results_per_page", "50"), ("max_days_old", "1")),
            _adzuna("search_p1_distance10", ("results_per_page", "50"), ("distance", "10")),
            _adzuna("search_p1_distance50", ("results_per_page", "50"), ("distance", "50")),
        ]
    if source == "arbeitnow" and phase in range(1, 7):
        if phase <= 2:
            return [Planned(f"page{phase}", ARBEITNOW, (("page", str(phase)),), gap_s=0)]
        return [Planned(f"page1_phase{phase}", ARBEITNOW, (("page", "1"),), body=False, gap_s=0)]
    if source == "ats" and phase == 1:
        return _ats_plan()
    raise ValueError(f"{source} has no phase {phase}")


def _ats_plan() -> list[Planned]:
    def board(case: str, url: str, *params: tuple[str, str], **kwargs: Any) -> Planned:
        return Planned(case, url, params, gap_s=0.5, **kwargs)

    lever = (("mode", "json"), ("skip", "0"), ("limit", "100"))
    return [
        board(
            "greenhouse_getyourguide",
            "https://boards-api.greenhouse.io/v1/boards/getyourguide/jobs",
            ("content", "true"),
        ),
        board(
            "greenhouse_404",
            "https://boards-api.greenhouse.io/v1/boards/jobhunter-nonexistent-board/jobs",
            ("content", "true"),
            expect=frozenset({404}),
        ),
        board("lever_leverdemo", "https://api.lever.co/v0/postings/leverdemo", *lever),
        board(
            "lever_eu_404",
            "https://api.eu.lever.co/v0/postings/leverdemo",
            *lever,
            expect=frozenset({404, 200}),
            renamed={200: "lever_eu_leverdemo"},
        ),
        board(
            "ashby_n8n", "https://api.ashbyhq.com/posting-api/job-board/n8n", ("includeCompensation", "true")
        ),
        board("personio_everphone", "https://everphone.jobs.personio.de/xml"),
        board("recruitee_bunq", "https://bunq.recruitee.com/api/offers/"),
        board("smartrecruiters_deliveryhero_list", SMARTRECRUITERS, ("limit", "100"), ("offset", "0")),
        board(
            "smartrecruiters_deliveryhero_detail_1",
            SMARTRECRUITERS,
            detail_of=("smartrecruiters_deliveryhero_list", 0),
        ),
        board(
            "smartrecruiters_deliveryhero_detail_2",
            SMARTRECRUITERS,
            detail_of=("smartrecruiters_deliveryhero_list", 1),
        ),
    ]


def _provider_keys(source: str) -> tuple[str, ...]:
    return {
        "jsearch": ("RAPIDAPI_KEY",),
        "serpapi": ("SERPAPI_API_KEY",),
        "adzuna": ("ADZUNA_APP_ID", "ADZUNA_APP_KEY"),
    }.get(source, ())


def is_free(source: str, url: str) -> bool:
    """SerpApi `account.json` reads are free (§5.3.2), by name; everything else counts."""
    return source == "serpapi" and urlsplit(url).path == "/account.json"


# --- the budget file ---


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    _fsync_dir(path.parent)


class Budget:
    """`.fixtures-raw/budget.json`, saved by atomic rename after every change."""

    def __init__(self, root: Path) -> None:
        self.path = root / "budget.json"
        self.data: dict[str, Any] = json.loads(self.path.read_text("utf-8")) if self.path.exists() else {}

    def provider(self, name: str) -> dict[str, Any]:
        state: dict[str, Any] = self.data.setdefault(name, {})
        state.setdefault("sent", 0)
        state.setdefault("requests", [])
        state.setdefault("phases", {})
        return state

    def save(self) -> None:
        _atomic_write(self.path, (json.dumps(self.data, indent=2, sort_keys=True) + "\n").encode("utf-8"))


@contextmanager
def exclusive(root: Path) -> Iterator[bool]:
    """Hold the exclusive `flock` on `budget.lock` for the whole invocation; yields False when it is taken."""
    fd = os.open(root / "budget.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        os.close(fd)


# --- files ---


def _without_secrets(data: bytes, secrets: Mapping[str, str]) -> bytes:
    """`data` with every standalone occurrence of a key value (8 characters or more: a shorter one would
    match ordinary text) replaced by `[secret:<VAR>]`."""
    for var, value in secrets.items():
        if len(value) < 8:
            continue
        data = re.sub(
            rb"(?<![A-Za-z0-9])" + re.escape(value.encode("utf-8")) + rb"(?![A-Za-z0-9])",
            f"[secret:{var}]".encode(),
            data,
        )
    return data


def _body_suffix(body: bytes, content_type: str) -> str:
    try:
        json.loads(body)
    except ValueError:
        pass
    else:
        return ".json"
    lowered, head = content_type.lower(), body.lstrip()[:64].lower()
    if "xml" in lowered or head.startswith(b"<?xml"):
        return ".xml"
    if "html" in lowered or head.startswith((b"<!doctype html", b"<html")):
        return ".html"
    return ".txt"


# The continuation key of the `search-v2` envelope: `cursor` (§5.3.1); the others only if it is absent.
CURSOR_KEYS: Final = ("cursor", "next_cursor", "nextCursor", "next_page_cursor")


def find_cursor(doc: Any) -> tuple[tuple[str | int, ...], str] | None:
    """The path and value of the first non-empty string under a `CURSOR_KEYS` key (in that order of
    preference, each searched breadth first); it is sent as the `cursor` parameter."""
    for wanted in CURSOR_KEYS:
        queue: list[tuple[tuple[str | int, ...], Any]] = [((), doc)]
        while queue:
            path, node = queue.pop(0)
            if isinstance(node, dict):
                for key, value in node.items():
                    if key == wanted and isinstance(value, str) and value:
                        return (*path, key), value
                    queue.append(((*path, key), value))
            elif isinstance(node, list):
                queue.extend(((*path, i), value) for i, value in enumerate(node))
    return None


def _read_body(root: Path, source: str, case: str) -> Any:
    path = root / source / f"{case}.json"
    return json.loads(path.read_text("utf-8")) if path.exists() else None


# --- one invocation ---

Clock = Callable[[], datetime]
Sleep = Callable[[float], Awaitable[None]]
ClientFactory = Callable[[str], httpx.AsyncClient]


@dataclass
class Recorder:
    source: str
    phase: int
    root: Path
    secrets: Mapping[str, str]
    client_factory: ClientFactory
    now: Clock
    sleep: Sleep
    out: TextIO
    budget: Budget = field(init=False)

    def __post_init__(self) -> None:
        self.budget = Budget(self.root)

    def say(self, line: str) -> None:
        print(line, file=self.out, flush=True)

    def refusal(self) -> str | None:
        """Why this phase must not run, or None."""
        state = self.budget.provider(self.source)
        if str(self.phase) in state["phases"] or any(e.get("phase") == self.phase for e in state["requests"]):
            return f"phase {self.phase} of {self.source} has already run (a phase never runs twice)"
        if (wait := NOT_BEFORE.get((self.source, self.phase))) is not None:
            first = state["phases"].get("1", {}).get("finished_at")
            if first is None:
                return f"phase 1 of {self.source} has not finished"
            ready = datetime.fromisoformat(first) + wait
            if self.now() < ready:
                return f"phase {self.phase} of {self.source} runs at {_iso(ready)} or later"
        return None

    def resolve(self, planned: Planned) -> Planned | str:
        """The request with its dynamic part (the cursor, the detail id), or why it cannot be sent."""
        if planned.cursor_from is not None:
            found = find_cursor(_read_body(self.root, self.source, planned.cursor_from))
            if found is None:
                return f"no cursor in {planned.cursor_from}: {planned.case} not sent"
            return replace(planned, params=(*planned.params, ("cursor", found[1])))
        if planned.detail_of is not None:
            case, index = planned.detail_of
            listing = _read_body(self.root, self.source, case)
            content = listing.get("content") if isinstance(listing, dict) else None
            if not isinstance(content, list) or len(content) <= index or not isinstance(content[index], dict):
                return f"no item {index} in {case}: {planned.case} not sent"
            return replace(planned, url=f"{planned.url}/{content[index].get('id')}")
        return planned

    def _spec(self, planned: Planned) -> RequestSpec:
        params = dict(planned.params)
        return RequestSpec(
            source_instance_id=0,
            operation="detail" if planned.detail_of else "list",
            cell_id=0,
            page_no=1,
            params={k: v for k, v in params.items() if k != "cursor"},
            cursor_in=params.get("cursor"),
            detail_native_id=None,
            parent_fetch_request_id=None,
            window=None,
            request_class="probe",
        )

    async def send(self, planned: Planned) -> tuple[int | None, bool]:
        """Count, send, record one request: (status, expected). Raises only for a bug, after counting."""
        state = self.budget.provider(self.source)
        free = is_free(self.source, planned.url)
        entry: dict[str, Any] = {
            "case": planned.case,
            "phase": self.phase,
            "state": "sending",
            "status": None,
            "free": free,
            "sent_at": _iso(self.now()),
        }
        state["requests"].append(entry)
        if not free:
            state["sent"] += 1
        self.budget.save()  # counted before the send: a crash after this line leaves it counted
        source_id = ATS_SOURCE.get(urlsplit(planned.url).hostname or "", self.source)
        params: list[tuple[str, str | int | float | bool | None]] = [
            *planned.params,
            *((name, self.secrets[var]) for name, var in planned.secret_params),
        ]
        headers = {
            **dict(planned.headers),
            **{name: self.secrets[var] for name, var in planned.secret_headers},
        }
        async with self.client_factory(source_id) as client:
            request = client.build_request("GET", planned.url, params=params, headers=headers)
            started = asyncio.get_running_loop().time()
            outcome = await http.send(client, self._spec(planned), request, now=self.now)
            duration_ms = round((asyncio.get_running_loop().time() - started) * 1000)
        page = outcome.page
        status = None if page is None else page.status
        case = planned.renamed.get(status, planned.case) if status is not None else planned.case
        error = None if outcome.error is None else f"{outcome.error.kind}:{outcome.error.code}"
        self._write(planned, case, outcome, duration_ms, error)
        entry.update(state="done", status=status, error=error, case=case, finished_at=_iso(self.now()))
        self.budget.save()
        self.say(f"{self.source} {case} {status if status is not None else '-'} {self._remaining(page)}")
        return status, error is None and status in planned.expect

    def _remaining(self, page: Any) -> str:
        units = "-"
        if page is not None:
            units = next((page.headers[h] for h in _REMAINING_HEADERS if h in page.headers), "-")
            if self.source == "serpapi" and units == "-":
                try:
                    doc = json.loads(page.body)
                except ValueError:
                    doc = None
                if isinstance(doc, dict) and "total_searches_left" in doc:
                    units = str(doc["total_searches_left"])
        left = BUDGET[self.source] - self.budget.provider(self.source)["sent"]
        return f"units_remaining={units} budget_left={left}"

    def _write(
        self, planned: Planned, case: str, outcome: http.HttpOutcome, duration_ms: int, error: str | None
    ) -> None:
        directory = self.root / self.source
        page = outcome.page
        meta: dict[str, Any] = {
            "source": self.source,
            "case": case,
            "phase": self.phase,
            "method": "GET",
            "url": planned.url,
            "params": dict(planned.params),
            "fetched_at": _iso(page.fetched_at) if page is not None else _iso(self.now()),
            "status": None if page is None else page.status,
            "error": error,
            "duration_ms": duration_ms,
            "free": is_free(self.source, planned.url),
        }
        if page is not None:
            body = _without_secrets(page.body, self.secrets)
            meta.update(body_bytes=len(page.body), body_sha256=hashlib.sha256(page.body).hexdigest())
            if planned.body:
                suffix = _body_suffix(body, page.headers.get("content-type", ""))
                _atomic_write(directory / f"{case}{suffix}", body)
            headers = {"status": page.status, "headers": dict(sorted(page.headers.items()))}
            _atomic_write(directory / f"{case}.headers.json", _json_bytes(headers, self.secrets))
        _atomic_write(directory / f"{case}.meta.json", _json_bytes(meta, self.secrets))

    async def run(self) -> int:
        state = self.budget.provider(self.source)
        phases = state["phases"]
        phases[str(self.phase)] = {"started_at": _iso(self.now())}
        if self.source == "jsearch" and self.phase == 1:
            phases["1"]["note"] = "V10 phase 3 runs >= 24 h after finished_at (recorder NOT_BEFORE)"
        self.budget.save()
        code = 0
        for planned in plan(self.source, self.phase):
            if planned.gap_s:
                await self.sleep(planned.gap_s)
            resolved = self.resolve(planned)
            if isinstance(resolved, str):
                self.say(f"{self.source} {planned.case} STOP {resolved}")
                code = STOPPED
                break
            if not is_free(self.source, resolved.url) and state["sent"] + 1 > BUDGET[self.source]:
                self.say(
                    f"{self.source} {planned.case} REFUSED: the budget of {BUDGET[self.source]} is spent"
                )
                code = REFUSED
                break
            status, expected = await self.send(resolved)
            if not expected:
                if self.source in PAID:
                    self.say(
                        f"{self.source} {planned.case} STOP: unexpected outcome ({status}); nothing more sent"
                    )
                    code = STOPPED
                    break
                self.say(
                    f"{self.source} {planned.case} UNEXPECTED ({status}); continuing with the free requests"
                )
                code = STOPPED
        phases[str(self.phase)]["finished_at"] = _iso(self.now())
        if code:
            phases[str(self.phase)]["stopped"] = code
        if self.source == "jsearch" and self.phase == 1:
            phases["1"]["v10_phase3_not_before"] = _iso(self.now() + NOT_BEFORE[("jsearch", 3)])
        self.budget.save()
        return code


def _json_bytes(doc: Any, secrets: Mapping[str, str]) -> bytes:
    return _without_secrets((json.dumps(doc, indent=2, ensure_ascii=False) + "\n").encode("utf-8"), secrets)


def _parse(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="record_sources.py", description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("source", choices=sorted(BUDGET))
    parser.add_argument("--phase", type=int, default=1)
    return parser.parse_args(argv)


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    client_factory: ClientFactory | None = None,
    now: Clock | None = None,
    sleep: Sleep | None = None,
    out: TextIO | None = None,
    root: Path | None = None,
) -> int:
    """Run one phase; the exit status is 0, or `LOCKED`, `REFUSED`, `STOPPED` or `FAILED`. `root` (tests
    only) replaces `DEFAULT_ROOT`."""
    configure_logging()  # first: it quiets httpx and httpcore, whose INFO lines carry the query-string keys
    # `http.send` logs a `source_request` line (credentials removed); the recorder prints its own instead.
    logging.getLogger("jobhunter.sources.http").setLevel(logging.WARNING)
    stream = sys.stdout if out is None else out
    try:
        args = _parse(sys.argv[1:] if argv is None else argv)
        env = os.environ if environ is None else environ
        root = DEFAULT_ROOT if root is None else root
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        planned = plan(args.source, args.phase)
        missing = sorted(
            {
                var
                for p in planned
                for _, var in (*p.secret_params, *p.secret_headers)
                if not env.get(var, "").strip()
            }
        )
        if missing:
            print(f"{args.source}: REFUSED, missing {', '.join(missing)}; nothing sent", file=stream)
            return REFUSED
        secrets = {var: env[var].strip() for var in KEY_VARS if env.get(var, "").strip()}
        with exclusive(root) as held:
            if not held:
                print(
                    f"{args.source}: LOCKED, another recorder holds {root / 'budget.lock'}; nothing sent",
                    file=stream,
                )
                return LOCKED
            recorder = Recorder(
                args.source,
                args.phase,
                root,
                secrets,
                client_factory or (lambda source_id: http.make_client(source_id)),
                now or (lambda: datetime.now(UTC)),
                sleep or asyncio.sleep,
                stream,
            )
            if (why := recorder.refusal()) is not None:
                print(f"{args.source}: REFUSED, {why}; nothing sent", file=stream)
                return REFUSED
            return asyncio.run(recorder.run())
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001 - the class only: an exception's text can carry a URL and its key
        print(f"record_sources: {type(exc).__name__}", file=stream)
        return FAILED


if __name__ == "__main__":
    raise SystemExit(main())
