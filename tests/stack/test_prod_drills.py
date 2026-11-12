"""The operations drills on the compose test stack (plan M5 Tasks 27 and 28; spec §19.6, §19.8, M5-R4 R2, R6
and R7, Deviations 31, 32 and 39): the upgrade from the preceding release and the rollback to it by its
compatibility floor; the rotation of the session secret, the data key and a database role's password.

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

The rotation drill (Task 28) needs no preceding image:

    JOBHUNTER_STACK_PREBUILT=embedder uv run pytest -m stack -q -p no:cacheprovider \
      tests/stack/test_prod_drills.py -k rotation_drill

Both drills run in one session too (CI's and the acceptance run's whole module): each drill's fixture is
function-scoped, so the upgrade drill's projects are removed (`down -v`) before the rotation drill's
`prod_session` starts, which would otherwise find PT up and skip.

Every secret it changes is a file of `.secrets/test`, PT's secrets directory (deploy/compose.test.env). It
never reads a value: each new value is written by `openssl` in a child shell to a `mktemp` file (`Scratch`),
and a file reaches a slot only by the two root commands of user decision M5-U6, run from the checkout with
`sudo -n` (which fails rather than ask for a password): `install -o root -g 10010 -m 0440 <scratch file>
.secrets/test/<name>`, and `scripts/rotate-db-role.sh --compose '<PT command line>' --secrets-dir
.secrets/test db_app <file>`. A value that moves to another slot (the old data key, which becomes
`data_key_previous`) is installed again from its scratch file, so the drill installs its own old data key
before it makes the data it rotates. What the containers report is key ids (hex), counts and inodes. At the
end, after a failure too, `data_key_previous` is emptied again, every scratch file is removed, and
`prod_session` removes `PT`, `PB` and the S3 stand-in with their volumes; the other secrets keep the drill's
new dummy values, which a fresh `pgdata` takes at its init.
"""

import io
import shlex
import subprocess
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final

import pytest
from jobhunter.db.migrate import MIGRATE_ORDER

from tests.support.prodsite import (
    PASSWORD,
    SEED_INPUTS,
    USERNAME,
    Site,
    backup,
    events,
    last_json,
    wait_for,
)
from tests.support.prodstack import (
    PB,
    PT,
    PT_PROJECT,
    REPO,
    S3_PROJECT,
    SECRETS_GROUP,
    create_user,
    down_v,
    prod_session,
    psql,
    run,
    up,
)

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


@pytest.fixture
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


# ---- the rotation drill (Task 28) --------------------------------------------------------------------------

SECRETS: Final = ".secrets/test"  # PT's secrets directory, from the checkout (deploy/compose.test.env)
ROTATE_DB_ROLE: Final = "scripts/rotate-db-role.sh"
HEALTHY_WITHIN_S: Final = 240
RESTORE_TIMEOUT_S: Final = 1200
# The api's data keys as key ids (hex): the data key, then `data_key_previous` while one is mounted.
KEY_IDS: Final = """
import json
from jobhunter import crypto
from jobhunter.config import load_config
print(json.dumps([crypto.key_id(key).hex() for key in crypto.data_keys(load_config("api"))]))
"""
# Every CV file and text a row names, opened with the api's data key alone (the first of its keys): how many
# there are, how many opened (one that does not open is counted out, `crypto.DecryptError`), and how many keys
# the api holds. No content is printed.
OPENED: Final = """
import json
import psycopg
from jobhunter import crypto
from jobhunter.config import load_config
from jobhunter.profile.storage import CvStore
cfg = load_config("api")
keys = crypto.data_keys(cfg)
store = CvStore(cfg.cv_store_dir)
with psycopg.connect(cfg.dsn("psycopg")) as conn:
    rows = conn.execute("SELECT id, storage_path, raw_text_enc FROM cv_files ORDER BY id").fetchall()
files = [name for _, name, _ in rows if name is not None]
texts = [(cv_file_id, bytes(blob)) for cv_file_id, _, blob in rows if blob is not None]
files_opened = texts_opened = 0
for name in files:
    try:
        store.read(name, keys[:1])
    except crypto.DecryptError:
        continue
    files_opened += 1
for cv_id, blob in texts:
    try:
        store.decrypt_text(blob, keys[:1], cv_file_id=cv_id)
    except crypto.DecryptError:
        continue
    texts_opened += 1
print(json.dumps({
    "keys": len(keys),
    "files": len(files),
    "files_opened": files_opened,
    "texts": len(texts),
    "texts_opened": texts_opened,
}))
"""
# `db_app` as the running postgres container mounts it (its file from its start: the old one after an
# install), or as a one-off mounts it (the installed one): `1` when PT's postgres accepts it for
# `jobhunter_app`, else psql's refusal. The password goes from the file into psql's environment only.
APP_LOGIN: Final = (
    "PGPASSWORD=$(cat /run/secrets/db_app) && export PGPASSWORD && "
    'exec psql -X -h postgres -U jobhunter_app -d "$POSTGRES_DB" -At -c "SELECT 1"'
)
LABEL: Final = '{{{{index .Config.Labels "com.docker.compose.{}"}}}}'
STARTED: Final = "|".join([LABEL.format("project"), LABEL.format("service"), LABEL.format("oneoff")])


