import re
from typing import get_args

import pytest
from jobhunter.normalize.titles import (
    GENDER_TAG,
    SENIORITY_LADDER,
    NormalizedTitle,
    SeniorityToken,
    normalize_title,
    seniority_tokens,
    strip_gender_tags,
)


@pytest.mark.parametrize(
    "raw",
    [
        "Entwickler (m/w/d)",
        "Entwickler (w/m/d)",
        "Entwickler (f/m/x)",
        "Entwickler (w_m_d)",
        "Entwickler (m|w|d)",
        "Entwickler (*)",
        "Entwickler (all genders)",
        "Entwickler (gn)",
        "Entwickler m/w/d",
        "Entwickler*in",
        "Entwickler:innen",
        "Entwickler_in",
        "Entwickler/-in",
        "EntwicklerIn",
    ],
)
def test_gender_tags_are_stripped(raw) -> None:
    assert strip_gender_tags(raw).strip() == "Entwickler"


@pytest.mark.parametrize(
    "raw",
    [
        "Entwickler (M/W/D)",
        "Entwickler [m/w/d]",
        "Entwickler ( m / w / d )",
        "Entwickler (d/m/w)",
        "Entwickler (m/w/x/div)",
        "Entwickler (m,w,d)",
        "Entwickler (m-w-d)",
        "Entwickler (ALL GENDERS)",
        "Entwickler (alle Geschlechter)",
        "Entwickler (div)",
        "Entwickler (i)",
        "Entwickler W/M/D",
        "Entwickler*IN",
        "Entwickler*innen",
        "Entwickler:in",
        "Entwickler/in",
        "Entwickler/innen",
        "EntwicklerInnen",
        "Entwickler*in (m/w/d)",
    ],
)
def test_gender_tag_variants_are_stripped(raw) -> None:
    assert strip_gender_tags(raw) == "Entwickler"


@pytest.mark.parametrize(
    "raw",
    [
        "Berlin Admin",  # "in" at the end of a word is not a suffix: only a capital "In" after a letter is
        "Marketing Robin Benjamin Berlin",
        "Plug-In Developer",  # not after a letter
        "InfoSec Analyst",  # "In" at the start of a word
        "BuildInfo Engineer",  # "In" inside a word: no word boundary after it
        "Intern Marketing",
        "Product Owner (Scrum)",  # a parenthesis that is not a tag list
        "Engineer (Data & Platform)",
        "Engineer (m/w/d/Teilzeit)",  # the list has to end at the closing bracket
        "Engineer (Berlin)",
        "m/w/d Engineer",  # the bare list is only stripped at the end
        "Consultant (SAP) - Stuttgart",
        "Software-Entwickler",
        # a capital or a singular `in` after a slash starts a word, it is no suffix (fix wave F1)
        "DevOps/Infrastructure Engineer",
        "Vertrieb/Innendienst",
        "Sales national/international",
    ],
)
def test_text_that_is_not_a_gender_tag_is_kept(raw) -> None:
    assert strip_gender_tags(raw) == raw


@pytest.mark.parametrize(
    ("raw", "stripped"),
    [
        # spec §8.2: only `In` needs a word boundary; the symbol forms also end a compound's first part
        ("Entwickler:innenteam", "Entwicklerteam"),
        ("Entwickler*innenteam", "Entwicklerteam"),
        ("Entwickler_innenteam Berlin", "Entwicklerteam Berlin"),
        ("Kolleg/innenkreis", "Kollegkreis"),
    ],
)
def test_a_gender_suffix_inside_a_compound_is_stripped(raw: str, stripped: str) -> None:
    assert strip_gender_tags(raw) == stripped


@pytest.mark.parametrize(
    ("raw", "title"),
    [
        ("DevOps Engineer (m/w/d), Berlin", "DevOps Engineer, Berlin"),  # no space before the comma
        ("Engineer (m/w/d): Backend", "Engineer: Backend"),
        ("Engineer ( m / w / d ) , Berlin", "Engineer, Berlin"),
        ("Engineer (Data (m/w/d))", "Engineer (Data)"),
        ("Engineer (m/w/d) - Berlin", "Engineer - Berlin"),  # a dash is a separator, not punctuation
        ("Backend (m/w/d) Entwickler", "Backend Entwickler"),
    ],
)
def test_a_removed_tag_leaves_no_space_before_punctuation(raw: str, title: str) -> None:
    assert normalize_title(raw).title == title


