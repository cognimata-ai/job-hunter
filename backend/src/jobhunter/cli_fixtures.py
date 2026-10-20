"""The fixture scrubber, `jobhunter fixtures scrub <in> <out>` (spec §18.4, §2.3; plan Task 6, ruling P6).

Raw provider responses carry recruiter personal data and session tokens; only their scrubbed form is
committed. `scrub_tree` copies a directory of recordings (bodies, `.headers.json`, `.meta.json`) and scrubs
every file on the way: nothing is copied unchanged, and the recorder's `budget.json` and `budget.lock` are
never copied.

The scrub is deterministic: one `Pseudonyms` per source directory numbers each distinct value per category
in order of first appearance (`[person:1]`, `[email:2]`, `[token:3]`) and files are read in sorted order, so
a second run writes the same bytes, one value gets one placeholder in every file of its source (the cursor of
a page and the cursor its next request sent), and a recording added to one source renumbers no other.

A JSON document is walked with its path and scrubbed per leaf (`scrub_document`). A string under an
identifier field of its source (`IDENTIFIER_FIELDS`, keyed by the leaf key) gets that field's rule and nothing
else:

- `keep`: the value stays (`job_uid`, `htidocid`, `id`, `slug`, ...: the identities of the §5.3 contracts);
- `url`: a link (`scrub_url`). Its path stays, because the job ids and slugs in it are the §8.4 publisher
  keys, except a `linkedin.com/in/<x>` or `xing.com/profile/<x>` user path (`[user:n]`). A query or fragment
  value of `utm_*` becomes `scrubbed`; of `se`, `v`, `adref`, `fc` or a credential parameter `[token:n]`; any
  other goes through `scrub_text`. Userinfo is dropped;
- `token`: `[token:n]` whatever the value's shape (cursors, page tokens, search and request ids);
- `serpapi_job_id`: the base64 JSON is decoded, `fc` becomes `scrubbed`, `htidocid` stays, the other strings
  go through `scrub_text`, and it is encoded again in its own alphabet;
- `jsearch_job_id`: `<job_uid>:<session blob>` in base64; the blob becomes `scrubbed`.

Every other string is free text and goes through `scrub_text`. Every http(s) link in it (`URL_IN_TEXT`) gets
the `url` rule. Outside the links, in this order: names near a contact marker (`Ansprechpartner`, `Kontakt`,
`Contact`, `Your contact`: 2 or 3 capitalised words within 120 characters after it), right before an e-mail
address or a phone number, after a salutation, in a team introduction, next to a role word in narrative text
(`ROLE_NAME`: "CEO und Gründer Max Mustermann", "Max Mustermann, CEO", "Gründer Max") and after an introducing
verb or an interview (`INTRODUCED_NAME`: "founded by", "gegründet von", "created by", "erstellt von",
"interview with", "Interview mit") become `[person:n]`, except a job title (every word in `TITLE_WORDS`, or a
gender marker after it) and a company (a legal form of `LEGAL_FORMS` in or right after it); a later mention of
a role-named or introduced person, by the whole name, its first or its last word, gets the same placeholder.
E-mail addresses `[email:n]`; phone numbers (`redact.PHONE`, the parenthesised area code `PHONE_PAREN` of
"(030) 123456", spaced groups `PHONE_GROUPS`, the unlabelled forms `PHONE_MORE`, a number after a phone label)
`[phone:n]`; then every `redact.KEY` match `[token:n]`. A string under a person key (`is_person_key`:
`PERSON_KEYS` such as `creator` and `recruiter`, and by the suffix rule `contact`, `employer_contact`,
`createdBy`, `recruiterName`, `hiring_manager_name`; its name or picture when it is an object) is `[person:n]`
(a picture `[token:n]`). The `KEY` pass runs on non-URL free text only, because `KEY` matches the ids and
slugs of real links. Object keys are never rewritten, so the wire structure stays. XML, HTML and text bodies
are scrubbed as free text. Before any of it, the values of this machine's provider key variables
(`SECRET_VARS`), wherever they stand, become `[key:n]`.

`valid_url`, `validate_identifier`, `role_names` (`ROLE_NAME` and `INTRODUCED_NAME`) and `contact_names` (the
contact window) are also checks of the PII scan (tests/contract/test_fixture_pii.py). `valid_url` refuses a
credential parameter (`api_key`, `token`, ...) whose value is set and is no placeholder. Ruling FR-m: where a
rule is unsure, it scrubs.
"""

import argparse
import base64
import binascii
import json
import os
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Final, Literal, get_args
from urllib.parse import quote_plus, unquote, unquote_plus, urlsplit

import httpx

from jobhunter.normalize.titles import GENDER_TAG
from jobhunter.normalize.urls import host_is
from jobhunter.observability.redact import EMAIL, KEY, PHONE
from jobhunter.sources.http import stored_headers
from jobhunter.types import JSONValue

Category = Literal["person", "email", "phone", "user", "token", "key"]
FieldRule = Literal["keep", "url", "token", "serpapi_job_id", "jsearch_job_id"]

SECRET_VARS: Final = ("RAPIDAPI_KEY", "SERPAPI_API_KEY", "ADZUNA_APP_ID", "ADZUNA_APP_KEY")
# The recorder's accounting files: never copied into the fixtures.
NEVER_COPIED: Final = frozenset({"budget.json", "budget.lock"})
TEXT_SUFFIXES: Final = (".xml", ".html", ".txt")
PLACEHOLDER: Final = re.compile(r"\[(?:person|email|phone|user|token|key):\d+\]")
TOKEN_PLACEHOLDER: Final = re.compile(r"\[token:\d+\]")

