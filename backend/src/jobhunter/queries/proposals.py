"""Query proposals (spec §5.5.1, §11.12; plan Task 20, Deviation 20): the semantic check of a proposed set,
the request that starts a proposal, and the writes of its row.

**Location words** (`location_words`). §11.12 forbids a location word in a query text, because a cell adds the
location (§5.5.2). A text holds one when a location word, folded (`normalize.text.fold`) and split into its
words, is a run of whole words of the folded text: `Software Engineer Berlin` and `DevOps Engineer
Home-Office` do, `Hallenmanager` (which contains `halle`) and `Java Entwickler` do not. The words are:
- the words of the scope's location labels, each on its own, without the particles of German and English place
  names (`LABEL_PARTICLES`: `am`, `an`, `der`, …) and the generic words of German place names
  (`LABEL_STOPWORDS`: `bad`, `neu`, `oder`, `weil`, `main`), which would otherwise drop any query that uses
  them (`Frankfurt am Main` gives `frankfurt`, `Bad Homburg` gives `homburg`);
- the folded name of each gazetteer place with at least 100,000 inhabitants (`LARGE_PLACE_POPULATION`), and
  each of its other folded spellings but those that are common words (`ALT_SPELLING_STOPWORDS`: `gerne` for
  Herne, `monster` for Münster, …, compared in words, so `kil'` is `kil`), each whole: `munchen`, `munich`,
  `frankfurt am main`, `frankfurt`, `mitte`;
- `LOCATION_WORDS_EXTRA`: the work-mode and country words and the federal states (its comment lists them).

**The check** (`validate_proposal`), in two levels (Deviation 20). An item's text and rationale are trimmed
first (the trimmed text is what a query is created with, `authoring.create_query`). An item is dropped by the
first rule it breaks, with that rule's code, in this order: `text_length` (not 2 to 120 characters),
`location_word`, `rationale_length` (over 300 characters), `duplicate` (its folded text equals that of an item
kept before it; `fold` also collapses the spacing, so `Größe` and `GRÖSSE` collide). A duplicate of a dropped
item is kept: it is the first of its text that passes. Then the set: a kept set over 16 items is cut to its
first 16 in model order, with `truncated`; a set that then has fewer than 6 items or none flagged
`exploration` is `([], ["set_invalid"])`, which makes the one repair due (`llm_outcome.after_invalid`). So a
published set always meets §11.12, even when its only exploration item came after the 16th. Pure: the
gazetteer is passed in, and the words of its large places are computed once per gazetteer.

**The request** (`request_proposal`, in the caller's transaction). Its caller holds `active_config` (`FOR
SHARE` at least, the lock schedule's step 2) from before it read which version to propose for, and passes the
active version or the one its transaction activates (M5 Task 14, Deviation 54): the confirm and activation
(`profile.activation`, `FOR UPDATE`) and the proposal route (`api.routes.profile`, `FOR SHARE`). A profile
purge's admission holds `active_config FOR UPDATE` and cancels the version's live operations, and a version
under its fence can be neither active nor activated (409 `profile_purge_in_progress`), so no proposal
operation of a purgeable version is ever created after its admission. The request takes the profile version's
row `FOR NO KEY UPDATE`, which serialises two requests for one version without blocking the foreign-key
checks of other writers, then reads (after that lock) whether the version is purged or under an unreleased
`profile` or `global` fence (`privacy.fences.version_fenced`): either is an `InvariantViolation`, nothing
written, the guard of a future caller that forgets the rule. Then a `running` proposal of that version is
returned as it is (`created=False`), with its live operation (recreated if none is live, so a running
proposal is never left without one); otherwise a `query_proposals` row in `running` and its
`propose_queries` operation (`KIND`), payload
`{"proposal_id": id, "scope_version_id": <the active scope version>}` and dedup key `propose_queries:<id>`,
with its job deferred by `defer`. The scope version is pinned with the request: every execution of the
operation renders its prompt from that version's location labels and checks every output, a receipt's
included, against them, so a scope saved between a crash and its reclaim never changes whether a paid answer
is usable. The handler of that operation and its failure hook are `profile.operations`' (this module registers
nothing).

**The row** (`lock_proposal`, `publish`, `fail`, `end_obsolete`): every write runs after the owner row of the
proposal's operation (§9.1: the owner row, then the domain rows). `publish` moves `running → ready` with
`items = {"queries": [<kept items>]}` and the call's id; `fail` moves `running → failed`
(`query_proposals` has no reason column: the code is the operation's, which `FAILURE_CODE_SQL` reads back for
the api: the `last_error_code` of the proposal's newest operation while it is `failed`); `end_obsolete` ends
an operation whose proposal is no longer `running` as `succeeded`, leaving the row. Logs carry ids and codes
only, never a text.
"""

