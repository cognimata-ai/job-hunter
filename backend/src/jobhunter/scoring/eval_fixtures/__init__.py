"""The packaged eval fixtures (spec §11.13, §18.6; M3 plan Task 19, Ruling P39): data files that ship in the
wheel, so the dev stack's installed package seeds them (`jobhunter golden seed-adversarial`).

`adversarial/` holds the adversarial golden cases, one JSON file per case (`scoring.golden.Fixture`):
synthetic ads and profiles of invented people, which the PII gate (`tests/contract/test_fixture_pii.py`)
scans. `adversarial_dir()` is that directory on disk.

A shipped fixture is immutable: an installation seeds each fixture once, by its `key`, and a re-seed that
finds the fixture's stored case no longer matching it fails (`golden.FixtureChanged`). A changed case is a
new file with a new key; the old one stays as it shipped.
"""

from importlib import resources
from pathlib import Path


def adversarial_dir() -> Path:
    """The directory of the packaged adversarial fixtures."""
    return Path(str(resources.files(__name__).joinpath("adversarial")))
