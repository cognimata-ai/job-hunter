"""Ad HTML (spec §8.9, §16.7): the nh3 allow-list, the plain text of sanitised HTML, entity unescaping.

The UI renders only what `sanitize_html` returns. `html_to_text` is defined on sanitised HTML only: nh3
serialises text with `<` and `&` escaped, so every `<` it meets starts a tag.
"""

import re
from html import unescape
from html.parser import HTMLParser
from typing import Final

import nh3

from jobhunter.normalize.text import norm_text

ALLOWED_TAGS: Final = frozenset({"p", "br", "ul", "ol", "li", "strong", "em", "b", "i", "h3", "h4", "a"})

# The tags whose boundaries end a line in the plain text. `br` is a break of its own, not a block.
_BLOCK_TAGS: Final = frozenset({"p", "ul", "ol", "li", "h3", "h4"})
# Block-level tags that the allow-list strips. Stripped without a trace they would glue the words on either
# side (`<h2>Aufgaben</h2><div>Entwicklung</div>` would read "AufgabenEntwicklung"), so a run of them becomes
# one `<br>`. The run is found in nh3's own output, not in the raw HTML (fix wave F1): a first nh3 pass keeps
# these tags as well, drops comments, scripts and styles, and escapes every `<` and `>` of text and attribute
# values, so in its output every `<` starts a tag. A comment holding `<div`, a `>` inside an attribute value
# and tag text inside a link can then no longer be taken for a block tag. The second pass applies the
# allow-list and stays the only judge of what survives.
_STRIPPED_BLOCK_TAGS: Final = frozenset(
    {"address", "article", "aside", "blockquote", "caption", "dd", "details", "div", "dl", "dt", "fieldset"}
    | {"figcaption", "figure", "footer", "form", "h1", "h2", "h5", "h6", "header", "hr", "main", "nav", "pre"}
    | {"section", "summary", "table", "tbody", "td", "tfoot", "th", "thead", "tr"}
)
_STRIPPED_BLOCKS: Final = re.compile(
    r"(?:</?(?:" + "|".join(sorted(_STRIPPED_BLOCK_TAGS)) + r")\b[^>]*>\s*)+", re.IGNORECASE
)
_LINK_REL: Final = "noopener noreferrer nofollow"
_URL_SCHEMES: Final = frozenset({"http", "https"})


def _clean(html: str, tags: frozenset[str]) -> str:
    return nh3.clean(
        html,
        tags=set(tags),
        clean_content_tags={"script", "style"},
        attributes={"a": {"href"}},
        url_schemes=set(_URL_SCHEMES),
        # nh3 passes relative and protocol-relative URLs by default; §8.9 allows http and https only.
        url_relative="deny",
        link_rel=_LINK_REL,
        set_tag_attribute_values={"a": {"target": "_blank"}},
        strip_comments=True,
    )


def sanitize_html(raw: str) -> str:
    """Strip all but `ALLOWED_TAGS`; keep an http(s) `href` on `a`; force `rel` and `target` on every link."""
    parsed = _clean(raw, ALLOWED_TAGS | _STRIPPED_BLOCK_TAGS)
    return _clean(_STRIPPED_BLOCKS.sub("<br>", parsed), ALLOWED_TAGS)


class _TextExtractor(HTMLParser):
    """Collects the text of a document; block tags and `br` become line breaks."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "br" or tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        # A newline inside a text node is whitespace in HTML, not a line break.
        self.parts.append(data.replace("\n", " "))


def html_to_text(sanitized: str) -> str:
    """Plain text of sanitised HTML: one `norm_text`-normalised line per block, no empty lines."""
    extractor = _TextExtractor()
    extractor.feed(sanitized)
    extractor.close()
    lines = (norm_text(line) for line in "".join(extractor.parts).split("\n"))
    return "\n".join(line for line in lines if line)


def unescape_html(s: str) -> str:
    """One level of HTML entity unescaping (Greenhouse delivers `content` as escaped HTML)."""
    return unescape(s)
