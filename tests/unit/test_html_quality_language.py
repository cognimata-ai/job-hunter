import re
import time
from importlib import resources

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from jobhunter.normalize.html import ALLOWED_TAGS, html_to_text, sanitize_html, unescape_html
from jobhunter.normalize.language import detect_language
from jobhunter.normalize.quality import description_quality
from jobhunter.normalize.text import norm_text

REL = 'rel="noopener noreferrer nofollow"'
LINK = f'target="_blank" {REL}'


def test_sanitiser_allowlist() -> None:
    out = sanitize_html(
        '<p onclick="x">Hi <script>evil()</script><a href="javascript:1">j</a><a href="https://e.x">ok</a></p><h1>t</h1>'
    )
    assert "<script" not in out
    assert "onclick" not in out
    assert "<h1" not in out
    assert 'href="https://e.x"' in out
    assert REL in out
    assert 'target="_blank"' in out
    assert "javascript" not in out
    assert html_to_text("<p>a&amp;b</p><ul><li>x</li></ul>") == "a&b\nx"


def test_the_allowed_tags_are_the_spec_list() -> None:
    assert ALLOWED_TAGS == frozenset({"p", "br", "ul", "ol", "li", "strong", "em", "b", "i", "h3", "h4", "a"})
    html = "".join(f"<{tag}>x</{tag}>" for tag in sorted(ALLOWED_TAGS - {"br", "a"}))
    assert sanitize_html(html) == html
    assert sanitize_html("a<br>b") == "a<br>b"


def test_the_exact_output_for_a_link() -> None:
    out = sanitize_html('<a href="https://e.x/p?a=1&b=2" class="c" style="x:y" id="i" onclick="z">ok</a>')
    assert out == f'<a href="https://e.x/p?a=1&amp;b=2" {LINK}>ok</a>'


@pytest.mark.parametrize(
    "href",
    [
        "javascript:alert(1)",
        "JaVaScRiPt:alert(1)",
        " javascript:alert(1)",
        "java\tscript:alert(1)",
        "data:text/html;base64,PHNjcmlwdD4=",
        "vbscript:x",
        "mailto:a@b.c",
        "tel:+491234",
        "ftp://e.x/f",
        "/relative/path",
        "relative/path",
        "//evil.example/x",
        "#fragment",
        "",
    ],
)
def test_only_http_and_https_links_survive(href) -> None:
    out = sanitize_html(f'<a href="{href}">x</a>')
    assert "href" not in out
    assert out == f"<a {LINK}>x</a>"


@pytest.mark.parametrize("href", ["http://e.x/", "HTTPS://E.X/Path", "https://e.x/a?b=c#d"])
def test_http_and_https_links_survive(href) -> None:
    out = sanitize_html(f'<a href="{href}">x</a>')
    assert f'href="{href}"' in out.replace("&amp;", "&")
    assert REL in out and 'target="_blank"' in out


def test_rel_and_target_are_forced_over_what_the_ad_says() -> None:
    out = sanitize_html(
        '<a href="https://e.x" rel="me" target="_self">x</a><a href="https://e.y" target="_top">y</a>'
    )
    assert out.count(REL) == 2
    assert out.count('target="_blank"') == 2
    assert 'rel="me"' not in out and "_self" not in out and "_top" not in out


def test_dangerous_content_is_removed_with_its_content_or_stripped() -> None:
    out = sanitize_html(
        "<style>p{color:red}</style><script>alert(1)</script><!-- note --><iframe src=https://e.x></iframe>"
        '<img src=x onerror=alert(1)><object data=x></object><p style="color:red" class="c" id="i">kept</p>'
    )
    assert out == "<p>kept</p>"


def test_tags_outside_the_allowlist_lose_the_tag_but_keep_the_text() -> None:
    assert sanitize_html("<span>one</span> <font color=red>two</font> <u>three</u>") == "one two three"
    assert sanitize_html("<span>a</span><span>b</span>") == "ab"  # inline tags do not separate words


