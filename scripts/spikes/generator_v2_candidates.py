"""The candidate wire schemas of prompt-schema version 2's generator (M3 pre-plan spike V4b,
`docs/spikes/V4b-generator-v2.md`).

The API rejects the §11.5 generator schema (prompt-schema version 1, `llm/schemas/generator.py`) with HTTP 400
"The compiled grammar is too large…" (`docs/spikes/V4.md`, M2 plan Deviation 33). The M3 scoring research
(A3 §3.6) drafted three smaller wire schemas. Each one keeps v1's internal draft shape: the validator
normalises the wire output into the v1 `GeneratorOut`. They are built here as JSON-schema dicts:

- **A** (`candidate_a`) is v1 with one change. `criteria` becomes an array of one generic `$defs.criterion`
  item, and that item gains a `criterion` key enum with the six §11.2 keys. Everything else is v1.
- **B** (`candidate_b`) is A, plus two changes:
  - Every remaining vocabulary enum becomes a plain string: `work_mode.value`, `remote_region.value`,
    `$defs.lang_code`, `$defs.cefr`, `seniority.value`, `employment_types.values[]`, `degree.level` and
    `$defs.profile_ref.section`.
  - The nullables become sentinels. `$defs.nullable_string` is a `string`, where `""` means none, and
    `$defs.nullable_int` is an `integer`, where `-1` means none.

  The `$defs` keep their names. Only `criterion`, `basis` and `level` stay constrained.
- **C** (`candidate_c`) is A3 §3.6's full JSON, verbatim: generic fact items (`code`, `value`, `qualifier`,
  `required`, `evidence`) and generic criterion items.

Each candidate is a fixed point of `anthropic.transform_schema` (`is_transform_fixed_point`). That is the
transform the gateway applies to every node schema: `gateway.output_schema`, and langchain's
`output_config.format` in `gateway._chat`. So the `output_config.format.schema` a request sends is the
candidate, byte for byte.

`counts(schema)` is A3 §3.4's structural walk on the ref-expanded schema, where every `$ref` is inlined.
`canonical_bytes` is the byte length of the schema as the request's byte bound renders it
(`fake.canonical_schema_json`), `$defs` included. Run the module to print the table:

    uv run python scripts/spikes/generator_v2_candidates.py
"""

import copy
import json
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from types import MappingProxyType
from typing import Any, Final

import anthropic
from jobhunter.llm.fake import canonical_schema_json
from jobhunter.llm.schemas import transformed
from jobhunter.llm.schemas.generator import GeneratorOut

# §11.2's criterion keys, in its order: `criteria` items carry one each (A, B, C).
CRITERION_KEYS: Final = (
    "role_fit",
    "skills",
    "experience_seniority",
    "domain",
    "languages",
    "education_certifications",
)
# B: the vocabulary enums that become plain strings, as paths into `$defs` (A3 §3.6, proposal B).
B_VOCABULARIES: Final = (
    ("job_facts", "properties", "work_mode", "properties", "value"),
    ("job_facts", "properties", "remote_region", "properties", "value"),
    ("lang_code",),
    ("cefr",),
    ("job_facts", "properties", "seniority", "properties", "value"),
    ("job_facts", "properties", "employment_types", "properties", "values", "items"),
    ("job_facts", "properties", "degree", "properties", "level"),
    ("profile_ref", "properties", "section"),
)
# B: the nullables and their sentinel types (`""` and `-1` mean none; the validator maps them to null).
B_SENTINELS: Final = MappingProxyType({"nullable_string": "string", "nullable_int": "integer"})


def v1() -> dict[str, Any]:
    """Prompt-schema version 1's generator schema as the gateway sends it (`schemas.transformed`)."""
    return copy.deepcopy(dict(transformed(GeneratorOut)))


