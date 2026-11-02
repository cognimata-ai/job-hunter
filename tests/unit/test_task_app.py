import pytest
from jobhunter.worker.tasks import _reset_task_app, job_task_name, make_queue_app, task_app

_DSN = "dbname=unused"


def test_reset_task_app_allows_a_fresh_task_app() -> None:
    _reset_task_app()  # whatever an earlier test left
    try:
        first = make_queue_app(_DSN)
        assert make_queue_app(_DSN) is first  # first call wins; later calls with its dsn return it
        assert job_task_name("tick") in first.tasks
        other = make_queue_app("dbname=other")  # another dsn: a separate app without the tasks
        assert other is not first
        assert job_task_name("tick") not in other.tasks
        _reset_task_app()
        with pytest.raises(RuntimeError):
            task_app()
        second = make_queue_app("dbname=other")
        assert second is not first and task_app() is second
        # The tasks were namespaced once, not twice.
        assert sorted(name for name in second.tasks if name.startswith("jh")) == sorted(
            job_task_name(name)
            for name in (
                "run_collect",
                "run_operation",
                "tick",
                "reconcile_heavy",
                "retention_daily",
                "score_cycle",  # M3 Task 11
                "batch_poll",  # M3 Task 17
                "batch_reconcile_ambiguous",  # M3 Task 17
                "canary_daily",  # M3 Task 20
            )
        )
    finally:
        _reset_task_app()


def test_the_batch_tasks_are_periodic_on_aux_with_their_queueing_locks() -> None:
    """M3 Task 17 (A2 B16, Deviation 27): `batch_poll` every 5 minutes and `batch_reconcile_ambiguous` every
    15, both on `aux`, each with the queueing lock of its name (one waiting job each)."""
    _reset_task_app()
    try:
        periodic = make_queue_app(_DSN).periodic_registry.periodic_tasks
        found = {
            name: (task.cron, task.task.queue, task.task.queueing_lock)
            for (name, _), task in periodic.items()
            if name in (job_task_name("batch_poll"), job_task_name("batch_reconcile_ambiguous"))
        }
        assert found == {
            job_task_name("batch_poll"): ("*/5 * * * *", "aux", "batch_poll"),
            job_task_name("batch_reconcile_ambiguous"): ("*/15 * * * *", "aux", "batch_reconcile_ambiguous"),
        }
    finally:
        _reset_task_app()