def test_stripped_block_tags_still_separate_words() -> None:
    # `<h2>Aufgaben</h2><div>Entwicklung</div>` must not become "AufgabenEntwicklung".
    assert sanitize_html("<h2>Aufgaben</h2><div>Entwicklung</div>") == "<br>Aufgaben<br>Entwicklung<br>"
    assert sanitize_html(
        "<div><span>one</span> <h1>two</h1><table><tr><td>three</td></tr></table></div>"
    ) == ("<br>one <br>two<br>three<br>")
    assert html_to_text(sanitize_html("<h2>Aufgaben</h2><div>Entwicklung</div><DIV class=x>Test</DIV>")) == (
        "Aufgaben\nEntwicklung\nTest"
    )
    assert (
        html_to_text(sanitize_html("<table><tr><td>a</td><td>b</td></tr><tr><td>c</td></tr></table>"))
        == "a\nb\nc"
    )
    assert html_to_text(sanitize_html("line<hr>line")) == "line\nline"


def test_a_comment_an_attribute_value_or_a_link_cannot_fake_a_block_tag() -> None:
    # fix wave F1 (ledger L40): the block tags are found in nh3's output, where every `<` starts a tag
    assert sanitize_html("<p>a</p><!-- <div --> <p>b</p> c") == "<p>a</p> <p>b</p> c"  # nothing swallowed
    assert html_to_text(sanitize_html("<p>a</p><!-- x <div class=y> z --><p>b</p>")) == "a\nb"
    assert sanitize_html('<div title="a>b">text</div>') == "<br>text<br>"  # no `b">` left behind
    assert (
        sanitize_html('<a href="https://e.x/?q=<td>">x</a>')
        == f'<a href="https://e.x/?q=&lt;td&gt;" {LINK}>x</a>'
    )
    assert (
        sanitize_html('<a href="https://e.x/</div>">x</a>')
        == f'<a href="https://e.x/&lt;/div&gt;" {LINK}>x</a>'
    )


def test_unterminated_tags_do_not_make_the_sanitiser_quadratic() -> None:
    started = time.perf_counter()
    out = sanitize_html("<div " * 200_000)
    sanitize_html("<td" + " x" * 500_000)
    assert time.perf_counter() - started < 5
    assert "<div" not in out


def test_unclosed_and_nested_markup_is_repaired() -> None:
    assert sanitize_html("<p>one<p>two<ul><li>a<li>b") == "<p>one</p><p>two</p><ul><li>a</li><li>b</li></ul>"
    assert sanitize_html("<b><i>x</b></i>") == "<b><i>x</i></b>"


def test_text_is_escaped_and_not_double_escaped() -> None:
    assert sanitize_html("1 < 2 & 3 > 2") == "1 &lt; 2 &amp; 3 &gt; 2"
    assert sanitize_html("a &amp; b") == "a &amp; b"
    assert sanitize_html("&lt;script&gt;alert(1)&lt;/script&gt;") == "&lt;script&gt;alert(1)&lt;/script&gt;"
    assert sanitize_html("") == ""


# Ledger L40: plain `st.text()` almost never draws markup, so the properties draw documents from tag
# fragments: allowed and stripped tags, comments, scripts, links with and without an allowed scheme, `>`
# inside an attribute value and tag text inside a link, mixed with words, entities and stray brackets.
_FRAGMENTS = st.sampled_from(
    [
        "<p>",
        "</p>",
        "<div>",
        "</div>",
        '<div class="c" title="a>b">',
        "<h2>",
        "</h2>",
        "<table><tr><td>",
        "</td></tr></table>",
        "<hr>",
        "<br>",
        "<ul><li>",
        "</li></ul>",
        "<strong>",
        "</strong>",
        "<span>",
        "</span>",
        "<!--",
        "-->",
        "<!-- <div -->",
        "<script>alert(1)",
        "</script>",
        "<style>p{}",
        "</style>",
        '<a href="https://e.x/?q=<td>">',
        '<a href="javascript:alert(1)">',
        '<a href="/relative">',
        "</a>",
        '<img src=x onerror="alert(1)">',
        "&amp;",
        "&lt;",
        "&nbsp;",
        "<",
        ">",
        "&",
        " ",
        "\n",
        "Wort",
        "word",
    ]
)
HTML_DOCUMENTS = st.lists(_FRAGMENTS | st.text(max_size=4), max_size=40).map("".join)
_TAG_NAME = re.compile(r"<(/?)([A-Za-z][A-Za-z0-9]*)")
_HREF = re.compile(r'href="([^"]*)"')


