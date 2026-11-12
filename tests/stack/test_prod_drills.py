"""The operations drills on the compose test stack (plan M5 Task 27; spec §19.6, M5-R4 R2, Deviation 32):
the upgrade from the preceding release and the rollback to it by its compatibility floor.

Run explicitly with `-m stack`, with no other stack on 172.30.0.0/24 (the dev stack: `$DC down` first), after
`scripts/build-prev-image.sh` has tagged the preceding release's app image `jobhunter-app:prev`:

    scripts/build-prev-image.sh
    JOBHUNTER_STACK_PREBUILT=embedder uv run pytest -m stack -q -p no:cacheprovider \
      tests/stack/test_prod_drills.py -k upgrade_then_rollback

The fixture is `tests.support.prodstack.prod_session("postgres")`: it builds the current images (the deploy
of 0011 runs them) and starts postgres alone on an empty `pgdata`. Every step of the preceding release sets
`app_image=jobhunter-app:prev` in the environment, which takes precedence over deploy/compose.test.env, and
starts without building (`up(PT, build=False)`): the test overlay's `build:` would otherwise rebuild the
current code under that tag. The backup image stays the current one throughout (`backup_image`); it is not
the drill's subject. At the end, after a failure too, `prod_session` removes the projects with their volumes
(`down -v`). The drill never removes an image: `jobhunter-app:prev` stays tagged, for the operator to remove
(CI removes it in an `if: always()` step, Task 29).

The site is driven as in tests/stack/test_prod_restore.py, by the browser (`Site`, logged in as the stack
modules' dummy user), the seed inputs and the log reader both share (`tests.support.prodsite`): a collection
on the fixtures and a confirmed profile from the recorded Claude streams, no paid or external call.
"""

import subprocess
from collections.abc import Iterator
from typing import Any, Final

import pytest
from jobhunter.db.migrate import MIGRATE_ORDER

from tests.support.prodsite import PASSWORD, SEED_INPUTS, USERNAME, Site, events, wait_for
from tests.support.prodstack import PT, REPO, create_user, prod_session, psql, run, up

pytestmark = pytest.mark.stack

PREV_IMAGE: Final = "jobhunter-app:prev"
PREV_HEAD: Final = "0010_m5_operations"
HEAD: Final = "0011_m5_purge_operation_index"
MIGRATION: Final = "backend/src/jobhunter/db/migrations/versions/0011_m5_purge_operation_index.py"
REVISION_LABEL: Final = "org.opencontainers.image.revision"
# The tables whose rows the drill keeps across the upgrade and the rollback.
KEPT: Final = (
    "jobs",
    "postings",
    "job_revisions",
    "profile_versions",
    "cv_files",
    "settings_versions",
    "users",
    "queries",
    "watchlist_companies",
)
NONE_MISSING: Final[dict[str, list[int]]] = {table: [] for table in KEPT}
READY_WITHIN_S: Final = 60


def one(sql: str) -> str:
    return psql(PT, sql).strip()


def preceding_commit() -> str:
    """The parent of the commit that added 0011, as `build-prev-image.sh` finds it."""
    log = ["git", "-C", str(REPO), "log", "--diff-filter=A", "--format=%H", "--", MIGRATION]
    added = run(log).stdout.split()[-1]
    return run(["git", "-C", str(REPO), "rev-parse", f"{added}^"]).stdout.strip()


def migrate() -> tuple[subprocess.CompletedProcess[str], list[dict[str, Any]]]:
    """`$PT run --rm migrate` (§19.6's step 2) with the release the environment names: its outcome and the
    JSON events it logged (stdout and stderr)."""
    done = run([*PT, "run", "--rm", "-T", "migrate"], timeout=600, check=False)
    return done, events(f"{done.stdout}\n{done.stderr}")


def steps_of(found: list[dict[str, Any]]) -> list[str]:
    return [event["step"] for event in found if event["event"] == "migrate_step"]


def snapshot() -> dict[str, Any]:
    """The ids of every table of `KEPT`, and the active profile version."""
    ids = {
        table: {int(n) for n in psql(PT, f"SELECT id FROM {table}").split()}  # noqa: S608
        for table in KEPT
    }
    return {"ids": ids, "profile": active_profile()}


def missing(before: dict[str, Any]) -> dict[str, list[int]]:
    """Per table of `KEPT`, the ids `before` holds that are gone (a scheduled collection may add rows, nothing
    may go): every list is empty while the data is intact (`NONE_MISSING`), and a failure names the table and
    its ids."""
    after = snapshot()["ids"]
    return {table: sorted(before["ids"][table] - after[table]) for table in KEPT}


def active_profile() -> str:
    return one("SELECT profile_version_id FROM active_config WHERE id = 1")


def images() -> dict[str, str]:
    """The image each running app service of the project was created from."""
    listed = run([*PT, "ps", "--format", "{{.Service}} {{.Image}}", "api", "worker"]).stdout
    return dict(line.split(" ", 1) for line in listed.splitlines() if line.strip())