class Scratch:
    """The drill's scratch files, each made by `mktemp`: a new value is written to one by `openssl` in a child
    shell, never read into this process; `remove` deletes them all."""

    def __init__(self) -> None:
        self.paths: list[Path] = []

    def _file(self, command: str | None) -> Path:
        path = Path(run(["mktemp", "-t", "jh-rotation.XXXXXXXX"]).stdout.strip())
        self.paths.append(path)
        if command is not None:
            run(["sh", "-c", f'umask 077 && {command} > "$1"', "scratch", str(path)])
        return path

    def key(self) -> Path:
        """32 random bytes, base64: a data key or a session secret, as scripts/test-secrets.sh makes one."""
        return self._file("openssl rand -base64 32")

    def password(self) -> Path:
        return self._file("openssl rand -hex 24")

    def empty(self) -> Path:
        return self._file(None)

    def remove(self) -> None:
        for path in self.paths:
            path.unlink(missing_ok=True)


def as_root(*argv: str) -> subprocess.CompletedProcess[str]:
    """One of the drill's two root commands (M5-U6), from the checkout; `sudo -n` fails rather than ask."""
    done = subprocess.run(
        ["sudo", "-n", *argv], cwd=REPO, capture_output=True, text=True, timeout=600, check=False
    )
    if done.returncode != 0:
        pytest.fail(f"sudo {argv[0]} exited {done.returncode}:\n{done.stdout}\n{done.stderr}")
    return done


def install(scratch: Path, name: str) -> None:
    """The scratch file installed as PT's secret `name`, as production installs one (§19.8)."""
    as_root(
        "install", "-o", "root", "-g", str(SECRETS_GROUP), "-m", "0440", str(scratch), f"{SECRETS}/{name}"
    )


def healthy(argv: list[str], *services: str) -> None:
    def all_healthy() -> bool:
        listed = run([*argv, "ps", "--format", "{{.Service}} {{.Health}}", *services]).stdout
        states = dict(line.split(" ", 1) for line in listed.splitlines() if line.strip())
        return set(states) == set(services) and set(states.values()) == {"healthy"}

    wait_for(all_healthy, within_s=HEALTHY_WITHIN_S, what=f"{', '.join(services)} healthy", argv=argv)


def restart(*services: str) -> None:
    """`$PT restart` of `services` (Compose does not see a secret file's new content), until healthy."""
    run([*PT, "restart", *services], timeout=300)
    healthy(PT, *services)


