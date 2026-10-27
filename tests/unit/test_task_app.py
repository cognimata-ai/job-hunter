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
            )
        )
    finally:
        _reset_task_app()