# An http(s) link in free text. It ends at whitespace, a quote, `<`, `>`, a backslash, an HTML-escaped quote
# or angle bracket (`&quot;`, `&lt;`, `&#39;`: links inside escaped HTML), and never on trailing punctuation.
_ESCAPED_DELIMITER: Final = r"&(?:quot|lt|gt|apos|#0*3[49]|#x0*2[27]|#0*6[02]|#x0*3[ce]);"
URL_IN_TEXT: Final[re.Pattern[str]] = re.compile(
    rf"https?://(?:(?!{_ESCAPED_DELIMITER})[^\s\"'<>\\`{{}}|^])+(?<![.,;:!?)])", re.IGNORECASE
)
# The PII scan's own phone pattern (test_fixture_pii.py): "(030) 123456", which redact.PHONE misses.
PHONE_PAREN: Final = re.compile(r"\(0\d{2,5}\)\s?(?:\d{4,}|\d{2,}(?:[ /\-]\d{2,})+)(?:[ /\-]\d{2,})*(?!\w)")
# An area code and two-digit or longer groups split by spaces or (spaced) dashes, which `redact.PHONE` misses:
# "08106 \u2013 39 80 370" (an en dash), "030 12 34 56" (found in the manual review of the recordings).
PHONE_GROUPS: Final = re.compile(
    r"(?<![\w+\-.,/])0\d{2,5}(?:\s?[ /\-\u2013\u2014]\s?\d{2,})(?:[ \-\u2013\u2014]\d{2,})*(?!\w)"
)
# Unlabelled forms that German pages use and none of the patterns above matches (6a review, finding 3):
# `(0)30 1234567`, an `00` country prefix (`0049 30 1234567`), dotted (`+49.30.1234567`, `030.1234567`),
# spaced groups after a slash (`030 / 12 34 56 78`), a mobile number without separators (`01711234567`) and
# a dotted area code before mixed groups (`030.12 34 56`, `089.12.34.56`; fix wave F1, ledger L78).
# Every repetition is bounded, so the pattern stays linear; an id, a UUID group (after `-`) and an epoch
# timestamp (no leading 0) do not match.
PHONE_MORE: Final = re.compile(
    r"(?<![\w+\-.])(?:"
    r"\(0\)\s?\d{2,5}(?:[ /\-.]?\d{2,8}){1,4}"
    r"|00[1-9]\d{0,2}(?:[ /\-.]{1,3}\d{2,8}){2,5}"
    r"|\+\d{1,3}(?:\.\d{2,8}){2,5}"
    r"|0\d{2,5}\.\d{2,8}(?:[ .]\d{2,8}){1,4}"
    r"|0\d{2,5}\.\d{3,8}(?:\.\d{2,8}){0,3}"
    r"|0\d{2,5}\s?/\s?\d{2,8}(?:\s\d{2,8}){0,5}"
    r"|01[5-7]\d{8,9}"
    r")(?!\w)"
)
# A number after a phone label, whatever its separators ("Telefon: 0301234567"); the label stays.
LABELLED_PHONE: Final = re.compile(
    r"(?i)(\b(?:tel(?:efon|ephone)?|phone|mobil(?:e|funk)?|handy|fon|fax|whatsapp|ruf)\b\.?\s*"
    r"(?:\([^)\d]{0,20}\))?\s*(?:nr\.?|no\.?)?\s*[:.]?\s*)([+(]?\d[\d /().\-]{5,}\d)(?!\w)"
)
CONTACT_MARKER: Final = re.compile(r"(?i)\b(?:ansprechpartner\w*|kontakt\w*|contact\w*|your\s+contact)")
CONTACT_WINDOW: Final = 120

_UPPER: Final = "A-Z\u00c0-\u00d6\u00d8-\u00de\u0100-\u017f"
_LOWER: Final = "a-z\u00df-\u00f6\u00f8-\u00ff\u0100-\u017f"
# Words that stand next to names but are none: the markers themselves and the contact labels.
_NOT_A_NAME: Final = (
    r"(?!(?:Ansprechpartner\w*|Kontakt\w*|Contact\w*|Your|Tel|Telefon|Telephone|Phone|Mobil\w*|Handy|Fax|Mail"
    r"|Email|E-Mail|Web|Website|Adresse|Address)\b)"
)
_CAPITALISED: Final = rf"{_NOT_A_NAME}[{_UPPER}][{_LOWER}]+(?:[-'\u2019][{_UPPER}]?[{_LOWER}]+)*"
_PARTICLE: Final = r"(?:von|van|de|der|den|du|di|da|zu|zur|vom|ter|ten|la|le|el|al|bin|ibn)"
NAME: Final = re.compile(rf"{_CAPITALISED}(?:[ \t]+(?:{_PARTICLE}[ \t]+)?{_CAPITALISED}){{1,2}}")
# What can stand between a name and its e-mail address or phone number: separators, a contact label, and the
# joining words of "steht Dir <name> unter <phone>" or "<name> per E-Mail an <e-mail>" (found in the 6b
# review). At most 12 pieces, each one character or one word, so the match stays linear.
_CONTACT_GLUE: Final = (
    r"(?:[\s,;:|(<\-\u2013\u2014/]|(?i:unter|via|at|on|per|an|bei|telefonisch|erreichbar|reachable"
    r"|e-?mail|mail|tel(?:efon)?\.?|phone|mobil|fon)){0,12}"
)
NAME_BEFORE_EMAIL: Final = re.compile(rf"({NAME.pattern}){_CONTACT_GLUE}(?={EMAIL.pattern})")
# Found in the manual review of the recordings: a signature's name right before its phone number; a surname
# (or names) after a salutation or an academic title ("Herr Mustermann", "Mr. Doe", "Prof. Dr. ..."); and in
# the contact window a first name alone after "an", "bei", "to" or "with" ("wende dich gerne an Lena").
NAME_BEFORE_PHONE: Final = re.compile(
    rf"({NAME.pattern}){_CONTACT_GLUE}(?=\+\d|\(0\d|0\d{{2,5}}[ /\-\u2013\u2014.])"
)
_ONE_NAME: Final = rf"{_CAPITALISED}(?:[ \t]+(?:{_PARTICLE}[ \t]+)?{_CAPITALISED}){{0,2}}"
SALUTED_NAME: Final = re.compile(
    rf"\b(?:(?:Herrn?|Frau|Mr\.?|Mrs\.?|Ms\.?|Miss|Mx\.?)[ \t]+(?:(?:Dr|Prof)\.[ \t]*)*|(?:Dr|Prof)\.[ \t]*"
    rf"(?:(?:Dr|Prof)\.[ \t]*)*)({_ONE_NAME})"
)
NAME_AFTER_PREPOSITION: Final = re.compile(rf"\b(?:an|bei|to|with)[ \t]+({_CAPITALISED})\b")
# Found in the 6b review: founders who introduce themselves by first name as the ad's contacts ("Hinter <firm>
# stehen wir: <name> und <name>", also with tags around the names, and "wir (<name> & <name>)"). Two to five
# capitalised words joined by commas, und, and or &, after an introducing verb only ("stehen wir:", "das sind
# wir:", "that's us:") or in brackets after wir/we/us, so "Das bieten wir: Urlaub und Gehalt" stays.
_TEAM_JOIN: Final = r"\s*(?:,|(?i:und|and)|&(?:amp;)?)\s*"
_TAGS: Final = (
    r"(?:\s*(?:<[^<>]{1,40}>|&lt;(?:(?!&gt;).){1,40}&gt;))*\s*"  # at most a few short tags, bounded
)
TEAM_INTRODUCTION: Final = re.compile(
    rf"(?:(?i:\b(?:stehen|sind)\s+wir|\b(?:that's|that is|this is|these are|meet)\s+us)\s*:{_TAGS}"
    rf"|(?i:\b(?:wir|we|us))\s*\(\s*)"
    rf"({_CAPITALISED}(?:{_TEAM_JOIN}{_CAPITALISED}){{1,4}})"
)

