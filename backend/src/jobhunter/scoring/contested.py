"""The contested null-out (spec §11.10, L4549-4561; plan Task 10, Codex r1 #10, Controller carries T9-a, T9-g,
Ruling P24): what a contested result is built from the last validated draft. Pure: no connection, no clock.

`null_out(draft, validation, last_review)` applies, to the validated `draft` and its `validation` (whose
`failures` are the draft row's exact keys, `score_drafts.deterministic_failures`, never the state's codes:
carry T9-a), with the last valid critic review `last_review` (`None` when no review was valid):
1. every criterion with an unresolved V2/V3 failure or V4 consistency failure becomes `level = null, basis =
   unknown` (its rationale and its quotes kept; the publication repairs drop the failing ones), listed in
   `nulled_criteria` in §11.2's order. "Unresolved" leaves out the failures of the facts the review rejected
   (they are dropped with the fact, item 3; `validator.unresolved_failures`) and the V4 failure
   `not_a_job_ad_with_levels`, which item 3a decides;
2. every fact with failed grounding (a failure of its key: V2's `fact_evidence_missing`, V3's quote failures)
   is not asserted, so its knockout is dropped;
3. every fact the review rejected (`critic_rejected`) is dropped; an `uncertain` or `unconfirmed` fact is kept
   (a badge, no cap: only a structured or critic-confirmed fact binds, `knockouts.binding`);
3a. `not_a_job_ad` is kept only when the review verified it (`not_a_job_ad:ad` confirmed with a grounded
   quote), and then every criterion is null; otherwise the result is a real ad with its criteria as
   validated and the badge "possibly not a job ad" (`badge_possibly_not_a_job_ad`) when the draft said it was
   none;
4. a semantic dispute keeps the validated level (the suggested one stays in `critic_notes`): nothing to do.
Items 5-8 (the repairs, the interval, the drafts kept, never withheld) are `publication.build`'s and the
publish transaction's. The draft and the validation given are never changed.

**Dropping a fact** (`drop_facts`, which the accepted publication uses for its rejected facts too, T9-g): a
single fact is reset to its not-asserted form (`work_mode` and `seniority` `unspecified`, `onsite_only` not
asserted, a `degree` not mandatory with no level, a clearance or a permit not required, no value, no
quote); an item of a list (`required_languages`, `must_haves`, `nice_to_haves`, `certifications`) is removed.
`Dropped.path` maps a path of the validated draft to the published one: the paths under a reset fact or a
removed item are gone, and the later items of a list move up, so the publication repairs and the correction
history name the published places.
"""

import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Final

from jobhunter.llm.schemas.generator import DraftCriterion, GeneratorDraft, JobFacts
from jobhunter.scoring.rubric import LLM_CRITERIA
from jobhunter.scoring.validator import CriticValidation, DraftValidation, fact_places, unresolved_failures
from jobhunter.types import JSONValue

LEVELS_FAILURE: Final = "not_a_job_ad_with_levels"  # V4: decided by item 3a, not item 1
FACT_LISTS: Final = ("required_languages", "must_haves", "nice_to_haves", "certifications")
_REJECTED: Final = "critic_rejected"
# A single fact's not-asserted form (item 2).
_NOT_ASSERTED: Final[Mapping[str, dict[str, JSONValue]]] = MappingProxyType(
    {
        "work_mode": {"value": "unspecified", "evidence": []},
        "onsite_only": {"asserted": False, "evidence": []},
        "remote_region": {"value": "unspecified", "countries": [], "evidence": []},
        "seniority": {"value": "unspecified", "evidence": []},
        "min_years_experience": {"value": None, "evidence": []},
        "industry": {"value": None, "evidence": []},
        "employment_types": {"values": [], "evidence": []},
        "degree": {
            "level": "unspecified",
            "mandatory": False,
            "or_equivalent": False,
            "field": None,
            "evidence": [],
        },
        "security_clearance": {"required": False, "level": None, "evidence": []},
        "work_permit": {"required": False, "evidence": []},
    }
)
_PREFIX: Final = "job_facts."
_LIST_ITEM: Final = re.compile(r"job_facts\.(" + "|".join(FACT_LISTS) + r")\[(\d+)\]")


