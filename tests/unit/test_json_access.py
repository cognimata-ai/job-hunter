from collections.abc import Callable

import pytest
from jobhunter.types import JSONValue, get_bool, get_int, get_list, get_object, get_str, json_get

DOC: JSONValue = {"a": {"flag": True, "n": 3, "s": "x", "l": [1, "y"], "o": {}}}


def test_accessors_narrow() -> None:
    assert get_bool(DOC, "a.flag") is True and get_int(DOC, "a.n") == 3 and get_str(DOC, "a.s") == "x"
    assert get_list(DOC, "a.l") == [1, "y"] and get_object(DOC, "a.o") == {} and json_get(DOC, "a.n") == 3


@pytest.mark.parametrize(
    ("call", "error", "text"),
    [
        (lambda: get_int(DOC, "a.flag"), TypeError, "a.flag: expected int, got bool"),
        (lambda: get_object(DOC, "a.s.x"), TypeError, "a.s: expected object, got str"),
        (lambda: get_str(DOC, "a.missing"), KeyError, "a.missing"),
        (lambda: get_str(DOC, "a.missing.deeper"), KeyError, "a.missing.deeper"),
        (lambda: get_bool(DOC, "a.n"), TypeError, "a.n: expected bool, got int"),
        (lambda: get_str(DOC, "a.n"), TypeError, "a.n: expected str, got int"),
        (lambda: get_list(DOC, "a.o"), TypeError, "a.o: expected list, got dict"),
        (lambda: get_object(DOC, "a.l"), TypeError, "a.l: expected object, got list"),
        (lambda: get_int(DOC, "a.s"), TypeError, "a.s: expected int, got str"),
        (lambda: get_object([1], ""), TypeError, "<root>: expected object, got list"),
    ],
)
def test_accessors_reject(call: Callable[[], object], error: type[Exception], text: str) -> None:
    with pytest.raises(error, match=text):
        call()


def test_an_empty_path_is_the_document_itself() -> None:
    assert get_object(DOC, "") == DOC and json_get(DOC, "") == DOC