def started() -> dict[tuple[str, str, str], str]:
    """Every running container of the host but a one-off, by project, service and name: when it last
    started."""
    ids = run(["docker", "ps", "--quiet"]).stdout.split()
    listed = run(["docker", "inspect", "--format", f"{STARTED}|{{{{.Name}}}}|{{{{.State.StartedAt}}}}", *ids])
    found = {}
    for line in listed.stdout.splitlines():
        project, service, oneoff, name, at = line.split("|")
        if oneoff != "True":
            found[(project, service, name.lstrip("/"))] = at
    return found


def in_api(argv: list[str], script: str, *, one_off: bool = False) -> Any:
    """`script` by the app's python in the site's api container (a one-off with `one_off`, its own
    mounts); the JSON it printed."""
    where = ["run", "--rm", "--no-deps", "-T", "api"] if one_off else ["exec", "-T", "api"]
    return last_json(run([*argv, *where, "python", "-I", "-c", script], timeout=300).stdout)


def crypto_rotate(argv: list[str]) -> tuple[int, Any]:
    """`jobhunter crypto rotate` in a one-off worker of the site, as the RUNBOOK runs it: its exit status and
    its report (key ids and counts)."""
    done = run(
        [*argv, "run", "--rm", "--no-deps", "-T", "worker", "jobhunter", "crypto", "rotate"], check=False
    )
    if done.returncode not in (0, 2, 3):
        pytest.fail(f"crypto rotate exited {done.returncode}:\n{done.stdout}\n{done.stderr}")
    return done.returncode, last_json(done.stdout)


def app_login(*where: str) -> subprocess.CompletedProcess[str]:
    return run([*PT, *where, "-T", "postgres", "sh", "-c", APP_LOGIN], check=False)


def secret_inode(name: str) -> str:
    """The inode of the api's mounted secret `name`, as the running container sees it."""
    return run([*PT, "exec", "-T", "api", "stat", "-c", "%i", f"/run/secrets/{name}"]).stdout.strip()


def download(site: Site, export_id: int) -> list[str]:
    """The export's zip, downloaded and opened: its file names."""
    answer = site.http.get(f"/api/privacy/exports/{export_id}", params={"download": "1"})
    assert answer.status_code == 200, (answer.status_code, answer.text[:200])
    return zipfile.ZipFile(io.BytesIO(answer.content)).namelist()


@pytest.fixture
def rotation_stack() -> Iterator[Scratch]:
    """The images built and postgres alone up (`prod_session("postgres")`), and the drill's scratch files; at
    the end, after a failure too, no rotation left in progress (`data_key_previous` empty again) and every
    scratch file removed, then the projects removed with their volumes."""
    scratch = Scratch()
    try:
        with prod_session("postgres"):
            try:
                yield scratch
            finally:
                install(scratch.empty(), "data_key_previous")
    finally:
        scratch.remove()