import functools
import re
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from typing import Final, Literal

from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.db.sql import q
from jobhunter.db.transitions import transition
from jobhunter.errors import InvariantViolation
from jobhunter.geo.gazetteer import Gazetteer
from jobhunter.llm.schemas.queries import QueryProposalOut
from jobhunter.normalize.text import fold
from jobhunter.observability.logging import get_logger
from jobhunter.privacy.fences import version_fenced
from jobhunter.types import JSONValue
from jobhunter.worker.operations import OperationContext, create_operation
from jobhunter.worker.tasks import Deferrer

KIND: Final = "propose_queries"
RUNNING: Final = "running"
READY: Final = "ready"
FAILED: Final = "failed"

# §11.12's semantic limits.
TEXT_CHARS: Final[tuple[int, int]] = (2, 120)
RATIONALE_MAX: Final = 300
SET_SIZE: Final[tuple[int, int]] = (6, 16)
LARGE_PLACE_POPULATION: Final = 100_000

# The issue codes (module docstring).
TEXT_LENGTH: Final = "text_length"
LOCATION_WORD: Final = "location_word"
RATIONALE_LENGTH: Final = "rationale_length"
DUPLICATE: Final = "duplicate"
SET_INVALID: Final = "set_invalid"
TRUNCATED: Final = "truncated"

# Location words beyond the scope labels and the gazetteer, folded (`fold`) as every location word is:
# - the work-mode and country words §11.12 means: `remote`, `homeoffice`, `home office`, `deutschland`,
#   `germany`, `bundesweit`, `deutschlandweit`, `europa`, `europe`;
# - the 16 federal states with their common short forms: each German name (with `ue` for `ü` where a name has
#   one, a common spelling without the umlaut), its English name where it differs, and the short forms `nrw`,
#   `bawu` (BaWü), `rlp`, `meck-pomm` and `meckpomm`. The two-letter codes (`BW`, `MV`, `SH`, the ISO codes
#   `BY`, `BE`, `HE`, …) are left out: they are words or other abbreviations too (`by`, `he`; `BW` is also the
#   Bundeswehr), and a false hit drops a good query unseen, where a miss stays visible in the review.
# Berlin, Hamburg and Bremen are states and gazetteer cities; they are listed here as states.
LOCATION_WORDS_EXTRA: Final[frozenset[str]] = frozenset(
    {
        "remote",
        "homeoffice",
        "home office",
        "deutschland",
        "germany",
        "bundesweit",
        "deutschlandweit",
        "europa",
        "europe",
        "baden-wurttemberg",
        "baden-wuerttemberg",
        "bawu",
        "bayern",
        "bavaria",
        "berlin",
        "brandenburg",
        "bremen",
        "hamburg",
        "hessen",
        "hesse",
        "mecklenburg-vorpommern",
        "mecklenburg-western pomerania",
        "meck-pomm",
        "meckpomm",
        "niedersachsen",
        "lower saxony",
        "nordrhein-westfalen",
        "north rhine-westphalia",
        "nrw",
        "rheinland-pfalz",
        "rhineland-palatinate",
        "rlp",
        "saarland",
        "sachsen",
        "saxony",
        "sachsen-anhalt",
        "saxony-anhalt",
        "schleswig-holstein",
        "thuringen",
        "thueringen",
        "thuringia",
    }
)
# The particles of German and English place names, folded: never a location word on their own.
LABEL_PARTICLES: Final[frozenset[str]] = frozenset(
    {
        "a",
        "am",
        "an",
        "auf",
        "bei",
        "d",
        "de",
        "dem",
        "den",
        "der",
        "des",
        "die",
        "das",
        "im",
        "in",
        "ob",
        "of",
        "on",
        "the",
        "und",
        "upon",
        "vor",
        "zu",
        "zum",
        "zur",
    }
)
# Generic words of German place names, folded: a label's word that is one of them is no location word, since
# it is a common word too (`oder` is "or") or names a whole class of places (`Bad …`, `Neu-…`, `Weil am …`,
# the river of `Frankfurt am Main`). The place's own words (`homburg`, `isenburg`, `frankfurt`) stay.
LABEL_STOPWORDS: Final[frozenset[str]] = frozenset({"bad", "neu", "oder", "weil", "main"})
# Other spellings of the large places that are common words of German or English, or of a job query, folded:
# never a location word, though the place's own name is (`herne`, `munster`, `kiel`, `bonn`, `essen`, `halle`,
# `neuss`, `moers`, `koln`). Only the listed spelling is left out: `colonia agrippina` and the like stay.
# `gerne` (Herne), `monster` and `minster` (Münster), `kilo`, `kil` and `kyle` (Kiel), `bon` (Bonn), `esse`
# (Essen), `galle` (Halle), `nuss` (Neuss), `mers` and `mors` (Moers), `mite` (Mitte), `colonia` (Köln).
ALT_SPELLING_STOPWORDS: Final[frozenset[str]] = frozenset(
    {
        "gerne",
        "monster",
        "kilo",
        "kil",
        "kyle",
        "bon",
        "esse",
        "galle",
        "nuss",
        "mers",
        "mors",
        "mite",
        "minster",
        "colonia",
    }
)
_WORD: Final = re.compile(r"[^\W_]+")

