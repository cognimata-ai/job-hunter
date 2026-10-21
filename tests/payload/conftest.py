"""The LLM payload contract tests (spec §10.9, §18.5): every test collected here is marked `payload`.

They run offline, with no database: the `payload` CI job has no PostgreSQL. A request is made by one
`gateway._chat` try on a capturing transport (`tests/support/llm.capture_request`), never by
`LLMGateway.call`, which reserves in the database.
"""

from pathlib import Path

import pytest
from jobhunter.config import AppConfig, load_config

_HERE = Path(__file__).resolve().parent


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    for item in items:
        if _HERE in Path(str(item.path)).resolve().parents:
            item.add_marker(pytest.mark.payload)


@pytest.fixture
def payload_cfg(tmp_path: Path) -> AppConfig:
    """A worker config with no database and no secrets: the http client is all a payload test builds."""
    return load_config("worker", {"JOBHUNTER_ENV": "test", "JOBHUNTER_DB_NAME": "payload"}, tmp_path / "none")
