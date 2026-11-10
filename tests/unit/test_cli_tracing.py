"""Only the worker traces (spec §17.8; plan M5 Task 6, Ruling T6-8, review I1).

`cli.main` turns LangSmith tracing off before any command but `worker` runs (`observability.langsmith.
disable_tracing`), so a command started in the worker's environment, which the prod compose gives
`LANGSMITH_TRACING=true`, such as `schema-dry-run`, never traces through the SDK's default client (no
anonymizer). The environment is set for the test only; no LangSmith client may be built and nothing reaches
the network. The SDK's cached reading of the environment and its process-wide switch are restored afterwards,
and the switch's context variable is set inside a copied context only.
"""

import contextvars
import threading
from collections.abc import Callable, Iterator
from typing import Any, cast

import pytest
from jobhunter import cli
from jobhunter.observability import langsmith as tracing
from langchain_core.callbacks.manager import CallbackManager
from langchain_core.tracers.context import _tracing_v2_is_enabled
from langchain_core.tracers.langchain import LangChainTracer
from langsmith import run_trees
from langsmith import utils as ls_utils
from langsmith._internal import _context


def _no_client(**kwargs: Any) -> Any:
    raise AssertionError("no LangSmith client may be built here")


def _clear_env_cache() -> None:
    """The SDK caches its reading of each environment variable (`utils.get_env_var`, an `lru_cache`)."""
    cast(Any, ls_utils.get_env_var).cache_clear()


@pytest.fixture
def traced_environment() -> Iterator[None]:
    """`LANGSMITH_TRACING=true` in the process environment, read afresh by the SDK, for this test only."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("LANGSMITH_TRACING", "true")
        mp.setattr(_context, "_GLOBAL_TRACING_ENABLED", None)  # unset: the environment decides
        mp.setattr(run_trees, "_CLIENT", run_trees._CLIENT)
        mp.setattr(run_trees, "get_cached_client", _no_client)  # the SDK's default client, from the env
        _clear_env_cache()
        yield
    _clear_env_cache()  # the environment is restored: nothing cached from the test's


def in_a_new_thread(read: Callable[[], object]) -> object:
    """What `read` returns in a thread of its own, which starts with an empty context."""
    seen: list[object] = []
    thread = threading.Thread(target=lambda: seen.append(read()))
    thread.start()
    thread.join(timeout=10)
    return seen[0]


def test_a_command_other_than_the_worker_runs_untraced_whatever_the_environment_says(
    traced_environment: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`schema-dry-run`, the review's case, with its own work faked and the real `disable_tracing`."""
    from jobhunter import cli_schema_dry_run

    monkeypatch.setattr(cli_schema_dry_run, "run_schema_dry_run", lambda args: 0)

    def run() -> None:
        _context._TRACING_ENABLED.set(None)  # unset in this copied context: the environment decides
        # the environment is live: the SDK and langchain-core would trace
        assert ls_utils.tracing_is_enabled() is True and _tracing_v2_is_enabled() is True
        assert cli.main(["schema-dry-run", "--live"]) == 0
        # off for this context, and for any thread the process starts later
        assert ls_utils.tracing_is_enabled() is False and _tracing_v2_is_enabled() is False
        assert in_a_new_thread(ls_utils.tracing_is_enabled) is False
        # langchain-core makes no tracer for a run (were it to, it would ask for a client and fail)
        handlers = CallbackManager.configure().handlers
        assert not any(isinstance(handler, LangChainTracer) for handler in handlers)

    contextvars.copy_context().run(run)


@pytest.mark.parametrize(
    ("argv", "module", "name"),
    [
        (["schema-dry-run", "--live"], "jobhunter.cli_schema_dry_run", "run_schema_dry_run"),
        (["api"], "jobhunter.cli_api", "run_api"),
        (["ops", "notify-test"], "jobhunter.cli_ops", "run_ops_command"),
        (["startup-check", "--role", "api"], "jobhunter.cli", "startup_or_exit"),
    ],
    ids=["schema-dry-run", "api", "ops", "startup-check"],
)
def test_tracing_is_turned_off_before_the_command_runs(
    monkeypatch: pytest.MonkeyPatch, argv: list[str], module: str, name: str
) -> None:
    import importlib

    calls: list[str] = []

    def command(*args: object) -> int:
        calls.append(name)
        return 0

    monkeypatch.setattr(tracing, "disable_tracing", lambda: calls.append("disable_tracing"))
    monkeypatch.setattr(importlib.import_module(module), name, command)
    assert cli.main(argv) == 0
    assert calls == ["disable_tracing", name]


@pytest.mark.parametrize(
    ("argv", "name"),
    [(["http-check", "http://127.0.0.1:1/healthz"], "_http_check"), (["worker-health"], "_worker_health")],
    ids=["http-check", "worker-health"],
)
def test_the_healthcheck_probes_leave_the_tracing_sdk_alone(
    monkeypatch: pytest.MonkeyPatch, argv: list[str], name: str
) -> None:
    """Ruling T6-9: the two probes load neither langchain nor langsmith, which their import guard tests pin
    (`test_cli_http_check.py`, `test_worker_singleton.py`), so they cannot trace and are not made to load the
    SDK only to switch it off."""
    calls: list[str] = []

    def probe(*args: object) -> int:
        calls.append(name)
        return 0

    monkeypatch.setattr(tracing, "disable_tracing", lambda: calls.append("disable_tracing"))
    monkeypatch.setattr(cli, name, probe)
    assert cli.main(argv) == 0
    assert calls == [name]
    assert frozenset({"http-check", "worker-health"}) == cli.UNTRACEABLE_PROBES


def test_the_worker_command_installs_its_own_tracing(monkeypatch: pytest.MonkeyPatch) -> None:
    """The worker installs its client itself (`install_tracing`, step 7 of its start): the CLI leaves it."""
    from jobhunter.worker import main as worker

    calls: list[str] = []

    async def worker_main(cfg: object) -> int:
        calls.append("worker_main")
        return 0

    monkeypatch.setattr(tracing, "disable_tracing", lambda: calls.append("disable_tracing"))
    monkeypatch.setattr(cli, "startup_or_exit", lambda role: calls.append(f"startup_or_exit:{role}"))
    monkeypatch.setattr(worker, "worker_main", worker_main)
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["worker"])
    assert exit_info.value.code == 0
    assert calls == ["startup_or_exit:worker", "worker_main"]
