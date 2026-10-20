import pytest
import sqlalchemy as sa
from jobhunter.normalize.text import fold, norm_text

pytestmark = pytest.mark.integration
CORPUS = [
    "Müller Straße Café",
    "Zürich Köln Mönchengladbach",
    "Groß-Umstadt",
    "Łódź Đakovo Þór",
    "Ærø Œuvre ørsted",
    "İstanbul",
    "Softwareentwickler\N{SOFT HYPHEN} (m/w/d) Berlin",
]

# The Unicode blocks over which `fold` is pinned to the database function, code point by code point:
# the Latin script (Basic Latin, Latin-1 Supplement, Latin Extended-A and -B, Latin Extended Additional), the
# standard combining diacritics, Greek and Coptic, and General Punctuation. Outside them PostgreSQL's rules
# file transliterates symbols and exotic letters that `text.unaccent` leaves alone (for example IPA letters,
# arrows and CJK punctuation), and it keeps accents that NFKD strips (for example Cyrillic й).
PINNED_BLOCKS = [
    (0x0020, 0x007F),
    (0x00A0, 0x0100),
    (0x0100, 0x0180),
    (0x0180, 0x0250),
    (0x0300, 0x0363),
    (0x0370, 0x0400),
    (0x1E00, 0x1F00),
    (0x2000, 0x2070),
]


@pytest.mark.parametrize("s", CORPUS)
async def test_python_fold_equals_db_unaccent_casefolded(engine, s) -> None:
    async with engine.connect() as c:
        db = (
            await c.execute(sa.text("SELECT lower(immutable_unaccent(:s))"), {"s": norm_text(s)})
        ).scalar_one()
    assert fold(s) == db.casefold()


async def test_python_fold_equals_db_unaccent_over_the_pinned_blocks(engine) -> None:
    chars = [chr(cp) for lo, hi in PINNED_BLOCKS for cp in range(lo, hi)]
    query = sa.text(
        "SELECT lower(immutable_unaccent(t.s)) "
        "FROM unnest(CAST(:chars AS text[])) WITH ORDINALITY AS t(s, n) ORDER BY t.n"
    )
    async with engine.connect() as c:
        in_db = (await c.execute(query, {"chars": [norm_text(ch) for ch in chars]})).scalars().all()
    assert len(in_db) == len(chars)
    mismatches = [
        (f"U+{ord(ch):04X}", ch, fold(ch), db.casefold())
        for ch, db in zip(chars, in_db, strict=True)
        if fold(ch) != db.casefold()
    ]
    assert mismatches == []
