"""Plain-text ad descriptions (spec §8.9): JSearch and SerpApi deliver text, not HTML.

`plain_description` stores such a text the way the quality gate measures it, one `norm_text`-normalised line
per non-empty line (the same shape `html_to_text` gives sanitised HTML), and grades that stored text. A value
that is not a string is an empty description: the gate calls it `garbage`, which is how a source that stops
sending its description shows up, never as a failed item.
"""

from jobhunter.normalize.quality import Quality, description_quality
from jobhunter.normalize.text import norm_text


def plain_description(raw: object) -> tuple[str, Quality]:
    """The stored text of a plain-text description and its quality."""
    text = raw if isinstance(raw, str) else ""
    stored = "\n".join(line for line in map(norm_text, text.splitlines()) if line)
    return stored, description_quality(stored)