# Fix wave F1 (final review C1): a person named next to a role word in narrative text, which the contact rules
# above miss ("gestaltest du mit unserem CEO und Gründer <name>"). Role words match in any case and chain with
# "und", "and", "&", "/" or a comma ("CEO und Gründer", "Co-Founder & CTO"). A name is one to three
# capitalised words with a particle or a middle initial between them ("Erika C. Musterfrau"):
# - after a lead role (`LEAD_ROLE`) and a space, a line break or a few inline tags: one to three words
#   ("Gründer Max"); after a dash two or three ("Founder-Max Mustermann"), as one word there is a compound
#   ("CEO-Level"). After a comma the next item of a list follows ("the CFO, Strategic Finance"), after a colon
#   a heading ("CEO: Regelmäßiger Austausch") and after a block tag the next line ("Reports to:
#   CEO</p><p>Working ..."): no name;
# - after a contact word (`CONTACT_ROLE`) and a space, a line break, a dash or a colon: two or three words, as
#   one word there is a heading ("Contact Information");
# - before a lead role, then a comma, an opening bracket or a dash: two or three words ("Max Mustermann, CEO",
#   "Jane Doe (Co-Founder)"; an en or em dash between spaces counts as a dash).
# A later bare mention of the name, or of its first word, in the same text gets the same placeholder
# (`_echoes`). `_NOT_A_ROLE_NAME` is the narrow allow-list of capitalised words next to a role word that name
# nobody: the role and contact words, the contact labels, pronouns and articles, job-title words ("Product
# Owner, founder"), firm words ("Global Founders Capital") and the compounds of the recorded ads ("Founders
# Associate", "Founder-Led Sales", "CEO Office", "mit dem Geschäftsführer Strukturen schaffen").
_UE, _AE = "(?:ü|ue)", "(?:ä|ae)"
LEAD_ROLE: Final = (
    rf"(?:co-?founders?|cofounders?|founders?|(?:mit)?gr{_UE}nder(?:in(?:nen)?|s)?|ceo|cto|coo|cfo|cpo|cmo"
    rf"|gesch{_AE}ftsf{_UE}hrer(?:in(?:nen)?|s)?|inhaber(?:in(?:nen)?|s)?|managing\s+director)"
)
CONTACT_ROLE: Final = r"(?:ansprechpartner(?:in(?:nen)?|s)?|ansprechperson(?:en)?|kontakt|contact)"
_NOT_A_ROLE_NAME: Final = (
    r"(?!(?:Ansprechpartner\w*|Ansprechperson\w*|Kontakt\w*|Contact\w*|Your|Tel|Telefon|Telephone|Phone|Mobil\w*"
    r"|Handy|Fax|Mail|Email|E-Mail|Web|Website|Adresse|Address"
    rf"|Co-?[Ff]ounders?|Cofounders?|Founders?|(?:Mit)?[Gg]r{_UE}nder\w*|Gesch{_AE}ftsf{_UE}hrer\w*|Inhaber\w*|Managing"
    r"|Du|Dich|Dir|Dein\w*|Sie|Ihr\w*|Ihnen|Wir|Uns\w*|Der|Die|Das|Den|Dem|Des|Ein\w*|We|You|Our|Us|The|An?"
    r"|Managers?|Owners?|Engineers?|Developers?|Directors?|Officers?|Leads?|Consultants?|Designers?|Analysts?"
    r"|Architects?|Specialists?|Recruiters?|Partners?|Capital|Ventures?|Funds?|Group|Labs?|Holding"
    r"|Associates?|Assistants?|Office|Teams?|Led|Strukturen)\b)"
)
_ROLE_NAME_WORD: Final = rf"{_NOT_A_ROLE_NAME}[{_UPPER}][{_LOWER}]+(?:[-'\u2019][{_UPPER}]?[{_LOWER}]+)*"
_ROLE_NAME_GAP: Final = rf"[ \t]+(?:{_PARTICLE}[ \t]+|[{_UPPER}]\.[ \t]*)?"
_ONE_TO_THREE: Final = rf"{_ROLE_NAME_WORD}(?:{_ROLE_NAME_GAP}{_ROLE_NAME_WORD}){{0,2}}"
_TWO_OR_THREE: Final = rf"{_ROLE_NAME_WORD}(?:{_ROLE_NAME_GAP}{_ROLE_NAME_WORD}){{1,2}}"
# Spaces, a line break and inline tags (`<strong>CEO</strong> Max`), raw or HTML-escaped; a block tag ends the
# line. No two parts can match the same characters, so a long run of spaces cannot make the match slow.
_INLINE_TAG: Final = (
    r"(?:</?(?:strong|b|em|i|u|span|a|font)\b[^<>]{0,40}>"
    r"|&lt;/?(?:strong|b|em|i|u|span|a|font)\b(?:(?!&gt;).){0,40}&gt;)"
)
_ROLE_SPACE: Final = rf"(?=[\s<&])[ \t]*(?:\r?\n[ \t]*)?(?:{_INLINE_TAG}[ \t]*(?:\r?\n[ \t]*)?)*"
# A hyphen joins without spaces ("Founder-Max Mustermann"); a spaced hyphen separates the levels of a label
# ("Brand Marketing - CMO - Acme"), an en or em dash may stand between spaces.
_ROLE_DASH: Final = r"(?:-|[ \t]*[\u2013\u2014][ \t]*)"
_NAME_END: Final = rf"(?![{_UPPER}{_LOWER}'\u2019])"


def _role(words: str) -> str:
    return rf"(?i:\b{words}\b)"


