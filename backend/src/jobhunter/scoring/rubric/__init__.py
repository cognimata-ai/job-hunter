"""The shipped rubric versions (spec §11.2, §11.3; `rubric_versions`, §7.3): v1 (M2) and v2 (M3, R13: v1 with
the `languages` anchor of an `unspecified` mandatory level reading as the §11.4 knockout table does).
`RUBRIC_SHIPPED` (v2) is the version a fresh install's `active_config` points at; `seed_defaults` stores both
and never moves a pointer already set.

`load_rubric(name)` (`"v1"` or `"v2"`) reads the packaged `<name>.yaml` with `yaml.safe_load` and validates it
(`parse_rubric`):
- `version` names the file;
- `weights` has exactly the eight §11.2 keys, each an integer, summing to 100 (`Rubric.weights` keeps the
  §11.2 order);
- `anchors` is an object with an entry for each of the six LLM-rated criteria.

`Rubric.content_hash` is the content hash of `{"weights", "anchors"}`, the `rubric_versions.content_hash` of
the rows `seed_defaults` inserts.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from functools import cached_property
from importlib import resources
from types import MappingProxyType
from typing import Final

import yaml

from jobhunter.db.seed import content_hash
from jobhunter.types import JSONValue

# §11.2, in its order: the six LLM-rated criteria, then the two deterministic ones.
LLM_CRITERIA: Final = (
    "role_fit",
    "skills",
    "experience_seniority",
    "domain",
    "languages",
    "education_certifications",
)
RUBRIC_KEYS: Final = (*LLM_CRITERIA, "location_work_mode", "employment_salary")
WEIGHT_TOTAL: Final = 100
RUBRIC_V1: Final = "v1"
RUBRIC_V2: Final = "v2"
RUBRIC_SHIPPED: Final = RUBRIC_V2
_VERSION_NAME: Final = re.compile(r"v[1-9][0-9]*")


@dataclass(frozen=True)
class Rubric:
    """A validated rubric version: the criterion weights and the anchors rendered into the rubric block."""

    weights: Mapping[str, int]
    anchors: Mapping[str, JSONValue]

    @cached_property
    def content_hash(self) -> bytes:
        """The SHA-256 of the canonical JSON of `{"weights", "anchors"}`."""
        return content_hash({"weights": dict(self.weights), "anchors": dict(self.anchors)})


def parse_rubric(document: str, *, name: str) -> Rubric:
    """The rubric `name` from its YAML text; `ValueError` names the first rule it breaks."""
    loaded = yaml.safe_load(document)
    if not isinstance(loaded, dict):
        raise ValueError(f"rubric {name}: the document is no mapping")
    if loaded.get("version") != name:
        raise ValueError(f"rubric {name}: the file names version {loaded.get('version')!r}")
    weights = loaded.get("weights")
    if not isinstance(weights, dict) or set(weights) != set(RUBRIC_KEYS):
        found = list(weights) if isinstance(weights, dict) else weights
        raise ValueError(f"rubric {name}: the weights' keys must be exactly {list(RUBRIC_KEYS)}, got {found}")
    for key, weight in weights.items():
        if isinstance(weight, bool) or not isinstance(weight, int):
            raise ValueError(f"rubric {name}: the weight of {key} must be an integer, got {weight!r}")
    if (total := sum(weights.values())) != WEIGHT_TOTAL:
        raise ValueError(f"rubric {name}: the weights sum to {total}, not {WEIGHT_TOTAL}")
    anchors = loaded.get("anchors")
    if not isinstance(anchors, dict) or not all(isinstance(anchors.get(key), dict) for key in LLM_CRITERIA):
        raise ValueError(f"rubric {name}: the anchors must hold an object for each of {list(LLM_CRITERIA)}")
    ordered = {key: weights[key] for key in RUBRIC_KEYS}
    return Rubric(weights=MappingProxyType(ordered), anchors=MappingProxyType(dict(anchors)))


def load_rubric(name: str) -> Rubric:
    """The shipped rubric version `name` (`"v1"` or `"v2"`), validated."""
    if not _VERSION_NAME.fullmatch(name):
        raise ValueError(f"no rubric version {name!r}: a version is named v1, v2, …")
    resource = resources.files(__name__).joinpath(f"{name}.yaml")
    if not resource.is_file():
        raise ValueError(f"no rubric version {name!r} is shipped")
    return parse_rubric(resource.read_text("utf-8"), name=name)