def test_gender_tag_pattern_is_the_bracketed_list_of_the_spec() -> None:
    # Spec §8.2 step 2, verbatim.
    assert GENDER_TAG.pattern == (
        r"[\(\[]\s*(m|w|f|d|x|div|gn|i|\*|all genders?|alle geschlechter)"
        r"(\s*[/|,_\-]\s*(m|w|f|d|x|div|gn|i|\*))*\s*[\)\]]"
    )
    assert GENDER_TAG.flags & re.IGNORECASE
    assert GENDER_TAG.search("Engineer (m/w/d)")
    assert GENDER_TAG.search("Engineer [all genders]")
    assert GENDER_TAG.search("Engineer (ALLE GESCHLECHTER)")
    assert GENDER_TAG.fullmatch("(w/m/d)") and GENDER_TAG.fullmatch("[f|m|x]") and GENDER_TAG.fullmatch("(*)")
    assert not GENDER_TAG.search("Engineer (Data)") and not GENDER_TAG.search("Engineer m/w/d")
    assert not GENDER_TAG.search("Engineer (m/w/d")  # unbalanced


def test_strip_gender_tags_tidies_the_gap() -> None:
    assert strip_gender_tags("Backend (m/w/d) Entwickler") == "Backend Entwickler"
    assert strip_gender_tags("  Backend   Entwickler (m/w/d)  ") == "Backend Entwickler"
    assert strip_gender_tags("") == ""


def test_normalized_title_and_tokens() -> None:
    t = normalize_title("Sr. Software Engineer (Data & Platform) \N{EN DASH} Berlin")
    assert (t.title_norm, t.seniority_tokens) == (
        "senior software engineer (data & platform) berlin",
        frozenset({"senior"}),
    )
    assert normalize_title("Werkstudent (m/w/d) Backend").seniority_tokens == {"working_student"}
    assert normalize_title("Head of Engineering").seniority_tokens == {"head"}
    assert normalize_title("Teamleiterin Buchhaltung").seniority_tokens == {"lead"}
    assert normalize_title("Software\N{SOFT HYPHEN}entwickler").title_norm == "softwareentwickler"
    assert (
        normalize_title("Senior Python Entwickler (m/w/d) | Berlin").title_norm
        == "senior python entwickler berlin"
    )


def test_title_is_the_display_form() -> None:
    t = normalize_title("  Sr.  Software\N{NO-BREAK SPACE}Engineer (m/w/d) \N{EN DASH} Berlin \N{EN DASH}  ")
    # norm_text and gender tags are applied; the wording, case, abbreviation and inner separator are kept.
    assert t.title == "Sr. Software Engineer \N{EN DASH} Berlin"
    assert t.title_norm == "senior software engineer berlin"
    assert t == NormalizedTitle(
        "Sr. Software Engineer \N{EN DASH} Berlin", "senior software engineer berlin", frozenset({"senior"})
    )
    assert normalize_title("Senior Engineer - Berlin").title == "Senior Engineer - Berlin"
    assert normalize_title("Entwicklerin (w/m/d)").title == "Entwicklerin"
    assert normalize_title("").title == "" and normalize_title("").title_norm == ""
    assert normalize_title("(m/w/d)").title_norm == ""


@pytest.mark.parametrize(
    ("raw", "title_norm"),
    [
        ("Jr. Developer", "junior developer"),
        ("Jr Developer", "junior developer"),
        ("JR. Developer", "junior developer"),
        ("Sr Engineer", "senior engineer"),
        ("Snr. Engineer", "senior engineer"),
        ("Engineer, Sr.", "engineer senior"),
        ("Sr.Engineer", "senior engineer"),
        ("Srinivas Kumar", "srinivas kumar"),  # the abbreviation is a whole word
        ("Jrock Engineer", "jrock engineer"),
    ],
)
def test_abbreviations_are_expanded(raw, title_norm) -> None:
    assert normalize_title(raw).title_norm == title_norm


@pytest.mark.parametrize(
    ("raw", "title_norm"),
    [
        ("- Engineer -", "engineer"),
        ("\N{EM DASH} Engineer \N{EN DASH} Berlin \N{EM DASH}", "engineer berlin"),
        ("Engineer | Berlin | Remote", "engineer berlin remote"),
        ("Engineer, Berlin: Backend", "engineer berlin backend"),
        ("Engineer - - Berlin", "engineer berlin"),
        ("Engineer \N{EN DASH}- Berlin", "engineer berlin"),
        ("Engineer \N{EN DASH} Berlin (m/w/d) -", "engineer berlin"),
        ("Full-Stack Developer", "full stack developer"),  # a separator inside a word is a separator too
        ("R&D Engineer & Scientist", "r&d engineer & scientist"),  # & stays
        ("Engineer (Data & Platform)", "engineer (data & platform)"),
        ("Müller-Straße Café", "muller strasse cafe"),
        ("ÄRZTIN", "arztin"),
        ("   ", ""),
        ("-", ""),
    ],
)
def test_separators_collapse_and_trim(raw, title_norm) -> None:
    assert normalize_title(raw).title_norm == title_norm


