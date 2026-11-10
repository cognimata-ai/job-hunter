"""LangSmith with hidden inputs and outputs, one anonymizer and the published tracing state (spec §17.8; plan
M5 Task 6, O13-O16, Q15-Q17, Rulings T6-1, T6-2).

The transforms are driven through the real hooks of langsmith 0.14.11's `Client` (`_hide_run_inputs`,
`_hide_run_outputs`, `_hide_run_metadata`, `_hide_run_error` and `_run_transform`, which applies all four to a
run), on the client `make_client` builds. Its HTTP session sends every request to an adapter that fails it
before any socket is opened, so nothing here reaches the network: the client's own control thread asks for
`/info` once at its start, and that request is refused too. `LANGSMITH_TRACING` is never set in the process
environment: `make_client` reads the configuration's.
"""

import threading
from pathlib import Path
from typing import Any, Final

import pytest
import requests
from jobhunter.config import LANGSMITH_EU_ENDPOINT, AppConfig, load_config
from jobhunter.errors import NodeFailed
from jobhunter.observability.langsmith import (
    METADATA_ALLOWLIST,
    TracingState,
    anonymize,
    error_tag,
    install_tracing,
    make_client,
)
from langchain_core.tracers.core import _TracerCore
from langsmith import Client, run_trees
from requests.adapters import BaseAdapter

KEY: Final = "lsv2-test-key"  # a low-entropy stand-in: no secret scanner reads it as a key
TRACING_ENV: Final = {
    "LANGSMITH_TRACING": "true",
    "LANGSMITH_ENDPOINT": LANGSMITH_EU_ENDPOINT,
    "LANGSMITH_HIDE_INPUTS": "true",
    "LANGSMITH_HIDE_OUTPUTS": "true",
}
CONTENT: Final = "Senior engineer, Musterstrasse 1, 12345 Berlin; my CV says"  # what must never be traced


class RefusingAdapter(BaseAdapter):
    """Fails every request it is given before any connection is made; records the thread that asked."""

    def __init__(self) -> None:
        super().__init__()
        self.threads: list[int] = []

    def send(self, request: requests.PreparedRequest, *args: Any, **kwargs: Any) -> requests.Response:
        self.threads.append(threading.get_ident())
        raise requests.ConnectionError("no network in the unit tests")

    def close(self) -> None:
        pass


class RefusingSession(requests.Session):
    """A session whose every URL goes to one refusing adapter, whatever adapters the client mounts on it."""

    def __init__(self, adapter: RefusingAdapter) -> None:
        super().__init__()
        self.refusing = adapter

    def get_adapter(self, url: str) -> BaseAdapter:
        return self.refusing


def worker_config(tmp_path: Path, env: dict[str, str] | None = None, *, key: str | None = KEY) -> AppConfig:
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    files = {"db_app": "pw", "data_key": "a" * 43 + "=", **({"langsmith_key": f"{key}\n"} if key else {})}
    for name, value in files.items():
        (secrets / name).write_text(value)
        (secrets / name).chmod(0o440)
    base = {"JOBHUNTER_ENV": "test", "JOBHUNTER_DB_NAME": "jobhunter", "JOBHUNTER_DB_HOST": "unused"}
    return load_config("worker", {**base, **(env or {})}, secrets)


@pytest.fixture
def refusing(monkeypatch: pytest.MonkeyPatch) -> RefusingAdapter:
    """Every `requests.Session` made while the test runs is a refusing one (the client makes its own)."""
    adapter = RefusingAdapter()
    monkeypatch.setattr(requests, "Session", lambda: RefusingSession(adapter))
    return adapter


@pytest.fixture
def client(tmp_path: Path, refusing: RefusingAdapter) -> Any:
    made = make_client(worker_config(tmp_path, TRACING_ENV))
    assert isinstance(made, Client)
    yield made
    made.close(timeout=0)  # stops its control thread without draining anything


