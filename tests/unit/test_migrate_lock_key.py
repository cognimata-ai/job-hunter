import ast
import inspect

from jobhunter.auth.throttle import THROTTLE_LOCK_KEY
from jobhunter.backup import restore_test, shipper
from jobhunter.backup.backup_set import BACKUP_JOB_LOCK_KEY
from jobhunter.backup.shipper import SHIP_LOCK_KEY
from jobhunter.db import migrate
from jobhunter.db.migrate import LOCK_ORDER, MIGRATE_LOCK_KEY
from jobhunter.observability.deadman import DEADMAN_LOCK_KEY
from jobhunter.worker.lock import ADVISORY_LOCK_KEY

WORKER_SINGLETON_KEY = 7261001
LOGIN_THROTTLE_KEY = 7261002


def test_the_migrate_lock_key_is_its_own() -> None:
    """Session and transaction advisory locks on one key conflict: migrate must not share a key."""
    assert MIGRATE_LOCK_KEY not in {WORKER_SINGLETON_KEY, LOGIN_THROTTLE_KEY}
    assert MIGRATE_LOCK_KEY == 7261003


def test_every_one_key_advisory_lock_has_a_key_of_its_own() -> None:
    """The one-key advisory locks `db.migrate` lists, each a distinct key (the two-key company lock, 7261004,
    is another key space): the shipment lock (final review C1) is 7261007."""
    keys = {
        "worker singleton": ADVISORY_LOCK_KEY,
        "login throttle": THROTTLE_LOCK_KEY,
        "migrate": MIGRATE_LOCK_KEY,
        "dead-man switch": DEADMAN_LOCK_KEY,
        "backup jobs": BACKUP_JOB_LOCK_KEY,
        "manifest shipment": SHIP_LOCK_KEY,
    }
    assert len(set(keys.values())) == len(keys)
    assert sorted(keys.values()) == [7261001, 7261002, 7261003, 7261005, 7261006, 7261007]


def test_a_shipper_never_takes_the_job_lock_the_prune_holds_around_the_shipment_lock() -> None:
    """The one pair of the backup container's locks a process holds at once (the worker holds 7261001 with
    7261005 and 7261004, `db.migrate`'s note, final fix E3): the prune takes the shipment lock (7261007)
    inside its backup job lock (7261006) (final fix D1 (a); `tests/integration/test_backup_set.py` watches it
    hold both). `db.migrate` registers that order, and the shipper names no job lock, so a shipper holding
    7261007 never waits for 7261006: no cycle can form."""
    assert LOCK_ORDER == ((BACKUP_JOB_LOCK_KEY, SHIP_LOCK_KEY),)
    tree = ast.parse(inspect.getsource(shipper))
    named = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    named |= {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    named |= {
        alias.name for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) for alias in node.names
    }
    imported = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert not named & {"BACKUP_JOB_LOCK_KEY", "backup_job_lock"}
    assert "jobhunter.backup.backup_set" not in imported
    assert str(BACKUP_JOB_LOCK_KEY) not in inspect.getsource(shipper)


def _lock_order_note() -> str:
    """`db.migrate`'s comment on the lock order, joined into one line."""
    source = inspect.getsource(migrate)
    start = source.index("# The lock order")
    lines = source[start : source.index("LOCK_ORDER: Final")].splitlines()
    return " ".join(line.strip().removeprefix("#").strip() for line in lines)


def test_the_restore_tests_migrate_lock_is_on_the_scratch_database_and_the_note_says_so() -> None:
    """The final re-review's M4-4: the restore test holds the backup jobs' lock (7261006) on the production
    database while its in-process `migrate` takes the migrate lock (7261003) on the scratch database.
    Advisory locks are per database and nothing else connects to the scratch one, so the pair forms no
    cycle; the restore test's one `migrate` names that database, and `db.migrate`'s note covers the pair."""
    tree = ast.parse(inspect.getsource(restore_test))
    [call] = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "run_migrate_async"
    ]
    [database] = [keyword.value for keyword in call.keywords if keyword.arg == "database"]
    assert isinstance(database, ast.Name) and database.id == "RESTORE_TEST_DB"
    note = _lock_order_note()
    assert "the restore test holds 7261006" in note
    assert "takes 7261003 on its scratch database" in note
    assert "advisory locks are per database" in note