_LEAD_CHAIN: Final = (
    rf"{_role(LEAD_ROLE)}(?:[ \t]*(?:&(?:amp;)?|/|,|\bund\b|\band\b)[ \t]*{_role(LEAD_ROLE)})*"
)
ROLE_NAME: Final = re.compile(
    rf"{_LEAD_CHAIN}(?:{_ROLE_SPACE}({_ONE_TO_THREE})|{_ROLE_DASH}({_TWO_OR_THREE})){_NAME_END}"
    rf"|{_role(CONTACT_ROLE)}(?:{_ROLE_DASH}|[ \t]*:[ \t]*|{_ROLE_SPACE})({_TWO_OR_THREE}){_NAME_END}"
    rf"|(?<![{_UPPER}{_LOWER}])({_TWO_OR_THREE})(?:[ \t]*[,(][ \t]*|{_ROLE_DASH}){_LEAD_CHAIN}"
)
# Ruling FR-f (fix round 1): the verbs that introduce a person ("founded by", "co-founded by", "gegründet
# von", "created by", "erstellt von", with a short "in Berlin 2019" between the verb and "by"/"von", and an
# optional colon after it) and an interview ("interview with", "Interview mit"), then two or three capitalised
# words. Every space run here is bounded, and no two parts can match the same characters, so a match stays
# linear in the text (fix round 2).
_SHORT_GAP: Final = r"(?:[ \t]{1,3}[^\s.;:!?<>][^.;:!?<>\n]{0,39}?)?"
_LABEL_COLON: Final = r"(?:[ \t]{0,3}:)?"
INTRODUCED_NAME: Final = re.compile(
    rf"(?:(?i:\b(?:co-?)?founded){_SHORT_GAP}[ \t]{{1,3}}(?i:by)"
    rf"|(?i:\b(?:mit)?gegr(?:ü|ue)ndet){_SHORT_GAP}[ \t]{{1,3}}(?i:von)"
    rf"|(?i:\bcreated[ \t]{{1,3}}by|\berstellt[ \t]{{1,3}}von|\binterview[ \t]{{1,3}}(?:with|mit)))\b"
    rf"{_LABEL_COLON}{_ROLE_SPACE}({_TWO_OR_THREE}){_NAME_END}"
)
# A second or third person right after an introduced one ("founded by <name> and <name>").
_JOINED_NAME: Final = re.compile(
    rf"[ \t]{{0,3}}(?:,|\band\b|\bund\b|&(?:amp;)?)[ \t]{{0,3}}({_TWO_OR_THREE}){_NAME_END}"
)
# A company is no person (ruling FR-f): a found name is dropped when one of its words, or of the capitalised
# words right after it, is a legal form ("founded by Acme Inc.", "created by Beispiel Ltd for you", "Kontakt:
# Muster Software GmbH", "erstellt von Muster Co. KG"). Compared case-sensitively, without a trailing comma,
# bracket or full stop.
LEGAL_FORMS: Final = frozenset(
    {"GmbH", "gGmbH", "mbH", "AG", "SE", "KG", "KGaA", "OHG", "UG", "eG", "e.V", "e. V", "GbR", "PartG"}
    | {"Inc", "Ltd", "LLC", "LLP", "PLC", "plc", "Corp", "Co", "Limited", "S.A", "SAS", "SARL", "B.V", "N.V"}
    | {"AB", "ASA", "Oy", "S.p.A", "S.r.l", "SA", "BV", "NV"}
)
# Ruling FR-m (fix round 2, the privacy fail-safe): the scrubber prefers a false positive to a false negative,
# so a job title suppresses a found name only through this explicit, case-sensitive vocabulary of department,
# region and title-tail words, plus the gender markers (`_TITLE_TAIL`); no suffix or place heuristic. A found
# name is a title when every word of it is in `TITLE_WORDS` ("Geschäftsführer Vertrieb und Marketing (m/w/d)",
# "Managing Director Germany", "CTO Platform Engineering", "Contact Customer Service"), or when a gender
# marker follows it, after more vocabulary words and joiners only ("Geschäftsführer Spedition (m/w/d)"). The
# last group are the gate's false positives on the committed fixtures, the documented allow-list: a contact
# window in `arbeitnow/page1.json` reaches "Umsetzung von Vertriebsstrategien" once placeholders have
# shortened the text before it.
TITLE_WORDS: Final = frozenset(
    {"Vertrieb", "Technik", "Marketing", "Finanzen", "Finance", "Personal", "Operations", "Platform"}
    | {"Engineering", "Customer", "Service", "Services", "Formular", "Seite", "Page", "Sales", "Produkt"}
    | {"Product", "Products", "Einkauf", "Logistik", "Produktion", "Entwicklung", "Forschung", "Recht"}
    | {"Legal", "Strategy", "Strategie", "Digital", "Business", "Data", "Technology", "Technologie"}
    | {"People", "Human", "Resources", "Development", "Software", "Hardware", "Infrastructure", "Security"}
    | {"Commercial", "Growth", "Revenue", "Partnerships", "Innovation", "Research", "Support"}
    | {"Administration", "Verwaltung", "Kommunikation", "Communications", "Center", "Centre", "Desk"}
    | {"Region", "International", "Global", "Europe", "Europa", "EMEA", "DACH", "Nord", "Süd", "Ost"}
    | {"West", "North", "South", "East", "Central", "Deutschland", "Germany", "Österreich", "Austria"}
    | {"Schweiz", "Switzerland"}
    | {"Umsetzung", "Vertriebsstrategien"}
)
_TITLE_WORD: Final = "|".join(sorted(map(re.escape, TITLE_WORDS), key=lambda w: (-len(w), w)))
_TITLE_TAIL: Final = re.compile(
    rf"(?:(?:[ \t]{{0,3}}(?:und|and|&|/|,|-)[ \t]{{0,3}}|[ \t]{{1,3}})"
    rf"(?:{_TITLE_WORD})(?![\w\-'\u2019])){{0,4}}"
    rf"[ \t]{{0,3}}(?:(?i:{GENDER_TAG.pattern})|\(?(?:m/w/d|w/m/d|m/f/d|f/m/d|m/w/x|w/m/x)\)?(?![\w/]))"
)

# Query and fragment parameters whose value is a token whatever it looks like: Adzuna's `se`, `v`, `adref`;
# the Google `fc` session and the other Google session and tracking tokens (`sxsrf` holds a `:`, which keeps
# it out of `KEY`); and the credential parameters (`redact`'s list).
_TOKEN_PARAMS: Final = frozenset(
    {"se", "v", "adref", "fc"}
    | {"sxsrf", "sca_esv", "sca_upv", "ved", "uds", "ei", "usg", "sig", "sei", "gs_lcrp", "shmds", "shem"}
)
_CREDENTIAL_PARAMS: Final = frozenset(
    {"app_id", "app_key", "api_key", "apikey", "key", "token", "access_token", "refresh_token", "secret"}
    | {"client_secret", "password", "authorization", "proxy-authorization"}
)
# `&`, HTML-escaped `&amp;`, and escaped again `&amp;amp;` (a link in escaped HTML inside escaped HTML).
_PARAM_SEPARATOR: Final = re.compile(r"(&(?:amp;)*)")
# User paths: the segment after these prefixes names a person.
_USER_PATHS: Final = (
    ("linkedin.com", re.compile(r"^(/(?:in|pub)/)([^/?#]+)")),
    ("xing.com", re.compile(r"^(/profile/)([^/?#]+)")),
)

# Field shapes (`validate_identifier`).
_HTIDOCID: Final = re.compile(r"[A-Za-z0-9+/_-]{22}(?:==|[A-Za-z0-9+/_-]=|[A-Za-z0-9+/_-]{2})")
_NUMERIC: Final = re.compile(r"\d{1,24}")
_UUID: Final = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_SLUG: Final = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)+")
_SHORT_REFERENCE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,38}")


def _rules(*urls: str, **rules: FieldRule) -> Mapping[str, FieldRule]:
    """A read-only rule table: each of `urls` is a `url` field, `rules` the others."""
    table: dict[str, FieldRule] = {name: "url" for name in urls}
    return MappingProxyType({**table, **rules})