def readyz(site: Site) -> tuple[int, Any]:
    answer = site.http.get("/readyz")
    return answer.status_code, answer.json()


def ready(site: Site) -> None:
    wait_for(lambda: readyz(site)[0] == 200, within_s=READY_WITHIN_S, what="/readyz 200")


def jobs_list(site: Site) -> list[int]:
    """The first page of the Jobs list, its job ids."""
    return [int(card["job_id"]) for card in site.get("/api/jobs?limit=100")["items"]]


@pytest.fixture(scope="module")
def stack() -> Iterator[None]:
    inspected = run(["docker", "image", "inspect", PREV_IMAGE], check=False)
    if inspected.returncode != 0:
        pytest.fail(f"{PREV_IMAGE} is missing: build it first with scripts/build-prev-image.sh")
    with prod_session("postgres"):
        yield


def test_upgrade_then_rollback(stack: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """The preceding release (0010) with data; §19.6's deploy of the 0011 image (`run --rm migrate`, then
    `up -d --wait`); the rollback to the preceding release, whose `migrate` accepts 0011 as
    `ahead_compatible` and whose site is ready and serves the same data; then 0011's record made
    destructive: the preceding release's `migrate` refuses it (`migrate_schema_ahead_incompatible`) and its
    `/readyz` answers 503 `migrations`."""
    # the image is the preceding release's: built from the parent of 0011's commit
    label = f'{{{{index .Config.Labels "{REVISION_LABEL}"}}}}'
    built_from = run(["docker", "image", "inspect", "--format", label, PREV_IMAGE]).stdout.strip()
    assert built_from == preceding_commit()

    # The preceding release, from an empty database, with data
    monkeypatch.setenv("app_image", PREV_IMAGE)
    up(PT, build=False)
    assert set(images().values()) == {PREV_IMAGE}
    assert one("SELECT version_num FROM alembic_version") == PREV_HEAD
    create_user(PT, USERNAME, PASSWORD)
    site = Site(PT)
    assert one(SEED_INPUTS).count("|") == 2
    site.collect(include_paid=True)
    version = site.confirmed_profile()
    before = snapshot()
    assert before["ids"]["jobs"] and before["ids"]["users"] and before["profile"] == str(version)
    assert jobs_list(site)

    # §19.6's deploy of the 0011 image, built from the current code at the session's start
    monkeypatch.delenv("app_image")
    done, found = migrate()
    assert done.returncode == 0, done.stdout + done.stderr
    assert steps_of(found) == list(MIGRATE_ORDER)
    assert not [e for e in found if e["event"].startswith("migrate_schema_ahead")]
    up(PT, build=False)
    assert set(images().values()) == {"jobhunter-app:dev"}
    assert one("SELECT version_num FROM alembic_version") == HEAD
    assert one(f"SELECT compat_floor || ' ' || destructive FROM schema_compat WHERE revision = '{HEAD}'") == (  # noqa: S608
        f"{PREV_HEAD} false"
    )
    assert one("SELECT to_regclass('operations_purge_manifest') IS NOT NULL") == "t"
    site = Site(PT)
    ready(site)
    assert missing(before) == NONE_MISSING
    assert active_profile() == before["profile"]
    assert jobs_list(site)

    # The rollback: the preceding release's images again, on the 0011 schema
    monkeypatch.setenv("app_image", PREV_IMAGE)
    done, found = migrate()
    assert done.returncode == 0, done.stdout + done.stderr
    [ahead] = [e for e in found if e["event"] == "migrate_schema_ahead_compatible"]
    assert (ahead["database_heads"], ahead["code_head"]) == ([HEAD], PREV_HEAD)
    assert steps_of(found) == [step for step in MIGRATE_ORDER if step != "alembic"]
    up(PT, build=False)
    assert set(images().values()) == {PREV_IMAGE}
    assert one("SELECT version_num FROM alembic_version") == HEAD  # nothing was downgraded
    site = Site(PT)  # the login works
    ready(site)
    assert readyz(site) == (200, {})
    assert missing(before) == NONE_MISSING
    assert active_profile() == before["profile"]
    assert jobs_list(site)

    # 0011 made destructive in the database: the preceding release refuses it
    psql(PT, f"UPDATE schema_compat SET destructive = true WHERE revision = '{HEAD}'")  # noqa: S608
    done, found = migrate()
    assert done.returncode == 1, done.stdout + done.stderr
    [refused] = [e for e in found if e["event"] == "migrate_schema_ahead_incompatible"]
    assert (refused["database_heads"], refused["code_head"]) == ([HEAD], PREV_HEAD)
    assert steps_of(found) == [] and "Traceback" not in done.stderr
    assert readyz(site) == (503, {"failing": ["migrations"]})

    # The drill removed no image
    assert run(["docker", "image", "inspect", PREV_IMAGE], check=False).returncode == 0