@given(st.text(max_size=300) | HTML_DOCUMENTS)
@settings(max_examples=300, deadline=None)
def test_property_sanitising_is_idempotent_and_never_leaves_a_script(raw: str) -> None:
    once = sanitize_html(raw)
    assert sanitize_html(once) == once
    assert "<script" not in once.casefold() and "<!--" not in once
    assert {m.group(2) for m in _TAG_NAME.finditer(once)} <= ALLOWED_TAGS
    assert all(href.startswith(("http://", "https://")) for href in _HREF.findall(once))


@pytest.mark.parametrize(
    ("html", "text"),
    [
        ("<p>a&amp;b</p><ul><li>x</li></ul>", "a&b\nx"),
        ("<p>one</p><p>two</p>", "one\ntwo"),
        ("one<br>two<br/>three", "one\ntwo\nthree"),
        ("<ul><li>a</li><li>b</li></ul><ol><li>c</li></ol>", "a\nb\nc"),
        ("<h3>Head</h3><p>Body</p><h4>Sub</h4>text", "Head\nBody\nSub\ntext"),
        ("<p>Hello <strong>wor</strong>ld <em>and</em> <b>more</b> <i>x</i></p>", "Hello world and more x"),
        ('<p>see <a href="https://e.x/?a>b" rel="x">the link</a>.</p>', "see the link."),
        ("<p>  spaced \n\t out\N{NO-BREAK SPACE}text\N{ZERO WIDTH SPACE} </p>", "spaced out text"),
        ("<p>a</p>\n\n<p></p><p>   </p><br><br><p>b</p>", "a\nb"),
        (
            "&lt;script&gt;x&lt;/script&gt; &amp;amp; &quot;q&quot; &#252;&nbsp;&euro;",
            '<script>x</script> &amp; "q" ü €',
        ),
        ("plain text", "plain text"),
        ("", ""),
    ],
)
def test_html_to_text(html, text) -> None:
    assert html_to_text(html) == text


@given(st.text(max_size=300) | HTML_DOCUMENTS)
@settings(max_examples=300, deadline=None)
def test_property_text_of_sanitised_html_is_normalised_lines(raw: str) -> None:
    text = html_to_text(sanitize_html(raw))
    assert all(line and line == norm_text(line) for line in text.split("\n")) or text == ""


def test_unescape_html() -> None:
    assert unescape_html("&lt;p&gt;Hi &amp;amp; bye&lt;/p&gt;") == "<p>Hi &amp; bye</p>"
    assert unescape_html("&#60;b&#62;x&#x3C;/b&#x3E; &uuml;") == "<b>x</b> ü"
    assert unescape_html("no entities") == "no entities"
    assert (
        html_to_text(
            sanitize_html(unescape_html("&lt;p&gt;Hi&lt;/p&gt;&lt;ul&gt;&lt;li&gt;x&lt;/li&gt;&lt;/ul&gt;"))
        )
        == "Hi\nx"
    )


def test_quality_gate() -> None:
    assert description_quality("x" * 56) == "garbage"
    assert description_quality("Wir suchen " * 15) == "short"
    assert description_quality("ok text here\nnull\nnull\nnull\n" + "word " * 60) == "garbage"
    assert description_quality("#" * 300) == "garbage"
    assert description_quality("Wir suchen eine erfahrene Entwicklerin. " * 10) == "ok"


@pytest.mark.parametrize(
    ("text", "quality"),
    [
        ("", "garbage"),
        ("a" * 59, "garbage"),
        ("a" * 60, "short"),
        ("a" * 199, "short"),
        ("a" * 200, "ok"),
        ("  " + "a" * 58 + "  ", "garbage"),  # the length is the trimmed text's
        ("a" * 120 + "1" * 80, "ok"),  # exactly 40% non-letters: not more than 40%
        ("a" * 119 + "1" * 81, "garbage"),
        ("a" * 400 + "\n" + "1" * 270, "garbage"),
        ("Ünïcödé ßtraße " * 20, "ok"),  # accented letters are letters
        ("日本語のテキスト" * 30, "ok"),
    ],
)
def test_quality_boundaries(text, quality) -> None:
    assert description_quality(text) == quality