def a_run(**fields: Any) -> dict[str, Any]:
    """A chain run as langchain-core's tracer hands it to the client, with content in every field it can."""
    return {
        "id": "5b2c0f53-4a35-4d39-9a83-0c2f4d6b8e11",
        "name": "generate",
        "run_type": "chain",
        "inputs": {"cv_text": CONTENT, "messages": [{"role": "user", "content": CONTENT}]},
        "outputs": {"profile": {"summary": CONTENT}},
        "extra": {"metadata": {"langgraph_node": "generate", "thread_id": "op-17", "cv": CONTENT}},
        **fields,
    }


def test_inputs_and_outputs_become_empty_dicts(client: Any) -> None:
    assert client._hide_inputs is True and client._hide_outputs is True
    assert client._hide_run_inputs({"cv_text": CONTENT}) == {}
    assert client._hide_run_outputs({"profile": {"summary": CONTENT}}) == {}
    # the whole run as the client sends it: no field keeps the content
    sent = client._run_transform(a_run(error=f"ValueError('{CONTENT}')"))
    assert (sent["inputs"], sent["outputs"]) == ({}, {})
    assert sent["extra"]["metadata"] == {"node": "generate"}
    assert sent["error"] == "ValueError:unknown"
    assert CONTENT not in repr(sent) and "Musterstrasse" not in repr(sent)


def test_metadata_is_the_allowlist_with_node_renamed(client: Any) -> None:
    metadata = {
        "run_id": 41,
        "cycle_id": 7,
        "target_id": 3001,
        "attempt_no": 2,
        "langgraph_node": "score_one",
        "langgraph_step": 4,
        "langgraph_path": ["__pregel_pull", "score_one"],
        "langgraph_checkpoint_ns": "score_one:1f0",
        "thread_id": "cycle-7",
        "checkpoint_ns": "score_one:1f0",
        "model": "claude-test-model",
        "effort": "medium",
        "prompt_schema_hash": "9f1c",
        "profile_version_id": 12,
        "ls_provider": "anthropic",
        "revision_id": "0.1.0",
        "ls_tracing_sample_rate": 1.0,
        "note": CONTENT,
    }
    expected = {
        "run_id": 41,
        "cycle_id": 7,
        "target_id": 3001,
        "attempt_no": 2,
        "node": "score_one",
        "model": "claude-test-model",
        "effort": "medium",
        "prompt_schema_hash": "9f1c",
        "profile_version_id": 12,
    }
    assert client._hide_run_metadata(metadata) == expected
    assert anonymize(metadata) == expected
    assert set(expected) | {"usage_metadata"} == METADATA_ALLOWLIST
    # a node named by the caller's own metadata stays as it is
    assert anonymize({"node": "extract", "langgraph_node": "extract"}) == {"node": "extract"}
    assert anonymize({}) == {}


def test_usage_metadata_keeps_token_counts_only(client: Any) -> None:
    usage = {
        "input_tokens": 350,
        "output_tokens": 240,
        "total_tokens": 590,
        "input_token_details": {"cache_creation": 200, "cache_read": 100, "note": CONTENT},
        "output_token_details": {"reasoning": 20, "flag": True},
        "input_cost": 0.0012,
        "model_note": CONTENT,
        "nested": {"deeper": {"tokens": 5}},
        "cached": True,
    }
    assert client._hide_run_metadata({"usage_metadata": usage, "langgraph_node": "generate"}) == {
        "usage_metadata": {
            "input_tokens": 350,
            "output_tokens": 240,
            "total_tokens": 590,
            "input_token_details": {"cache_creation": 200, "cache_read": 100},
            "output_token_details": {"reasoning": 20},
        },
        "node": "generate",
    }
    # anything else under the key holds no count
    assert anonymize({"usage_metadata": CONTENT}) == {"usage_metadata": {}}


