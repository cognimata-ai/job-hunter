"""Company keys (spec §8.3): the folded name without punctuation, legal form and a trailing country word."""

import re
import unicodedata
from typing import Final

from jobhunter.normalize.text import fold

# The §8.3 list, verbatim and in the order of the specification.
_SPEC_LEGAL_FORMS: Final = (
    "gmbh & co. kgaa",
    "gmbh & co. kg",
    "gmbh & co kg",
    "ag & co. kg",
    "se & co. kg",
    "ggmbh",
    "gmbh",
    "mbh",
    "ag",
    "se",
    "kgaa",
    "kg",
    "ohg",
    "gbr",
    "ug (haftungsbeschränkt)",
    "ug",
    "e.v.",
    "ev",
    "e.k.",
    "plc",
    "ltd",
    "limited",
    "inc",
    "llc",
    "b.v.",
    "bv",
    "n.v.",
    "nv",
    "s.a.",
    "sa",
    "sarl",
    "s.r.l.",
    "srl",
    "spa",
    "ab",
    "oy",
    "corp",
    "corporation",
)
# Longest first; `sorted` is stable, so forms of the same length keep the order of the specification.
LEGAL_FORMS: Final[tuple[str, ...]] = tuple(sorted(_SPEC_LEGAL_FORMS, key=len, reverse=True))
_TRAILING_COUNTRIES: Final = frozenset({"deutschland", "germany"})


def _strip_punctuation(s: str) -> str:
    """Remove punctuation and symbols except `&` (`Co.` becomes `Co`, `e.V.` becomes `eV`)."""
    return "".join(c for c in s if c == "&" or unicodedata.category(c)[0] not in "PS")


def _plain(s: str) -> str:
    """Step 1 on a string that is already folded: no punctuation but `&`, single spaces."""
    return " ".join(_strip_punctuation(s).split())


# The text is folded and has lost its punctuation before the forms are looked for, so the forms are put in
# that shape too: `ug (haftungsbeschränkt)` is looked for as `ug haftungsbeschrankt`, `e.v.` as `ev`.
_PLAIN_LEGAL_FORMS: Final = sorted(
    dict.fromkeys(_plain(fold(form)) for form in LEGAL_FORMS), key=len, reverse=True
)
_LEGAL_FORM: Final = re.compile(r"(?<!\S)(?:" + "|".join(map(re.escape, _PLAIN_LEGAL_FORMS)) + r")(?!\S)")


def company_key(raw: str) -> str:
    """§8.3 steps 1-4: `Zalando SE` and `Vitaservices GmbH & Co. KG` become `zalando` and `vitaservices`."""
    folded = fold(raw)
    text = " ".join(_LEGAL_FORM.sub(" ", _plain(folded)).split())
    tokens = text.split()
    if len(tokens) > 1 and tokens[-1] in _TRAILING_COUNTRIES:
        tokens.pop()
    return " ".join(tokens) or folded