@pytest.mark.parametrize(
    ("lines", "quality"),
    [
        (["null", "null"], "ok"),
        (["null", "null", "null"], "garbage"),
        (["NULL", "Undefined", " null "], "garbage"),
        (["undefined", "null", "undefined"], "garbage"),
        (["null", "", "null", "  ", "null"], "garbage"),
        (["nullable", "null", "undefined"], "ok"),  # only whole lines count
        (["null null null"], "ok"),
    ],
)
def test_null_lines(lines, quality) -> None:
    body = "Wir suchen eine erfahrene Entwicklerin mit viel Erfahrung. " * 6
    assert description_quality("\n".join([body, *lines])) == quality


def test_quality_counts_the_normalised_text() -> None:
    padded = "\n\n   \n".join(["Wir suchen eine erfahrene Entwicklerin."] * 3)
    assert description_quality(padded) == "short"  # 3 x 38 characters, not the indentation
    assert description_quality(padded + "\n" * 500 + " " * 500) == "short"


DE = (
    "Wir suchen für unser Team in Berlin eine erfahrene Entwicklerin, die mit uns die Zukunft gestaltet"
    " und nicht nur auf dem Papier gut ist. "
)
EN = (
    "We are looking for an experienced engineer who will join our team in Berlin and help us build the"
    " future of our product with the team. "
)


def test_language_detection() -> None:
    de = DE * 3
    en = EN * 3
    assert (detect_language(de), detect_language(en), detect_language("kurz"), detect_language(de + en)) == (
        "de",
        "en",
        "unknown",
        "unknown",
    )


def test_language_needs_50_tokens() -> None:
    words = DE.split() * 3
    assert detect_language(" ".join(words[:49])) == "unknown"
    assert detect_language(" ".join(words[:50])) == "de"
    assert detect_language(" ".join(EN.split() * 3)[:1000]) == "en"
    assert detect_language("") == "unknown"
    assert detect_language("123 456 789 " * 40) == "unknown"  # digits are not tokens


def test_language_threshold_is_a_difference_of_0_2() -> None:
    filler = ["Zukunft"] * 100
    # 100 filler words, 25 German stop words: de_ratio = 25/125 = 0.2, en_ratio = 0.
    assert detect_language(" ".join(filler + ["und"] * 25)) == "de"
    # 24 stop words: 24/124 < 0.2.
    assert detect_language(" ".join(filler + ["und"] * 24)) == "unknown"
    assert detect_language(" ".join(["engineer"] * 100 + ["the"] * 25)) == "en"
    assert detect_language(" ".join(["engineer"] * 100 + ["the"] * 24)) == "unknown"


def test_language_folds_umlauts_and_case() -> None:
    # `für`, `über`, `während` are stop words; the tokens and the lists are both folded.
    assert detect_language("FÜR ÜBER WÄHREND Ihre Aufgaben " * 20) == "de"
    assert detect_language("fur uber wahrend Ihre Aufgaben " * 20) == "de"


def test_the_stop_word_files_are_the_postgresql_lists() -> None:
    data = resources.files("jobhunter.normalize") / "data"
    de = (data / "stopwords_de.txt").read_text(encoding="utf-8").split("\n")
    en = (data / "stopwords_en.txt").read_text(encoding="utf-8").split("\n")
    assert de[-1] == "" and en[-1] == ""
    de, en = de[:-1], en[:-1]
    assert (len(de), de[0], de[-1]) == (231, "aber", "zwischen")  # the full german.stop (ruling T3-a)
    assert (len(en), en[0], en[-1]) == (127, "i", "now")
    assert len(set(de)) == 231 and len(set(en)) == 127
    assert {"und", "der", "die", "für", "nicht"} <= set(de)
    assert {"wir", "zu", "wird", "werden", "wie", "wenn"} <= set(
        de
    )  # frequent words the alphabetical cut dropped
    assert {"the", "and", "of", "to", "is"} <= set(en)
    assert all(w == w.strip() and w for w in de + en)