@dataclass(frozen=True)
class Dropped:
    """The places a publication drops (module docstring): the single facts reset (their paths) and, per list,
    the indices of the items removed (in the validated draft)."""

    reset: frozenset[str] = frozenset()
    removed: Mapping[str, frozenset[int]] = field(default_factory=dict)

    def path(self, path: str) -> str | None:
        """`path` of the validated draft in the published draft, `None` when it is dropped."""
        for prefix in self.reset:
            if path == prefix or path.startswith((f"{prefix}.", f"{prefix}[")):
                return None
        found = _LIST_ITEM.match(path)
        if found is None or found.group(1) not in self.removed:
            return path
        index, removed = int(found.group(2)), self.removed[found.group(1)]
        if index in removed:
            return None
        moved = index - sum(1 for other in removed if other < index)
        return f"job_facts.{found.group(1)}[{moved}]{path[found.end() :]}"


@dataclass(frozen=True)
class Contested:
    """The null-out of a draft (module docstring): the draft to publish, the criteria nulled, the facts
    dropped (their keys), whether `not_a_job_ad` is published, the badge, and the places dropped."""

    draft: GeneratorDraft
    nulled_criteria: list[str]
    dropped_facts: list[str]
    not_a_job_ad: bool
    badge_possibly_not_a_job_ad: bool
    dropped: Dropped = field(default_factory=Dropped)


def rejected_facts(review: CriticValidation | None) -> list[str]:
    """The fact ids `review` rejected (`critic_rejected`), in its order; none without a review."""
    if review is None:
        return []
    return [name for name, status in review.fact_reviews.items() if status == _REJECTED]


def drop_facts(draft: GeneratorDraft, keys: Collection[str]) -> tuple[GeneratorDraft, list[str], Dropped]:
    """`draft` with the facts of `keys` (fact ids or pseudo keys) dropped (module docstring), the keys of the
    facts it dropped, in the fact table's order, and the places dropped. A key that names no fact of the
    draft is ignored."""
    places = [place for place in fact_places(draft.job_facts) if place.key in keys]
    if not places:
        return draft, [], Dropped()
    doc = draft.job_facts.model_dump(mode="json")
    reset: set[str] = set()
    removed: dict[str, set[int]] = {}
    for place in places:
        found = _LIST_ITEM.fullmatch(place.fact_path)
        if found is not None:
            removed.setdefault(found.group(1), set()).add(int(found.group(2)))
            continue
        name = place.fact_path.removeprefix(_PREFIX)
        doc[name] = dict(_NOT_ASSERTED[name])
        reset.add(place.fact_path)
    for name, indices in removed.items():
        items = doc[name]
        if not isinstance(items, list):
            raise TypeError(f"job_facts.{name} is no list")
        doc[name] = [item for i, item in enumerate(items) if i not in indices]
    dropped = Dropped(frozenset(reset), {name: frozenset(indices) for name, indices in removed.items()})
    facts = JobFacts.model_validate(doc)
    return draft.model_copy(update={"job_facts": facts}), [place.key for place in places], dropped


def _nulled(criterion: DraftCriterion) -> DraftCriterion:
    return criterion.model_copy(update={"level": None, "basis": "unknown"})


def null_out(
    draft: GeneratorDraft, validation: DraftValidation, last_review: CriticValidation | None
) -> Contested:
    """The contested null-out of `draft` (module docstring, items 1-4)."""
    verified = draft.not_a_job_ad and last_review is not None and last_review.not_a_job_ad_verified
    rejected = rejected_facts(last_review)
    keys = unresolved_failures([f.key for f in validation.failures], dict.fromkeys(rejected, _REJECTED))
    failing = {f.key for f in validation.failures if f.key in keys and (verified or f.code != LEVELS_FAILURE)}
    nulled: list[str] = [key for key in LLM_CRITERIA if key in failing]
    if verified:  # item 3a: every criterion null
        nulled = [
            key for key in LLM_CRITERIA if key in failing or getattr(draft.criteria, key).level is not None
        ]
    criteria = draft.criteria.model_copy(
        update={key: _nulled(getattr(draft.criteria, key)) for key in (LLM_CRITERIA if verified else nulled)}
    )
    fact_keys = [key for key in dict.fromkeys(keys) if key not in LLM_CRITERIA] + rejected
    dropped_draft, dropped_keys, dropped = drop_facts(draft, fact_keys)
    return Contested(
        draft=dropped_draft.model_copy(update={"criteria": criteria})
        if nulled or verified
        else dropped_draft,
        nulled_criteria=nulled,
        dropped_facts=dropped_keys,
        not_a_job_ad=verified,
        badge_possibly_not_a_job_ad=draft.not_a_job_ad and not verified,
        dropped=dropped,
    )
