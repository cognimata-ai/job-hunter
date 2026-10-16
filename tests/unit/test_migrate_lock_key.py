from jobhunter.db.migrate import MIGRATE_LOCK_KEY

WORKER_SINGLETON_KEY = 7261001
LOGIN_THROTTLE_KEY = 7261002


def test_the_migrate_lock_key_is_its_own() -> None:
    """Session and transaction advisory locks on one key conflict: migrate must not share a key."""
    assert MIGRATE_LOCK_KEY not in {WORKER_SINGLETON_KEY, LOGIN_THROTTLE_KEY}
    assert MIGRATE_LOCK_KEY == 7261003
