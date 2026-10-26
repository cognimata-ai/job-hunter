"""Rubric v2 (M3 Task 3, R13): rubric v1 with exactly one anchor text changed, the last sentence of the
`languages` paragraph of §11.3, so that an `unspecified` mandatory level reads as the §11.4 knockout table
does (B2 or higher `met`, below B2 `uncomparable`). The weights are v1's; the content hash differs."""

# ruff: noqa: E501  (the anchor is the spec's sentence as it is)

from importlib import resources

from jobhunter.scoring.rubric import RUBRIC_SHIPPED, RUBRIC_V1, RUBRIC_V2, load_rubric, parse_rubric
from jobhunter.types import JSONValue

V2_SENTENCE = (
    "A mandatory language with an `unspecified` level is *provably unmet* only when the language is absent "
    "from the profile; when the profile has it at B2 or higher, the knockout comparison is `met`; below B2 it "
    "is `uncomparable` (§11.4)."
)


def strings(value: JSONValue, path: str = "") -> dict[str, str]:
    """Every string of an anchors document by its path."""
    if isinstance(value, str):
        return {path: value}
    if isinstance(value, dict):
        return {p: s for key, item in value.items() for p, s in strings(item, f"{path}.{key}").items()}
    if isinstance(value, list):
        return {
            p: s for index, item in enumerate(value) for p, s in strings(item, f"{path}[{index}]").items()
        }
    return {}


def test_v2_is_shipped_and_the_shipped_rubric() -> None:
    assert (RUBRIC_V1, RUBRIC_V2, RUBRIC_SHIPPED) == ("v1", "v2", "v2")
    text = resources.files("jobhunter.scoring.rubric").joinpath("v2.yaml").read_text("utf-8")
    assert parse_rubric(text, name="v2") == load_rubric("v2")


def test_v2_keeps_the_weights_of_v1() -> None:
    v1, v2 = load_rubric("v1"), load_rubric("v2")
    assert dict(v2.weights) == dict(v1.weights)
    assert tuple(v2.weights) == tuple(v1.weights)  # the §11.2 order
    assert list(v2.weights.values()) == [20, 25, 15, 10, 10, 5, 10, 5]


def test_only_the_unspecified_level_sentence_of_languages_changes() -> None:
    v1, v2 = strings(dict(load_rubric("v1").anchors)), strings(dict(load_rubric("v2").anchors))
    assert v1.keys() == v2.keys()  # no anchor added or removed
    changed = {path for path in v1 if v1[path] != v2[path]}
    assert changed == {".languages.unspecified_level"}
    assert v2[".languages.unspecified_level"] == V2_SENTENCE
    assert "B2 or higher, the knockout comparison is `met`" in V2_SENTENCE
    assert "at any level" in v1[".languages.unspecified_level"]  # v1 is unchanged


def test_the_content_hash_differs_from_v1() -> None:
    v1, v2 = load_rubric("v1"), load_rubric("v2")
    assert v2.content_hash != v1.content_hash
    assert len(v2.content_hash) == 32
