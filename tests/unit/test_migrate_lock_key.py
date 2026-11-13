from jobhunter.auth.throttle import THROTTLE_LOCK_KEY
from jobhunter.backup.backup_set import BACKUP_JOB_LOCK_KEY
from jobhunter.backup.shipper import SHIP_LOCK_KEY
from jobhunter.db.migrate import MIGRATE_LOCK_KEY
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