TOKEN_CASES = [
    # intern
    ("Praktikum Marketing", {"intern"}),
    ("Praktikant Vertrieb", {"intern"}),
    ("Praktikantin Vertrieb", {"intern"}),
    ("Praktika im Ausland", {"intern"}),
    ("Intern Data Science", {"intern"}),
    ("Internship Backend", {"intern"}),
    # working_student
    ("Werkstudent Backend", {"working_student"}),
    ("Werkstudentin Backend", {"working_student"}),
    ("Werkstudenten gesucht", {"working_student"}),
    ("Working Student Data", {"working_student"}),
    # apprentice
    ("Ausbildung Fachinformatiker", {"apprentice"}),
    ("Azubi Mediengestalter", {"apprentice"}),
    ("Auszubildende Kauffrau", {"apprentice"}),
    ("Apprentice Developer", {"apprentice"}),
    ("Trainee Controlling", {"apprentice"}),
    # junior
    ("Junior Developer", {"junior"}),
    ("Jr. Developer", {"junior"}),
    ("Einsteiger Softwareentwicklung", {"junior"}),
    ("Berufseinsteiger Vertrieb", {"junior"}),
    ("Entry Level Analyst", {"junior"}),
    ("Entry-Level Analyst", {"junior"}),
    ("Graduate Program", {"junior"}),
    # senior
    ("Senior Developer", {"senior"}),
    ("Sr. Developer", {"senior"}),
    ("Snr Developer", {"senior"}),
    # lead
    ("Lead Developer", {"lead"}),
    ("Teamlead Backend", {"lead"}),
    ("Team Lead Backend", {"lead"}),
    ("Team-Lead Backend", {"lead"}),
    ("Teamleiter Vertrieb", {"lead"}),
    ("Teamleiterin Vertrieb", {"lead"}),
    ("Teamleitung Vertrieb", {"lead"}),
    ("Tech Lead Platform", {"lead"}),
    # principal
    ("Principal Engineer", {"principal"}),
    ("Staff Engineer", {"principal"}),
    # head
    ("Head of Engineering", {"head"}),
    ("Leiter Entwicklung", {"head"}),
    ("Leiterin Entwicklung", {"head"}),
    ("Director Engineering", {"head"}),
    ("VP Engineering", {"head"}),
    # combinations and none
    ("Senior Tech Lead", {"senior", "lead"}),
    ("Junior Praktikant", {"junior", "intern"}),
    ("Software Engineer", set()),
    ("Entwickler", set()),
    ("", set()),
    # whole words only, except the three prefix stems
    ("International Sales Manager", set()),
    ("Internal Auditor", set()),
    ("Leiterplatten Designer", set()),
    ("Leadership Coach", set()),
    ("Staffing Consultant", set()),
    ("Seniorenbetreuer", set()),
    ("Headhunter", set()),
    ("Vpn Administrator", set()),
    ("Directory Services Engineer", set()),
]


@pytest.mark.parametrize(("raw", "expected"), TOKEN_CASES)
def test_seniority_tokens(raw, expected) -> None:
    assert normalize_title(raw).seniority_tokens == frozenset(expected)
    assert seniority_tokens(normalize_title(raw).title_norm) == frozenset(expected)


def test_seniority_tokens_work_on_a_folded_title_directly() -> None:
    assert seniority_tokens("senior software engineer") == frozenset({"senior"})
    assert seniority_tokens("leiter produktion") == frozenset({"head"})
    assert seniority_tokens("") == frozenset()
    assert isinstance(seniority_tokens("lead"), frozenset)


def test_seniority_ladder_is_the_enum_order_and_covers_every_token() -> None:
    assert SENIORITY_LADDER == (
        "intern",
        "working_student",
        "apprentice",
        "junior",
        "mid",
        "senior",
        "lead",
        "principal",
        "head",
    )
    # `mid` is the level without a token: every other rung is a token and every token is a rung.
    assert set(get_args(SeniorityToken)) == set(SENIORITY_LADDER) - {"mid"}
    assert [t for t in SENIORITY_LADDER if t != "mid"] == list(get_args(SeniorityToken))