@pytest.mark.parametrize(
    ("error", "tag"),
    [
        pytest.param(
            "NodeFailed(node='generate', error_class='RateLimitError')\nTraceback (most recent call last):\n"
            '  File "/app/graph.py", line 7, in generate\n'
            f"    raise NodeFailed('{CONTENT}')\njobhunter.errors.NodeFailed: {CONTENT}",
            "NodeFailed:RateLimitError",
            id="repr_then_traceback",
        ),
        # Ruling T6-5: a `NodeFailed` repr gives its code only in exactly the form `NodeFailed` makes
        pytest.param(
            "NodeFailed('node generate failed: RateLimitError extra words')",
            "NodeFailed:unknown",
            id="node_failed_extra_words",
        ),
        pytest.param(
            "NodeFailed('node generate failed: RateLimitError') and more",
            "NodeFailed:unknown",
            id="node_failed_text_after",
        ),
        pytest.param(
            'NodeFailed("node generate failed: RateLimitError")',
            "NodeFailed:unknown",
            id="node_failed_quotes",
        ),
        pytest.param(
            "ValueError('node x failed: Secret')", "ValueError:unknown", id="fixed_form_other_class"
        ),
        pytest.param("Authorization: Bearer x", "Unknown:unknown", id="header"),
        pytest.param(
            "HTTPError(request=<Request headers={'Authorization': 'Bearer x'}>)",
            "HTTPError:unknown",
            id="nested",
        ),
        pytest.param(
            "NodeFailed(node='generate', error_class='Bearer x')", "NodeFailed:unknown", id="quoted_space"
        ),
        pytest.param("NodeFailed(error_class=Bearer x)", "NodeFailed:unknown", id="unquoted_space"),
        pytest.param(
            "jobhunter.graphs.sanitize.NodeFailed(node='extract', error_class='TimeoutError')",
            "NodeFailed:TimeoutError",
            id="dotted_class",
        ),
        pytest.param("LlmError(code='rate_limited', detail='x')", "LlmError:rate_limited", id="code"),
        pytest.param('ApiError(error_code="http_429")', "ApiError:http_429", id="error_code"),
        pytest.param("ApiError(code=429)", "ApiError:unknown", id="number"),  # a number is no identifier
        pytest.param(f"ApiError(code='{'x' * 65}')", "ApiError:unknown", id="too_long"),  # past 64 characters
        pytest.param(
            "ApiError(barcode='abc')", "ApiError:unknown", id="longer_name"
        ),  # only the three keywords
        # a value runs to `,` or `)`
        pytest.param("ValueError('the error_code=abc is ours')", "ValueError:unknown", id="in_a_message"),
        pytest.param("NodeFailed(error_class='A', code='B')", "NodeFailed:A", id="first_keyword"),
        pytest.param("NodeFailed(error_class='a b', code='B')", "NodeFailed:unknown", id="first_only"),
        pytest.param(" ValueError('x')", "Unknown:unknown", id="not_leading"),  # the class leads the string
        pytest.param("ValueError: x", "Unknown:unknown", id="no_parenthesis"),  # no `(` after it: no class
        pytest.param("", "Unknown:unknown", id="empty"),
    ],
)
def test_the_error_string_becomes_class_and_code(client: Any, error: str, tag: str) -> None:
    assert error_tag(error) == tag
    assert client._hide_run_error(error) == tag
    assert anonymize({"error": error}) == {"error": tag}
    assert "Bearer" not in tag and CONTENT not in tag


def test_a_node_failure_gives_the_class_of_its_message_as_its_code(client: Any) -> None:
    """Ruling T6-5: the real `NodeFailed`'s repr, `NodeFailed('node <node> failed: <ErrorClass>')`, built from
    the class itself, so a change of its message breaks this test. The message holds a class name only
    (`graphs.sanitize`)."""
    error = f"{NodeFailed('generate', 'RateLimitError')!r}\nTraceback (most recent call last):\n  {CONTENT}"
    assert error_tag(error) == "NodeFailed:RateLimitError"
    assert anonymize({"error": error}) == {"error": "NodeFailed:RateLimitError"}
    assert client._hide_run_error(error) == "NodeFailed:RateLimitError"