log = get_logger("jobhunter.queries.proposals")


@dataclass(frozen=True)
class ProposedQuery:
    """A proposal item that passed the check, trimmed; `asdict` is its stored form in `items.queries`."""

    text: str
    lang: Literal["de", "en"]
    target_title: str
    exploration: bool
    rationale: str


def words(text: str) -> tuple[str, ...]:
    """The words of `fold(text)`: its runs of letters and digits, in order."""
    return tuple(_WORD.findall(fold(text)))


@functools.cache
def _large_place_words(gazetteer: Gazetteer) -> frozenset[str]:
    """The names of the large places, and their other spellings but `ALT_SPELLING_STOPWORDS`; a stop-listed
    spelling that is a large place's own name stays. A spelling is compared with the stop list as the match
    sees both, split into `words`, so `kil'` (Kiel), whose words are those of the stop-listed `kil`, is left
    out as well (final review FR-8)."""
    places = gazetteer.places_of_population(LARGE_PLACE_POPULATION)
    names = {place.folded for place in places}
    stopped = {words(stopword) for stopword in ALT_SPELLING_STOPWORDS}
    spellings = {
        spelling for place in places for spelling in place.alt_folded if words(spelling) not in stopped
    }
    return frozenset(names | spellings)


def location_words(scope_labels: Sequence[str], gazetteer: Gazetteer) -> frozenset[str]:
    """The location words of a proposal for a scope with `scope_labels` (module docstring)."""
    left_out = LABEL_PARTICLES | LABEL_STOPWORDS
    label_words = {word for label in scope_labels for word in words(label) if word not in left_out}
    return frozenset(label_words) | _large_place_words(gazetteer) | LOCATION_WORDS_EXTRA


def _phrases(location: Iterable[str]) -> frozenset[tuple[str, ...]]:
    return frozenset(phrase for phrase in map(words, location) if phrase)


def _holds_phrase(text_words: tuple[str, ...], phrases: frozenset[tuple[str, ...]], longest: int) -> bool:
    """Whether a run of whole words of `text_words` is one of `phrases`."""
    for size in range(1, min(longest, len(text_words)) + 1):
        for start in range(len(text_words) - size + 1):
            if text_words[start : start + size] in phrases:
                return True
    return False


def validate_proposal(
    out: QueryProposalOut, *, location_words: frozenset[str]
) -> tuple[list[ProposedQuery], list[str]]:
    """The kept items of `out` and the issue codes, or `([], ["set_invalid"])` (module docstring)."""
    phrases = _phrases(location_words)
    longest = max(map(len, phrases), default=0)
    low, high = TEXT_CHARS
    kept: list[ProposedQuery] = []
    issues: list[str] = []
    seen: set[str] = set()
    for item in out.queries:
        text, rationale = item.text.strip(), item.rationale.strip()
        key = fold(text)
        code = None
        if not low <= len(text) <= high:
            code = TEXT_LENGTH
        elif _holds_phrase(words(text), phrases, longest):
            code = LOCATION_WORD
        elif len(rationale) > RATIONALE_MAX:
            code = RATIONALE_LENGTH
        elif key in seen:
            code = DUPLICATE
        if code is not None:
            issues.append(code)
            continue
        seen.add(key)
        kept.append(ProposedQuery(text, item.lang, item.target_title, item.exploration, rationale))
    fewest, most = SET_SIZE
    if len(kept) > most:
        kept = kept[:most]
        issues.append(TRUNCATED)
    if len(kept) < fewest or not any(query.exploration for query in kept):
        return [], [SET_INVALID]
    return kept, issues


# --- the request and the row --------------------------------------------------------------------------------