_JSEARCH_URLS: Final = (
    "job_apply_link",
    "apply_link",
    "job_google_link",
    "employer_logo",
    "employer_website",
)
_ATS_URLS: Final = (
    "absolute_url",
    "hostedUrl",
    "applyUrl",
    "jobUrl",
    "careers_url",
    "careers_apply_url",
    "postingUrl",
)
# Per source (the first directory of a fixture path), by the leaf key of the JSON path.
IDENTIFIER_FIELDS: Final[Mapping[str, Mapping[str, FieldRule]]] = MappingProxyType(
    {
        "jsearch": _rules(
            *_JSEARCH_URLS,
            "employer_linkedin",
            job_uid="keep",
            job_id="jsearch_job_id",
            cursor="token",
            request_id="token",
        ),
        "serpapi": _rules(
            "link",
            "share_link",
            "thumbnail",
            "next",
            "json_endpoint",
            "raw_html_file",
            "google_jobs_url",
            job_id="serpapi_job_id",
            htidocid="keep",
            id="token",
            next_page_token="token",  # noqa: S106 - a field name, not a password
            api_key="token",
            account_id="token",
        ),
        "adzuna": _rules("redirect_url", id="keep", adref="token"),
        "arbeitnow": _rules("url", "first", "last", "prev", "next", "path", slug="keep"),
        "ats": _rules(
            *_ATS_URLS,
            "ref",
            "url",
            id="keep",
            requisition_id="keep",
            internal_job_id="keep",
            slug="keep",
            uuid="keep",
        ),
    }
)


class Pseudonyms:
    """Deterministic per-category counters: the first appearance of a value gets the next number."""

    def __init__(self) -> None:
        self._seen: dict[str, dict[str, int]] = {category: {} for category in get_args(Category)}

    def number(self, category: Category, value: str) -> int:
        seen = self._seen[category]
        return seen.setdefault(value, len(seen) + 1)

    def placeholder(self, category: Category, value: str) -> str:
        return f"[{category}:{self.number(category, value)}]"

    def counts(self) -> dict[str, int]:
        """The number of distinct values replaced, per category."""
        return {category: len(seen) for category, seen in self._seen.items()}


# --- free text ---------------------------------------------------------------------------------------------

# Links are stashed while the free-text passes run: a private-use sentinel, the index spelled in private-use
# characters too, so no pattern (digits, words, `\b`) can match inside or next to it.
_SENTINEL_OPEN, _SENTINEL_CLOSE, _SENTINEL_DIGIT0 = "", "", 0xE010
_SENTINEL: Final = re.compile(f"{_SENTINEL_OPEN}([-]+){_SENTINEL_CLOSE}")


def _sentinel(index: int) -> str:
    return _SENTINEL_OPEN + "".join(chr(_SENTINEL_DIGIT0 + int(d)) for d in str(index)) + _SENTINEL_CLOSE


def _sentinel_index(match: re.Match[str]) -> int:
    return int("".join(str(ord(ch) - _SENTINEL_DIGIT0) for ch in match.group(1)))


def _is_title(text: str, start: int, end: int) -> bool:
    """Whether the name found at `text[start:end]` is the rest of a job title: every word of it in
    `TITLE_WORDS`, or a gender marker after it (`_TITLE_TAIL`). Ruling FR-m: nothing else counts."""
    words = [w for w in text[start:end].split() if w[:1].isupper() and not w.endswith(".")]
    return _TITLE_TAIL.match(text, end) is not None or all(w in TITLE_WORDS for w in words)


def _is_company(text: str, start: int, end: int) -> bool:
    """Whether the name found at `text[start:end]` names a company: one of its words, or of the up to three
    capitalised words (or `&`) right after it, is a legal form (`LEGAL_FORMS`; `e. V.` as two words too). A
    comma, colon, semicolon or closing bracket ends the name, so "Max Mustermann, Acme GmbH" stays a person.
    The words are the text's own, so a name that `NAME` ends inside "GmbH" is seen with the whole word."""
    count = len(text[start:end].split())
    words = text[start:].split(None, count + 3)[: count + 3]
    bare = [word.strip(",;:()[]").rstrip(".") for word in words]
    for index, word in enumerate(words):
        if index >= count and words[index - 1][-1:] in ",;:)":
            return False
        if bare[index] in LEGAL_FORMS or (bare[index] == "e" and bare[index + 1 : index + 2] == ["V"]):
            return True
        if index >= count and not (word[:1].isupper() or word == "&"):
            return False
    return False


def _kept(text: str, start: int, end: int) -> bool:
    """A found name that is neither a job title nor a company."""
    return not _is_title(text, start, end) and not _is_company(text, start, end)


def role_names(text: str) -> list[tuple[int, int]]:
    """The spans of the names next to a role word (`ROLE_NAME`) or introduced by a verb or an interview
    (`INTRODUCED_NAME`), job titles left out. The scrubber replaces them; the PII scan fails on any."""
    found = [next(m.span(g) for g in range(1, 5) if m.group(g) is not None) for m in ROLE_NAME.finditer(text)]
    for introduced in INTRODUCED_NAME.finditer(text):
        found.append(introduced.span(1))
        end = introduced.end()
        for _ in range(2):  # at most two more people joined to the first
            if (joined := _JOINED_NAME.match(text, end)) is None:
                break
            found.append(joined.span(1))
            end = joined.end()
    return [(start, end) for start, end in sorted(set(found)) if _kept(text, start, end)]


def contact_names(text: str) -> list[tuple[int, int]]:
    """The spans of the names in the window after a contact marker: 2 or 3 capitalised words, or one after a
    preposition ("Your contact person: Jane Doe", "Ihr Ansprechpartner für Fragen: Max Mustermann"), job
    titles left out ("Kontakt Formular Seite", "Contact Customer Service"). The PII scan fails on any."""
    spans: list[tuple[int, int]] = []
    for marker in CONTACT_MARKER.finditer(text):
        limit = marker.end() + CONTACT_WINDOW
        for pattern, group in ((NAME, 0), (NAME_AFTER_PREPOSITION, 1)):
            for name in pattern.finditer(text, marker.end()):
                if name.start() >= limit:
                    break
                if _kept(text, *name.span(group)):
                    spans.append(name.span(group))
    return spans


_NAME_WORD: Final = re.compile(_ROLE_NAME_WORD)


def _echoes(text: str, names: Iterable[tuple[int, int]]) -> list[tuple[int, int, str]]:
    """Every other mention of a role-named person of two or more words: the whole name, its first word
    alone (a later "mit Max") or its last word alone (a later "Mustermann sagt"), each as a whole word that
    is no allow-listed word; with the person key of the whole name."""
    echoes: list[tuple[int, int, str]] = []
    # one search per person, not per mention
    distinct = {tuple(text[start:end].split()) for start, end in names}
    for words in sorted(w for w in distinct if len(w) >= 2):
        key = " ".join(words)
        whole = r"[ \t\r\n]+".join(map(re.escape, words))
        single = [w for w in dict.fromkeys((words[0], words[-1])) if _NAME_WORD.fullmatch(w)]
        for pattern in (whole, *map(re.escape, single)):
            mention = re.compile(rf"(?<![\w\-'\u2019]){pattern}(?![\w\-'\u2019])")
            echoes.extend((m.start(), m.end(), key) for m in mention.finditer(text))
    return echoes