def test_a_node_failure_as_langchain_core_traces_it_gives_its_code(client: Any) -> None:
    """langchain-core's own error string (`_get_stacktrace`): the repr, then the formatted traceback with no
    line break before its `Traceback (most recent call last):` header."""
    with pytest.raises(NodeFailed) as caught:
        raise NodeFailed("generate", "RateLimitError")
    error = _TracerCore._get_stacktrace(caught.value)
    assert error.startswith(
        "NodeFailed('node generate failed: RateLimitError')Traceback (most recent call last):"
    )
    assert error_tag(error) == "NodeFailed:RateLimitError"
    assert client._hide_run_error(error) == "NodeFailed:RateLimitError"


def test_an_error_that_is_no_string_keeps_nothing(client: Any) -> None:
    assert anonymize({"error": {"message": CONTENT}}) == {"error": "Unknown:unknown"}
    assert client._hide_run_error(["x", CONTENT]) == "Unknown:unknown"


@pytest.mark.parametrize("env", [{}, {**TRACING_ENV, "LANGSMITH_TRACING": "false"}])
def test_make_client_is_none_without_tracing(
    tmp_path: Path, refusing: RefusingAdapter, env: dict[str, str]
) -> None:
    assert make_client(worker_config(tmp_path, env)) is None
    assert refusing.threads == []  # no client, so not even its `/info`


def test_the_client_uses_the_eu_endpoint(client: Any) -> None:
    assert client.api_url == LANGSMITH_EU_ENDPOINT
    assert list(client._write_api_urls) == [LANGSMITH_EU_ENDPOINT]  # no write replica
    assert client.api_key == KEY  # the secret file's value, without its newline
    assert client._anonymizer is anonymize and client.tracing_sample_rate is None


def test_no_request_is_made_by_the_transforms(client: Any, refusing: RefusingAdapter) -> None:
    assert client.session.get_adapter(f"{LANGSMITH_EU_ENDPOINT}/runs") is refusing
    me = threading.get_ident()
    client._run_transform(a_run(error="NodeFailed(node='generate', error_class='RateLimitError')"))
    client._run_transform(a_run(), update=True, copy=True)
    client._hide_run_inputs({"cv_text": CONTENT})
    client._hide_run_outputs({"profile": CONTENT})
    client._hide_run_metadata({"langgraph_node": "generate", "usage_metadata": {"input_tokens": 1}})
    client._hide_run_error("Authorization: Bearer x")
    assert me not in refusing.threads  # had a transform asked, the refusal would also have raised here


def test_install_tracing_configures_the_process_wide_client(
    tmp_path: Path, refusing: RefusingAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured: list[dict[str, Any]] = []
    monkeypatch.setattr(run_trees, "configure", lambda **kwargs: configured.append(kwargs))
    state = install_tracing(worker_config(tmp_path, TRACING_ENV))
    assert state == TracingState(tracing=True, region="eu")
    [kwargs] = configured
    installed = kwargs["client"]
    assert kwargs == {"client": installed, "enabled": True}
    assert isinstance(installed, Client) and installed.api_url == LANGSMITH_EU_ENDPOINT
    assert installed._anonymizer is anonymize
    installed.close(timeout=0)


def test_without_tracing_install_configures_nothing(
    tmp_path: Path, refusing: RefusingAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured: list[dict[str, Any]] = []
    monkeypatch.setattr(run_trees, "configure", lambda **kwargs: configured.append(kwargs))
    assert install_tracing(worker_config(tmp_path)) == TracingState(tracing=False, region=None)
    assert configured == [] and refusing.threads == []
