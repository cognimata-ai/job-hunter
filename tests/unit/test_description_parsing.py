from jobhunter.normalize.quality import description_quality
from jobhunter.sources.parsing.description import plain_description


def test_a_plain_text_description_is_stored_one_normalised_line_per_non_empty_line() -> None:
    raw = "  First   line  \n\n\u00a0Second\u200b line \r\n\t\r\nThird\u2028fourth\n"
    text, _ = plain_description(raw)
    assert text == "First line\nSecond line\nThird\nfourth"


def test_the_quality_is_judged_on_the_stored_text() -> None:
    padding = "\n\n   \n" * 100  # blank lines and indentation are not characters
    body = "A reasonably long sentence about the role and the team, written in plain words. " * 3
    text, quality = plain_description(f"{padding}{body}{padding}")
    assert quality == description_quality(text) == "ok" and len(text) >= 200
    assert plain_description(body[:100])[1] == "short"
    assert plain_description("x" * 10)[1] == "garbage"
    assert plain_description("null\nundefined\nnull\n" + body)[1] == "garbage"
    assert plain_description("1234567890 " * 30)[1] == "garbage"  # more than 40% non-letters


def test_a_value_that_is_not_text_is_an_empty_garbage_description() -> None:
    for raw in (None, 17, ["text"], {"a": "b"}, True):
        assert plain_description(raw) == ("", "garbage")