def _name_spans(text: str) -> list[tuple[int, int, str]]:
    """The spans of names with their person keys: 2 or 3 capitalised words (or one after a preposition)
    within the window after a contact marker, a name right before an e-mail address or a phone number, the
    name after a salutation or an academic title, a name next to a role word and its later mentions.
    Overlapping spans are merged; a merged span is keyed by its own text, unless it is exactly a later mention
    of a role-named person."""
    spans: list[tuple[int, int, str | None]] = [(start, end, None) for start, end in contact_names(text)]
    for pattern in (NAME_BEFORE_EMAIL, NAME_BEFORE_PHONE, SALUTED_NAME, TEAM_INTRODUCTION):
        spans.extend((*m.span(1), None) for m in pattern.finditer(text))
    role_named = role_names(text)
    spans.extend((start, end, None) for start, end in role_named)
    spans.extend(_echoes(text, role_named))
    merged: list[tuple[int, int, list[tuple[int, int, str | None]]]] = []
    for span in sorted(spans, key=lambda s: (s[0], -s[1])):
        if merged and span[0] < merged[-1][1]:
            start, end, parts = merged[-1]
            merged[-1] = (start, max(end, span[1]), [*parts, span])
        else:
            merged.append((span[0], span[1], [span]))
    keyed: list[tuple[int, int, str]] = []
    for start, end, parts in merged:
        echo = next((key for s, e, key in parts if key is not None and (s, e) == (start, end)), None)
        keyed.append((start, end, echo if echo is not None else _person_key(text[start:end])))
    return keyed


def _person_key(name: str) -> str:
    return " ".join(name.split())


def _phone_key(phone: str) -> str:
    return re.sub(r"\D", "", phone)


def _scrub_free(text: str, p: Pseudonyms) -> str:
    """Names, e-mail addresses, phone numbers and key-shaped tokens, in that order (no link handling)."""
    out, last = [], 0
    for start, end, key in _name_spans(text):
        out += [text[last:start], p.placeholder("person", key)]
        last = end
    text = "".join([*out, text[last:]])
    text = EMAIL.sub(lambda m: p.placeholder("email", m.group(0).lower()), text)
    for pattern in (PHONE_PAREN, PHONE, PHONE_GROUPS, PHONE_MORE):
        text = pattern.sub(lambda m: p.placeholder("phone", _phone_key(m.group(0))), text)
    text = LABELLED_PHONE.sub(lambda m: m.group(1) + p.placeholder("phone", _phone_key(m.group(2))), text)
    return KEY.sub(lambda m: p.placeholder("token", m.group(0)), text)


def scrub_text(text: str, p: Pseudonyms) -> str:
    """Free text: every link by the `url` rule, then names, e-mails, phones and keys outside the links."""
    text = text.replace(_SENTINEL_OPEN, "�").replace(_SENTINEL_CLOSE, "�")
    links: list[str] = []

    def stash(match: re.Match[str]) -> str:
        links.append(scrub_url(match.group(0), p))
        return _sentinel(len(links) - 1)

    masked = _scrub_free(URL_IN_TEXT.sub(stash, text), p)
    return _SENTINEL.sub(lambda m: links[_sentinel_index(m)], masked)


# --- links -------------------------------------------------------------------------------------------------


def _token(value: str, p: Pseudonyms) -> str:
    return value if PLACEHOLDER.fullmatch(value) or not value else p.placeholder("token", value)


def _scrub_value(raw: str, p: Pseudonyms) -> str:
    """A query value through `scrub_text`, decoded; re-encoded only when the scrub changed it."""
    decoded = unquote_plus(raw)
    scrubbed = scrub_text(decoded, p)
    return raw if scrubbed == decoded else quote_plus(scrubbed, safe="[]:/")


def _scrub_params(params: str, p: Pseudonyms) -> str:
    """`a=1&b=2` (or HTML-escaped `a=1&amp;b=2`), each value by its parameter's rule, separators kept."""
    pieces = _PARAM_SEPARATOR.split(params)
    for i in range(0, len(pieces), 2):
        name, equals, value = pieces[i].partition("=")
        if not equals:
            pieces[i] = _scrub_value(name, p)
            continue
        folded = unquote_plus(name).lower()
        if folded.startswith("utm_"):
            value = "scrubbed"
        elif folded in _TOKEN_PARAMS or folded in _CREDENTIAL_PARAMS:
            value = _token(value, p)
        else:
            value = _scrub_value(value, p)
        pieces[i] = f"{name}={value}"
    return "".join(pieces)


def _scrub_path(host: str, path: str, p: Pseudonyms) -> str:
    for domain, user_path in _USER_PATHS:
        if host_is(host, domain):
            path = user_path.sub(
                lambda m: (
                    m.group(1)
                    + (
                        m.group(2)
                        if PLACEHOLDER.fullmatch(m.group(2))
                        else p.placeholder("user", unquote(m.group(2)).lower())
                    )
                ),
                path,
            )
    return EMAIL.sub(lambda m: p.placeholder("email", m.group(0).lower()), path)


def scrub_url(url: str, p: Pseudonyms) -> str:
    """The `url` rule: the path kept (user paths excepted), the query and fragment values by their rules."""
    try:
        parts = urlsplit(url)
        _ = parts.port
    except ValueError:
        return _scrub_free(url, p)
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return _scrub_free(url, p)
    netloc = parts.netloc.rsplit("@", 1)[-1]  # userinfo is dropped
    out = f"{parts.scheme}://{netloc}{_scrub_path(parts.hostname.lower(), parts.path, p)}"
    if "?" in url.split("#", 1)[0]:
        out += "?" + _scrub_params(parts.query, p)
    if "#" in url:
        fragment = parts.fragment
        out += "#" + (_scrub_params(fragment, p) if "=" in fragment else _scrub_value(fragment, p))
    return out


def _shady(value: str) -> bool:
    """A query value that holds a key, an e-mail address or a phone number. The key is judged on the decoded
    value, as the scrub judges it (a form-encoded `software+engineer+Berlin+since+yesterday` is words, while a
    percent-encoded key decodes to a key); the e-mail and phone patterns on both forms."""
    decoded = unquote_plus(value)
    return KEY.search(decoded) is not None or any(
        pattern.search(form)
        for form in (value, decoded)
        for pattern in (EMAIL, PHONE, PHONE_PAREN, PHONE_GROUPS, PHONE_MORE)
    )


