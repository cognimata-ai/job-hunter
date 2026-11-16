"""`GET /api/privacy/operator` (spec §20.6, §19.2, §14.2; M5 follow-ups Task F1, Ruling FU-1): the privacy
statement's operator values, read from the deployment's operator file once, when the api starts.

- It needs a session: 401 without one.
- With a valid file: 200 and the five values as the file has them.
- Outside prod, without a file or with one that is not valid: 200 and all five null, so the statement keeps
  its markers; an invalid file is logged `operator_file_invalid` with its path and the problem.
- The file is read at the start only: a file changed or removed afterwards changes nothing until the api
  starts again (`restart api`), and the api started again serves the new values.
- No value reaches a log line, the request's included (a caplog probe; no value is e-mail- or phone-shaped,
  which the redaction would mask before the probe saw it). Every value is invented.
"""

import asyncio
import json
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

import httpx
import pytest
from jobhunter.api.app import JobHunterApp, create_app
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.fixtures import ConfigFactory

pytestmark = pytest.mark.integration

OPERATOR: Final = "/api/privacy/operator"
VALUES: Final[Mapping[str, str]] = {
    "operator": "Quirinus Zappelmeier",
    "contact": "Postfach am Kuckucksweg, Zappelstadt",
    "controller": "Zappelmeier Datenhaltung GmbH",
    "vps_provider": "Wolkenkuckucksheim Hosting",
    "s3_provider": "Eimerweise Objektspeicher AG",
}
OTHER: Final[Mapping[str, str]] = {key: f"Neu {value}" for key, value in VALUES.items()}
NULLS: Final = dict.fromkeys(VALUES)
VALID: Final = json.dumps(VALUES).encode()


@pytest.fixture
def operator_file(request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The api's operator file: `VALID` by default, else the parameter's bytes, or no file for `None`. The
    test runs in `tmp_path`, and the path is relative: a log line keeps it whole (the redaction masks a run of
    40 or more path characters as a key, which the mount target is not)."""
    monkeypatch.chdir(tmp_path)
    path = Path("operator.json")
    content: bytes | None = getattr(request, "param", VALID)
    if content is not None:
        path.write_bytes(content)
    return path


@pytest.fixture
def api_app(
    engine: AsyncEngine,
    make_config: ConfigFactory,
    cv_store_dir: Path,
    operator_file: Path,
    caplog: pytest.LogCaptureFixture,
) -> JobHunterApp:
    """The test api (`tests.support.fixtures.api_app`) on `operator_file`, every record captured from its
    start on."""
    caplog.set_level(logging.DEBUG)
    cfg = make_config(
        "api", JOBHUNTER_CV_STORE_DIR=str(cv_store_dir), JOBHUNTER_OPERATOR_FILE=str(operator_file)
    )
    return create_app(cfg=cfg, engine=engine)


def records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [*caplog.get_records("setup"), *caplog.records]


def assert_no_value(caplog: pytest.LogCaptureFixture, values: Mapping[str, str] = VALUES) -> None:
    for record in records(caplog):
        text = str(record.msg) + repr(record.args) + str(record.exc_info)
        assert [key for key, value in values.items() if value in text] == [], text


async def test_the_route_needs_a_session(api_client: httpx.AsyncClient) -> None:
    r = await api_client.get(OPERATOR)
    assert (r.status_code, r.json()["code"]) == (401, "unauthenticated")
    assert all(value not in r.text for value in VALUES.values())


async def test_a_valid_file_serves_its_five_values(
    logged_in_client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    r = await logged_in_client.get(OPERATOR)
    assert r.status_code == 200, r.text
    assert r.json() == VALUES
    assert r.headers["content-type"] == "application/json"
    assert_no_value(caplog)


@pytest.mark.parametrize(
    ("operator_file", "problem"),
    [
        (None, None),
        (b'{"operator": "Quirinus Zappelmeier"', "is not valid JSON (line 1, column 36)"),
        (json.dumps({**VALUES, "phone": "+49 30 1234567"}).encode(), "has 1 key other than"),
        (json.dumps({**VALUES, "contact": "q" * 201}).encode(), "contact must be at most 200 characters"),
    ],
    indirect=["operator_file"],
    ids=["no file", "not JSON", "another key", "overlong"],
)
async def test_outside_prod_no_valid_file_serves_nulls(
    logged_in_client: httpx.AsyncClient,
    operator_file: Path,
    caplog: pytest.LogCaptureFixture,
    problem: str | None,
) -> None:
    r = await logged_in_client.get(OPERATOR)
    assert r.status_code == 200, r.text
    assert r.json() == NULLS
    warned = [
        record.msg
        for record in records(caplog)
        if isinstance(record.msg, dict) and record.msg.get("event") == "operator_file_invalid"
    ]
    if problem is None:
        assert warned == []
    else:
        [event] = warned
        assert event["path"] == str(operator_file) and event["problem"].startswith(problem)
        assert event["level"] == "warning"
    assert_no_value(caplog)


async def test_the_file_is_read_once_at_the_start(
    logged_in_client: httpx.AsyncClient,
    operator_file: Path,
    engine: AsyncEngine,
    make_config: ConfigFactory,
    cv_store_dir: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    assert (await logged_in_client.get(OPERATOR)).json() == VALUES
    await asyncio.to_thread(operator_file.write_text, json.dumps(OTHER), encoding="utf-8")
    assert (await logged_in_client.get(OPERATOR)).json() == VALUES  # the running api keeps what it read
    # The api started again (`restart api`), on the same database: the user's session goes on.
    cfg = make_config(
        "api", JOBHUNTER_CV_STORE_DIR=str(cv_store_dir), JOBHUNTER_OPERATOR_FILE=str(operator_file)
    )
    restarted = create_app(cfg=cfg, engine=engine)
    await asyncio.to_thread(operator_file.unlink)  # read already: the new api keeps what it started with
    async with again(restarted, logged_in_client) as client:
        r = await client.get(OPERATOR)
    assert (r.status_code, r.json()) == (200, OTHER)
    assert_no_value(caplog, {**VALUES, **{f"new {key}": value for key, value in OTHER.items()}})


def again(app: JobHunterApp, client: httpx.AsyncClient) -> httpx.AsyncClient:
    """A client of `app` with `client`'s address, headers and session cookie."""
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False, client=("198.51.100.20", 50000))
    headers: Any = client.headers
    return httpx.AsyncClient(
        transport=transport, base_url=str(client.base_url), headers=headers, cookies=client.cookies
    )