def test_rotation_drill(rotation_stack: Scratch) -> None:
    """R6, R7 and Deviation 31 on PT: the session secret, the data key and `db_app` rotated as the RUNBOOK
    does it, then a backup set from before the data-key rotation restored into PB and recovered with the
    kit's old key."""
    scratch = rotation_stack
    old_key, new_key = scratch.key(), scratch.key()
    install(old_key, "data_key")  # the drill's own old key, before any data exists
    install(scratch.empty(), "data_key_previous")
    up(PT, build=False)
    backup(PT, "init")
    create_user(PT, USERNAME, PASSWORD)
    [old_id] = in_api(PT, KEY_IDS)

    # R6: a new session secret. The running api keeps the file it mounted at its start until it restarts;
    # then the old session is gone.
    site = Site(PT)
    inode = secret_inode("session_secret")
    install(scratch.key(), "session_secret")
    assert secret_inode("session_secret") == inode
    assert site.http.get("/api/auth/session").status_code == 200
    restart("api")
    wait_for(lambda: site.http.get("/healthz").status_code == 200, within_s=120, what="the api through Caddy")
    assert secret_inode("session_secret") != inode
    assert site.http.get("/api/auth/session").status_code == 401  # the old cookie
    site = Site(PT)  # a new login works

    # The data under the old key, and a backup set of it.
    site.confirmed_profile()  # a CV file and its extracted text
    export_id = site.ready_export()
    set_id = int(backup(PT, "set")["set_id"])
    assert in_api(PT, OPENED) == {"keys": 1, "files": 1, "files_opened": 1, "texts": 1, "texts_opened": 1}

    # The data key: the old key mounted as data_key_previous, the new as data_key, then `crypto rotate`.
    install(old_key, "data_key_previous")
    install(new_key, "data_key")
    restart("api", "worker")
    [new_id, previous_id] = in_api(PT, KEY_IDS)
    assert previous_id == old_id != new_id
    code, report = crypto_rotate(PT)
    print("crypto rotate on PT:", code, report, "keys:", {"old": old_id, "new": new_id})
    assert (code, report) == (
        0,
        {"rewritten": {"cv_files": 1, "cv_texts": 1, "totp": 0, "exports": 1}, "remaining_by_key_id": {}},
    )
    assert in_api(PT, OPENED) == {"keys": 2, "files": 1, "files_opened": 1, "texts": 1, "texts_opened": 1}
    assert "README.txt" in download(site, export_id)
    install(scratch.empty(), "data_key_previous")  # the verification found no ciphertext under the old key
    restart("api", "worker")
    assert in_api(PT, KEY_IDS) == [new_id]
    assert in_api(PT, OPENED) == {"keys": 1, "files": 1, "files_opened": 1, "texts": 1, "texts_opened": 1}
    assert "README.txt" in download(Site(PT), export_id)

    # db_app: the role's password and the services that hold it, nothing else.
    assert app_login("exec").stdout.strip() == "1"  # the running postgres's mount: the old password
    before = started()
    assert {key[0] for key in before} >= {PT_PROJECT, S3_PROJECT}
    done = as_root(
        ROTATE_DB_ROLE,
        "--compose",
        shlex.join(PT),
        "--secrets-dir",
        SECRETS,
        "db_app",
        str(scratch.password()),
    )
    print("rotate-db-role:", done.stdout)
    assert (
        done.stdout.splitlines()[-1]
        == "rotate-db-role: done: jobhunter_app has its new password; restarted api worker"
    )
    healthy(PT, "api", "worker")
    after = started()
    moved = {key for key in before if after.get(key) != before[key]}
    assert {(project, service) for project, service, _ in moved} == {
        (PT_PROJECT, "api"),
        (PT_PROJECT, "worker"),
    }
    assert after.keys() == before.keys()  # the S3 stand-in's containers, and every other one, kept running
    refused = app_login("exec")  # the running postgres still mounts the old file
    assert refused.returncode != 0 and "password authentication failed" in refused.stderr, refused.stderr
    assert app_login("run", "--rm", "--no-deps").stdout.strip() == "1"  # a fresh mount: the new password
    site = Site(PT)  # login works
    ready(site)
    assert readyz(site) == (200, {})

    # Deviation 31: site A lost; the set from before the rotation restored into B, recovered with the old key.
    down_v(PT)
    install(old_key, "data_key_previous")  # the recovery kit's historical key; data_key is the new one
    up(PB, "postgres", build=False)
    command = [*PB, "--profile", "restore", "run", "--rm", "-T", "restore", "jobhunter", "restore", "run"]
    restored = run([*command, "--set", str(set_id)], timeout=RESTORE_TIMEOUT_S)
    assert last_json(restored.stdout) == {"restored": True, "set_id": set_id, "data_key_ids": [old_id]}
    code, report = crypto_rotate(PB)
    print("crypto rotate on PB:", code, report)
    assert (code, report) == (
        0,
        {"rewritten": {"cv_files": 1, "cv_texts": 1, "totp": 0, "exports": 0}, "remaining_by_key_id": {}},
    )
    assert in_api(PB, OPENED, one_off=True) == {
        "keys": 2,
        "files": 1,
        "files_opened": 1,
        "texts": 1,
        "texts_opened": 1,
    }