def valid_url(value: str) -> bool:
    """An http(s) URL of `canonical_url` shape (a scheme and a host, a parseable port), without userinfo,
    with no user path left in place, and whose query and fragment values match no `KEY`, e-mail or phone
    pattern."""
    try:
        parts = urlsplit(value)
        _ = parts.port
    except ValueError:
        return False
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname or "@" in parts.netloc:
        return False
    host = parts.hostname.lower()
    for domain, user_path in _USER_PATHS:
        if (
            host_is(host, domain)
            and (m := user_path.match(parts.path))
            and not PLACEHOLDER.fullmatch(m.group(2))
        ):
            return False
    if EMAIL.search(unquote(parts.path)):
        return False
    for params in (parts.query, parts.fragment):
        for piece in _PARAM_SEPARATOR.split(params)[::2]:
            name, equals, raw = piece.partition("=")
            if _shady(raw if equals else name) or (equals and _credential_value(name, raw)):
                return False
    return True


def _credential_value(name: str, raw: str) -> bool:
    """A credential parameter (`_CREDENTIAL_PARAMS`: `api_key`, `app_id`, `token`, ...) whose value is set
    and is no placeholder: the scrub turns every such value into `[token:n]`, whatever its shape (fix wave
    F1)."""
    value = unquote_plus(raw)
    return (
        unquote_plus(name).lower() in _CREDENTIAL_PARAMS and bool(value) and not PLACEHOLDER.fullmatch(value)
    )


# --- identifiers -------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Base64:
    raw: bytes
    urlsafe: bool
    padded: bool


def _b64decode(value: str) -> _Base64 | None:
    urlsafe = "-" in value or "_" in value
    try:
        raw = base64.b64decode(
            value + "=" * (-len(value) % 4), altchars=b"-_" if urlsafe else None, validate=True
        )
    except (binascii.Error, ValueError):
        return None
    return _Base64(raw, urlsafe, padded=len(value) % 4 == 0)  # unpadded only when it needs padding


def _b64encode(raw: bytes, like: _Base64) -> str:
    encoded = (base64.urlsafe_b64encode(raw) if like.urlsafe else base64.b64encode(raw)).decode("ascii")
    return encoded if like.padded else encoded.rstrip("=")