def candidate_a() -> dict[str, Any]:
    """A: v1 with `criteria` an array of one generic criterion item that carries its `criterion` key."""
    schema = v1()
    schema["properties"]["criteria"] = {"type": "array", "items": {"$ref": "#/$defs/criterion"}}
    criterion = schema["$defs"]["criterion"]
    criterion["properties"] = {
        "criterion": {"type": "string", "enum": list(CRITERION_KEYS)},
        **criterion["properties"],
    }
    criterion["required"] = ["criterion", *criterion["required"]]
    return schema


def candidate_b() -> dict[str, Any]:
    """B: A with the vocabularies as plain strings and the nullables as sentinel types."""
    schema = candidate_a()
    defs = schema["$defs"]
    for path in B_VOCABULARIES:
        *parents, last = path
        node = defs
        for key in parents:
            node = node[key]
        if "enum" not in node[last]:
            raise ValueError(f"$defs.{'.'.join(path)} has no enum to remove")
        node[last] = {"type": "string"}
    for name, sentinel_type in B_SENTINELS.items():
        defs[name] = {"type": sentinel_type}
    return schema


# A3 §3.6, proposal C, verbatim (key order as A3 printed it; the canonical JSON sorts the keys).
_C: Final = """
{
 "type": "object",
 "additionalProperties": false,
 "required": ["not_a_job_ad", "injection_observed", "facts", "criteria", "summary_en", "summary_de"],
 "properties": {
  "not_a_job_ad": {"type": "boolean"},
  "injection_observed": {"type": "boolean"},
  "facts": {"type": "array", "items": {"$ref": "#/$defs/fact"}},
  "criteria": {"type": "array", "items": {"$ref": "#/$defs/criterion"}},
  "summary_en": {"type": "string"},
  "summary_de": {"type": "string"}
 },
 "$defs": {
  "quotes": {"type": "array", "items": {"type": "string"}},
  "fact": {
   "type": "object",
   "additionalProperties": false,
   "required": ["code", "value", "qualifier", "required", "evidence"],
   "properties": {
    "code": {
     "type": "string",
     "enum": [
      "work_mode", "onsite_only", "remote_region", "remote_country", "required_language", "must_have",
      "nice_to_have", "seniority", "min_years_experience", "industry", "employment_type", "degree",
      "degree_or_equivalent", "certification", "security_clearance", "work_permit"
     ]
    },
    "value": {"type": "string"},
    "qualifier": {"type": "string"},
    "required": {"type": "boolean"},
    "evidence": {"$ref": "#/$defs/quotes"}
   }
  },
  "criterion": {
   "type": "object",
   "additionalProperties": false,
   "required": ["criterion", "level", "basis", "ad_evidence", "profile_evidence", "rationale"],
   "properties": {
    "criterion": {
     "type": "string",
     "enum": ["role_fit", "skills", "experience_seniority", "domain", "languages", "education_certifications"]
    },
    "level": {"anyOf": [{"type": "integer", "enum": [0, 1, 2, 3, 4]}, {"type": "null"}]},
    "basis": {"type": "string", "enum": ["observed", "unknown", "unconstrained"]},
    "ad_evidence": {"$ref": "#/$defs/quotes"},
    "profile_evidence": {
     "type": "array",
     "items": {
      "type": "object",
      "additionalProperties": false,
      "required": ["section", "quote"],
      "properties": {"section": {"type": "string"}, "quote": {"type": "string"}}
     }
    },
    "rationale": {"type": "string"}
   }
  }
 }
}
"""


def candidate_c() -> dict[str, Any]:
    """C: generic fact items and generic criterion items (A3 §3.6's full JSON)."""
    schema: dict[str, Any] = json.loads(_C)
    return schema


# The probe's ladder order (A, then B, then C), and each candidate's builder; every call builds a fresh dict.
CANDIDATES: Final[Mapping[str, Callable[[], dict[str, Any]]]] = MappingProxyType(
    {"a": candidate_a, "b": candidate_b, "c": candidate_c}
)


def is_transform_fixed_point(schema: Mapping[str, Any]) -> bool:
    """Whether `anthropic.transform_schema` leaves `schema` unchanged: equal as data and as canonical JSON."""
    out = anthropic.transform_schema(copy.deepcopy(dict(schema)))
    return out == schema and canonical_schema_json(out) == canonical_schema_json(schema)