_LOCK_VERSION: Final = q("SELECT purged_at FROM profile_versions WHERE id = :pv FOR NO KEY UPDATE")
_ACTIVE_SCOPE: Final = q("SELECT scope_version_id FROM active_config WHERE id = 1")
_RUNNING: Final = q(
    "SELECT id FROM query_proposals WHERE profile_version_id = :pv AND status = 'running' ORDER BY id LIMIT 1"
)
_INSERT: Final = q(
    "INSERT INTO query_proposals (profile_version_id, status) VALUES (:pv, 'running') RETURNING id"
)
_LOCK_PROPOSAL: Final = q("SELECT status, profile_version_id FROM query_proposals WHERE id = :p FOR UPDATE")
_TABLE: Final = "query_proposals"


def dedup_key(proposal_id: int) -> str:
    return f"{KIND}:{proposal_id}"


# A failed proposal's code (module docstring, the row), as a column of a statement over `query_proposals p`:
# its newest operation's, by id under `dedup_key`'s key (`operations_dedup_newest` serves the newest), `NULL`
# for a proposal in any other status.
FAILURE_CODE_SQL: Final = (
    "CASE WHEN p.status = 'failed' THEN (SELECT o.last_error_code FROM operations o "  # noqa: S608 (constants)
    f"WHERE o.kind = '{KIND}' AND o.dedup_key = '{KIND}:' || p.id ORDER BY o.id DESC LIMIT 1) END"
)


async def _operation(conn: AsyncConnection, proposal_id: int, defer: Deferrer | None) -> tuple[int, bool]:
    """The live operation of the proposal, created with the active scope version pinned when there is none."""
    scope_version_id = int((await conn.execute(_ACTIVE_SCOPE)).scalar_one())
    payload: dict[str, JSONValue] = {"proposal_id": proposal_id, "scope_version_id": scope_version_id}
    return await create_operation(
        conn, kind=KIND, payload=payload, dedup_key=dedup_key(proposal_id), defer=defer
    )


async def request_proposal(
    conn: AsyncConnection, profile_version_id: int, *, defer: Deferrer | None
) -> tuple[int, int, bool]:
    """`(proposal_id, operation_id, created)`: the running proposal of the version, or a new one with its
    operation (module docstring). The caller holds `active_config` and passes the active version or the one
    it activates. `LookupError` for a version that does not exist, `InvariantViolation` for one purged or
    fenced."""
    version = (await conn.execute(_LOCK_VERSION, {"pv": profile_version_id})).first()
    if version is None:
        raise LookupError(f"profile version {profile_version_id} does not exist")
    if version.purged_at is not None or await version_fenced(conn, profile_version_id):
        raise InvariantViolation(
            f"profile version {profile_version_id} is purged or under a purge fence: no proposal is requested"
        )
    running = (await conn.execute(_RUNNING, {"pv": profile_version_id})).scalar_one_or_none()
    if running is not None:
        operation_id, recreated = await _operation(conn, int(running), defer)
        if recreated:
            log.warning(
                "query_proposal_operation_recreated", proposal_id=int(running), operation_id=operation_id
            )
        return int(running), operation_id, False
    proposal_id = int((await conn.execute(_INSERT, {"pv": profile_version_id})).scalar_one())
    operation_id, created = await _operation(conn, proposal_id, defer)
    if not created:
        raise InvariantViolation(f"query proposal {proposal_id}: a new proposal met a live operation")
    return proposal_id, operation_id, True


@dataclass(frozen=True)
class ProposalRow:
    status: str
    profile_version_id: int


async def lock_proposal(conn: AsyncConnection, proposal_id: int) -> ProposalRow | None:
    """The proposal's status and version, its row held `FOR UPDATE`; `None` when it is gone."""
    row = (await conn.execute(_LOCK_PROPOSAL, {"p": proposal_id})).first()
    return None if row is None else ProposalRow(str(row.status), int(row.profile_version_id))


async def publish(
    conn: AsyncConnection, proposal_id: int, kept: Sequence[ProposedQuery], llm_call_id: int
) -> None:
    """`running → ready` with the kept items and the call they came from (`TransitionConflict` otherwise)."""
    items = {"queries": [asdict(query) for query in kept]}
    await transition(
        conn, _TABLE, proposal_id, [RUNNING], READY, set_={"items": items, "llm_call_id": llm_call_id}
    )


async def fail(conn: AsyncConnection, proposal_id: int) -> None:
    """`running → failed` (`TransitionConflict` otherwise)."""
    await transition(conn, _TABLE, proposal_id, [RUNNING], FAILED)


async def end_obsolete(
    conn: AsyncConnection, op: OperationContext, proposal_id: int, status: str | None
) -> None:
    """End the operation `succeeded`: its proposal (in `status`, or gone) is no longer `running`."""
    await op.finish_in(conn, status="succeeded")
    log.info(
        "query_proposal_operation_obsolete",
        operation_id=op.claim.operation_id,
        proposal_id=proposal_id,
        status=status,
    )