def _serpapi_object(value: str) -> tuple[dict[str, JSONValue], _Base64] | None:
    decoded = _b64decode(value)
    if decoded is None:
        return None
    try:
        doc: JSONValue = json.loads(decoded.raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return (doc, decoded) if isinstance(doc, dict) else None


def _scrub_serpapi_job_id(value: str, p: Pseudonyms) -> str:
    found = _serpapi_object(value)
    if found is None:
        return _token(value, p)
    doc, decoded = found
    scrubbed: dict[str, JSONValue] = {}
    for key, item in doc.items():
        if key == "fc":
            scrubbed[key] = "scrubbed"
        elif key == "htidocid":
            scrubbed[key] = item
        else:
            scrubbed[key] = scrub_document(item, "", p)
    return _b64encode(
        json.dumps(scrubbed, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), decoded
    )


def _scrub_jsearch_job_id(value: str, p: Pseudonyms) -> str:
    decoded = _b64decode(value)
    if decoded is None:
        return _token(value, p)
    head, colon, _blob = decoded.raw.partition(b":")
    if not colon or not _HTIDOCID.fullmatch(head.decode("ascii", "replace")):
        return _token(value, p)
    return _b64encode(head + b":scrubbed", decoded)


def _apply_rule(rule: FieldRule, value: str, p: Pseudonyms) -> str:
    if rule == "keep":
        return value
    if rule == "token":
        return _token(value, p)
    if rule == "serpapi_job_id":
        return _scrub_serpapi_job_id(value, p)
    if rule == "jsearch_job_id":
        return _scrub_jsearch_job_id(value, p)
    if URL_IN_TEXT.fullmatch(value):
        return scrub_url(value, p)
    return scrub_text(value, p)


# Structured person fields, found by the manual review (SmartRecruiters names each posting's recruiter in
# `creator: {name, avatarUrl}`): a string under one of these keys, or a name or picture under an object of
# one, is a person whatever the source.
PERSON_KEYS: Final = frozenset(
    {
        "creator",
        "recruiter",
        "recruiters",
        "hiring_manager",
        "hiringmanager",
        "contact_person",
        "contactperson",
    }
    | {"owner", "author", "hiring_team", "hiringteam", "coordinator", "sourcer"}
)
_PERSON_NAMES: Final = frozenset(
    {"name", "firstname", "lastname", "fullname", "first_name", "last_name", "full_name"}
)
_PERSON_PICTURES: Final = frozenset(
    {"avatarurl", "avatar", "photourl", "photo", "imageurl", "pictureurl", "picture"}
)
# The suffix rule (fix wave F1, ledger L73). Keys are compared lower-case without `_`, `-` and spaces: a
# person key is one of `PERSON_KEYS`, an audit key naming who did something (`createdBy`), a key that ends in
# a person role (`contact`, `employer_contact`, `hiring_manager`), or a person role or `manager` followed by a
# name field (`recruiterName`, `contactName`, `hiring_manager_name`).
_PERSON_ROLES: Final = (
    "contact",
    "contactperson",
    "recruiter",
    "hiringmanager",
    "creator",
    "owner",
    "author",
    "coordinator",
    "sourcer",
)
_NAMED_ROLES: Final = (*_PERSON_ROLES, "manager")
_AUDIT_PERSON_KEYS: Final = frozenset({"createdby", "updatedby", "modifiedby", "postedby", "publishedby"})


def _compact(key: str) -> str:
    return re.sub(r"[_\-\s]", "", key.lower())


_PERSON_KEYS_COMPACT: Final = frozenset(map(_compact, PERSON_KEYS))
_PERSON_NAMES_COMPACT: Final = frozenset(map(_compact, _PERSON_NAMES))


def is_person_key(key: str) -> bool:
    """Whether a value under `key` names a person (`PERSON_KEYS` and the suffix rule)."""
    k = _compact(key)
    if not k:
        return False
    if k in _PERSON_KEYS_COMPACT or k in _AUDIT_PERSON_KEYS or k.endswith(_PERSON_ROLES):
        return True
    return any(
        k.endswith(suffix) and k.removesuffix(suffix).endswith(_NAMED_ROLES)
        for suffix in _PERSON_NAMES_COMPACT
    )


def _person_rule(path: tuple[str | int, ...]) -> Literal["person", "token"] | None:
    keys = [k for k in path if isinstance(k, str)]
    if not keys or not isinstance(path[-1], str):
        return "person" if keys and is_person_key(keys[-1]) else None
    if is_person_key(keys[-1]):
        return "person"
    if len(keys) >= 2 and is_person_key(keys[-2]):
        if keys[-1].lower() in _PERSON_NAMES:
            return "person"
        if keys[-1].lower() in _PERSON_PICTURES:
            return "token"
    return None


def _field_rule(source: str, path: tuple[str | int, ...]) -> FieldRule | None:
    leaf = path[-1] if path else None
    if not isinstance(leaf, str):
        return None
    rule = IDENTIFIER_FIELDS.get(source, {}).get(leaf)
    if rule is None and source == "serpapi" and "serpapi_pagination" in path and "token" in leaf.lower():
        return "token"  # `serpapi_pagination.*token*`
    return rule


def scrub_document(doc: JSONValue, source: str, p: Pseudonyms) -> JSONValue:
    """`doc` scrubbed per leaf: an identifier field by its rule only, every other string as free text; keys,
    numbers, booleans and nulls stay."""

    def walk(node: JSONValue, path: tuple[str | int, ...]) -> JSONValue:
        if isinstance(node, str):
            if (person := _person_rule(path)) is not None and node.strip():
                return p.placeholder(person, _person_key(node) if person == "person" else node)
            rule = _field_rule(source, path)
            return scrub_text(node, p) if rule is None else _apply_rule(rule, node, p)
        if isinstance(node, dict):
            return {key: walk(value, (*path, key)) for key, value in node.items()}
        if isinstance(node, list):
            return [walk(value, (*path, i)) for i, value in enumerate(node)]
        return node

    return walk(doc, ())


def _keep_shape(key: str, value: str) -> bool:
    if key in ("job_uid", "htidocid"):
        return _HTIDOCID.fullmatch(value) is not None
    if key == "slug":
        return _SLUG.fullmatch(value) is not None or _SHORT_REFERENCE.fullmatch(value) is not None
    if key == "requisition_id":
        return _SHORT_REFERENCE.fullmatch(value) is not None
    return _NUMERIC.fullmatch(value) is not None or _UUID.fullmatch(value) is not None


def validate_identifier(source: str, key: str, value: str) -> bool:
    """Whether `value` has the shape that identifier field `key` of `source` must have after the scrub."""
    rule = IDENTIFIER_FIELDS.get(source, {}).get(key)
    if rule is None:
        return False
    if rule == "url":
        return valid_url(value)
    if rule == "token":
        return TOKEN_PLACEHOLDER.fullmatch(value) is not None
    if rule == "keep":
        return _keep_shape(key, value)
    if rule == "jsearch_job_id":
        decoded = _b64decode(value)
        if decoded is None:
            return False
        head, colon, blob = decoded.raw.partition(b":")
        return (
            bool(colon)
            and blob == b"scrubbed"
            and _HTIDOCID.fullmatch(head.decode("ascii", "replace")) is not None
        )
    found = _serpapi_object(value)
    if found is None:
        return False
    doc, _ = found
    htidocid = doc.get("htidocid")
    return (
        doc.get("fc", "scrubbed") == "scrubbed"
        and (htidocid is None or (isinstance(htidocid, str) and _HTIDOCID.fullmatch(htidocid) is not None))
        and not any(
            _shady(item) for k, item in doc.items() if isinstance(item, str) and k not in ("fc", "htidocid")
        )
    )


# --- files -------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ScrubReport:
    files: int  # files written
    skipped: tuple[str, ...]  # relative paths not copied (the budget files, unknown kinds)
    pseudonyms: Mapping[str, int]  # distinct values replaced, per category


def _secret_values(environ: Mapping[str, str]) -> list[str]:
    values = {environ.get(var, "").strip() for var in SECRET_VARS}
    return sorted((v for v in values if len(v) >= 8), key=lambda v: (-len(v), v))


def _replace_secrets(text: str, secrets: Iterable[str], p: Pseudonyms) -> str:
    """Every standalone occurrence of a secret value (not inside a longer alphanumeric run) as `[key:n]`."""

    def replacement(secret: str) -> Callable[[re.Match[str]], str]:
        return lambda _match: p.placeholder("key", secret)

    for secret in secrets:
        text = re.sub(rf"(?<![A-Za-z0-9]){re.escape(secret)}(?![A-Za-z0-9])", replacement(secret), text)
    return text


def _scrub_headers(doc: JSONValue, p: Pseudonyms) -> dict[str, JSONValue]:
    """`{status, headers}` with the §5.2 header allowlist only (lower-case names), each value scrubbed."""
    raw = doc.get("headers") if isinstance(doc, dict) else None
    if not isinstance(doc, dict) or not isinstance(raw, dict):
        raise ValueError("a .headers.json file is {status, headers}")
    kept = stored_headers(httpx.Headers({str(k): str(v) for k, v in raw.items() if isinstance(v, str | int)}))
    headers: dict[str, JSONValue] = {name: scrub_text(value, p) for name, value in sorted(kept.items())}
    return {"status": doc.get("status"), "headers": headers}


def _dump(doc: JSONValue) -> str:
    return json.dumps(doc, ensure_ascii=False, indent=2) + "\n"


def scrub_tree(src: Path, dst: Path, *, environ: Mapping[str, str] | None = None) -> ScrubReport:
    """Scrub every recording under `src` into the same relative path under `dst` (files there are replaced,
    nothing else is touched). `environ` (default `os.environ`) names the provider keys to remove."""
    per_source: dict[str, Pseudonyms] = {}
    secrets = _secret_values(os.environ if environ is None else environ)
    written, skipped = 0, []
    for path in sorted(q for q in src.rglob("*") if q.is_file()):
        rel = path.relative_to(src)
        source = rel.parts[0] if len(rel.parts) > 1 else ""
        if rel.name in NEVER_COPIED or not (rel.suffix == ".json" or rel.suffix in TEXT_SUFFIXES):
            skipped.append(str(rel))
            continue
        p = per_source.setdefault(source, Pseudonyms())
        text = _replace_secrets(path.read_text(encoding="utf-8"), secrets, p)
        if rel.name.endswith(".headers.json"):
            out = _dump(_scrub_headers(json.loads(text), p))
        elif rel.suffix == ".json":
            out = _dump(scrub_document(json.loads(text), source, p))
        else:
            out = scrub_text(text, p)
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(out, encoding="utf-8")
        written += 1
    counts = [p.counts() for p in per_source.values()]
    totals = {category: sum(c[category] for c in counts) for category in get_args(Category)}
    return ScrubReport(written, tuple(skipped), MappingProxyType(totals))


def run_fixtures_command(args: argparse.Namespace) -> int:
    """`jobhunter fixtures scrub <in> <out>`: one summary line, counts only (never a scrubbed value)."""
    src, dst = Path(args.input), Path(args.output)
    if not src.is_dir():
        print(f"fixtures scrub: {src} is not a directory")
        return 2
    report = scrub_tree(src, dst)
    counts = " ".join(f"{category}={n}" for category, n in report.pseudonyms.items())
    print(f"fixtures scrub: {report.files} files written, {len(report.skipped)} skipped; replaced {counts}")
    return 0