# ---- the structural walk (A3 §3.4) -------------------------------------------------------------------------


@dataclass(frozen=True)
class Counts:
    """A3 §3.4's counts of one schema. Every count but `canonical_bytes` is taken on the ref-expanded schema.

    - An *enum* is a node with an `enum`, and `enum_values` is the number of values of all of them.
    - A *union* is a node with an `anyOf` or a type array.
    - A *free string* is a `string` node without an `enum`.
    - *Optional* counts the properties an object leaves out of `required`.
    - *Depth* counts the property and array levels below the root.
    """

    canonical_bytes: int
    expanded_bytes: int
    objects: int
    properties: int
    arrays: int
    enums: int
    enum_values: int
    unions: int
    free_strings: int
    booleans: int
    numbers: int
    optional: int
    depth: int


def expanded(schema: Mapping[str, Any]) -> dict[str, Any]:
    """`schema` with every local `$ref` inlined and no `$defs`. A recursive or foreign `$ref` is a
    `ValueError`."""
    defs: Mapping[str, Any] = schema.get("$defs") or {}

    def inline(node: Any, seen: tuple[str, ...]) -> Any:
        if isinstance(node, list):
            return [inline(item, seen) for item in node]
        if not isinstance(node, Mapping):
            return node
        if "$ref" in node:
            ref = str(node["$ref"])
            name = ref.removeprefix("#/$defs/")
            if name == ref or name not in defs or name in seen:
                raise ValueError(f"a recursive or unresolvable $ref: {ref}")
            return inline(defs[name], (*seen, name))
        return {key: inline(value, seen) for key, value in node.items() if key != "$defs"}

    out: dict[str, Any] = inline(schema, ())
    return out


def counts(schema: Mapping[str, Any]) -> Counts:
    """A3 §3.4's structural counts of `schema` (see `Counts`)."""
    tally = dict.fromkeys(
        (
            "objects",
            "properties",
            "arrays",
            "enums",
            "enum_values",
            "unions",
            "free_strings",
            "booleans",
            "numbers",
            "optional",
            "depth",
        ),
        0,
    )

    def walk(node: Mapping[str, Any], depth: int) -> None:
        tally["depth"] = max(tally["depth"], depth)
        type_ = node.get("type")
        if "enum" in node:
            tally["enums"] += 1
            tally["enum_values"] += len(node["enum"])
        if "anyOf" in node or isinstance(type_, list):
            tally["unions"] += 1
            for branch in node.get("anyOf", []):
                walk(branch, depth)
            return
        if type_ == "object":
            properties: Mapping[str, Any] = node.get("properties", {})
            tally["objects"] += 1
            tally["properties"] += len(properties)
            tally["optional"] += len(set(properties) - set(node.get("required", [])))
            for child in properties.values():
                walk(child, depth + 1)
        elif type_ == "array":
            tally["arrays"] += 1
            walk(node["items"], depth + 1)
        elif type_ == "string" and "enum" not in node:
            tally["free_strings"] += 1
        elif type_ == "boolean":
            tally["booleans"] += 1
        elif type_ in ("integer", "number") and "enum" not in node:
            tally["numbers"] += 1

    inlined = expanded(schema)
    walk(inlined, 0)
    return Counts(
        canonical_bytes=len(canonical_schema_json(schema).encode("utf-8")),
        expanded_bytes=len(canonical_schema_json(inlined).encode("utf-8")),
        **tally,
    )


def main() -> int:
    rows = {"v1": v1(), **{name: build() for name, build in CANDIDATES.items()}}
    fields = list(asdict(counts(rows["v1"])))
    print("schema " + " ".join(fields) + " fixed_point")
    for name, schema in rows.items():
        values = asdict(counts(schema))
        print(f"{name} " + " ".join(str(values[f]) for f in fields) + f" {is_transform_fixed_point(schema)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
