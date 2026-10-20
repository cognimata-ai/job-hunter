"""Contract tests on the scrubbed fixtures (spec §18.4): every test collected here is marked `contract`.

The fixture tree is read relative to the repository root (`tests/contract/fixtures`), where pytest runs.
"""

from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    for item in items:
        if _HERE in Path(str(item.path)).resolve().parents:
            item.add_marker(pytest.mark.contract)
