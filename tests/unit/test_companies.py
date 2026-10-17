import pytest
from jobhunter.normalize.companies import LEGAL_FORMS, company_key


def test_legal_forms_and_fallback() -> None:
    assert (
        company_key("Zalando SE") == "zalando" and company_key("Vitaservices GmbH & Co. KG") == "vitaservices"
    )
    assert (
        company_key("Delivery Hero SE") == "delivery hero"
        and company_key("Müller Deutschland GmbH") == "muller"
    )
    assert company_key("Deutschland GmbH") == "deutschland" and company_key("GmbH") == "gmbh"
    assert company_key("UG (haftungsbeschränkt) Acme") == "acme" and company_key("Acme Inc.") == "acme"


def test_legal_form_list_is_the_spec_list_longest_first() -> None:
    assert set(LEGAL_FORMS) == {
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
    }
    assert len(LEGAL_FORMS) == len(set(LEGAL_FORMS)) == 38
    lengths = [len(form) for form in LEGAL_FORMS]
    assert lengths == sorted(lengths, reverse=True)


@pytest.mark.parametrize(
    ("raw", "key"),
    [
        # Each legal form of the list, in the writing the sources use.
        ("Acme GmbH & Co. KGaA", "acme"),
        ("Acme GmbH & Co. KG", "acme"),
        ("Acme GmbH & Co KG", "acme"),
        ("Acme AG & Co. KG", "acme"),
        ("Acme SE & Co. KG", "acme"),
        ("Acme gGmbH", "acme"),
        ("Acme GmbH", "acme"),
        ("Acme mbH", "acme"),
        ("Acme AG", "acme"),
        ("Acme SE", "acme"),
        ("Acme KGaA", "acme"),
        ("Acme KG", "acme"),
        ("Acme OHG", "acme"),
        ("Acme GbR", "acme"),
        ("Acme UG (haftungsbeschränkt)", "acme"),
        ("Acme UG", "acme"),
        ("Acme e.V.", "acme"),
        ("Acme eV", "acme"),
        ("Acme e.K.", "acme"),
        ("Acme plc", "acme"),
        ("Acme Ltd", "acme"),
        ("Acme Ltd.", "acme"),
        ("Acme Limited", "acme"),
        ("Acme Inc", "acme"),
        ("Acme LLC", "acme"),
        ("Acme B.V.", "acme"),
        ("Acme BV", "acme"),
        ("Acme N.V.", "acme"),
        ("Acme NV", "acme"),
        ("Acme S.A.", "acme"),
        ("Acme SA", "acme"),
        ("Acme SARL", "acme"),
        ("Acme S.r.l.", "acme"),
        ("Acme SRL", "acme"),
        ("Acme SpA", "acme"),
        ("Acme AB", "acme"),
        ("Acme Oy", "acme"),
        ("Acme Corp.", "acme"),
        ("Acme Corporation", "acme"),
        # Whole words anywhere, not only at the end; several forms in a row all go.
        ("Acme Inc Corp", "acme"),
        ("Acme AG Holding", "acme holding"),
        ("AG Acme", "acme"),
        ("Acme GmbH Berlin", "acme berlin"),
        # Not whole words: a legal form inside a longer word stays.
        ("Sagan Agency", "sagan agency"),
        ("Seven Kgx", "seven kgx"),
        ("Absa Group", "absa group"),
        # Punctuation goes (except &), case and accents are folded.
        ("Müller & Söhne GmbH", "muller & sohne"),
        ("McKinsey & Company, Inc.", "mckinsey & company"),
        ("Dr. Oetker", "dr oetker"),
        ("  Zalando   SE  ", "zalando"),
        ("ZALANDO SE", "zalando"),
        # A trailing country word goes only when a token remains.
        ("SAP Deutschland SE & Co. KG", "sap"),
        ("Acme Germany GmbH", "acme"),
        ("Acme Germany", "acme"),
        ("Germany GmbH", "germany"),
        ("Deutschland", "deutschland"),
        ("Deutsche Bahn", "deutsche bahn"),
        ("Deutschland Acme", "deutschland acme"),  # only a trailing one
        # Fallback to fold(raw) when nothing remains.
        ("SE", "se"),
        ("G.m.b.H.", "g.m.b.h."),
        ("AG & Co. KG", "ag & co. kg"),
        ("", ""),
        ("  ", ""),
    ],
)
def test_company_key_cases(raw, key) -> None:
    assert company_key(raw) == key


def test_company_key_is_stable_across_spellings() -> None:
    forms = [
        "Vitaservices GmbH & Co. KG",
        "VITASERVICES GMBH & CO KG",
        "vitaservices gmbh & co. kg",
        "Vitaservices",
    ]
    assert {company_key(f) for f in forms} == {"vitaservices"}
