"""The alert catalogue as data (spec §17.6 L5631-5698; plan M5 Task 2, O-ops §2.2, Deviations 7 and 8).

`CATALOGUE` holds an entry for every alert code the code base raises, each a `CatalogueEntry`:

- `severity`, the severity the code is raised with, and `escalates_to`, the higher one the same alert takes
  when its condition worsens: §17.6's `warn / critical` rows, `backup.stale` (36 h / 72 h) and
  `system.disk_pressure` (80 % / 90 %). `alerts.raise_alert` escalates an open alert raised again with a
  higher severity, whatever its code; the severities rank in `SEVERITIES`' order (`info < warn < critical`).
- `auto_resolves`: the code resolves itself when its condition clears. §17.6's "Auto-resolves" column says
  how; "manual" and "manual acknowledgement" mean it does not. A manual alert is resolved by the user
  (`POST /api/alerts/{id}/resolve`, `lists.resolve_manual`); one in `RESOLVE_ON_ACK` also by its
  acknowledgement (`llm.reservation_bound_violated`: §17.6 "manual acknowledgement", so its next occurrence
  opens a new row that pauses the score lane and is pushed again). `embedder.checksum_mismatch`, manual in
  §17.6, resolves itself on a later `checksum_ok: true` (Deviation 8).
- `pushed`: the alert dispatcher sends it to ntfy, at `push_priority(severity)` (L5631: `warn` 3, `critical`
  5, `info` in-app only), unless its code is in `NEVER_PUSHED`: `notify.delivery_failed` ("warn (in-app
  only)", L5695) reports that the push channel failed, so it is never sent through it.

Beyond §17.6's 63 rows the catalogue holds the eight codes the code raised before M5 without a row (O-ops
§2.2): `llm.unexpected_stop_reason` (M2 Deviation 6), `profile.extract_generation_mismatch` (M2 Carry-forward
3), `dedup.ambiguous_grouping` (§8), `quota.serpapi_renewal_stale` (ruling T7-b), `reconciler.failed`,
`retention.failed`, `api.unhandled_exception` (§17.1) and `auth.totp_key_mismatch` (ruling T16-c); and the
new `docparse.leftover` (Deviation 7). `is_manual` treats a code the catalogue lacks as manual, so such an
alert can always be resolved; `tests/unit/test_alert_catalogue.py` keeps every raised code catalogued.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Literal

Severity = Literal["info", "warn", "critical"]
# In rank order: an escalation moves right, never left.
SEVERITIES: Final[tuple[Severity, ...]] = ("info", "warn", "critical")

Resolution = Literal["auto", "manual"]
AUTO: Final[Resolution] = "auto"
MANUAL: Final[Resolution] = "manual"

NEVER_PUSHED: Final = frozenset({"notify.delivery_failed"})
RESOLVE_ON_ACK: Final = frozenset({"llm.reservation_bound_violated"})


@dataclass(frozen=True)
class CatalogueEntry:
    """An alert code as the catalogue states it (module docstring)."""

    code: str
    severity: Severity
    escalates_to: Severity | None
    auto_resolves: bool
    pushed: bool


def push_priority(severity: Severity) -> Literal[3, 5] | None:
    """The ntfy priority of an alert of `severity`: `warn` 3, `critical` 5, `None` for `info`, in-app only."""
    match severity:
        case "critical":
            return 5
        case "warn":
            return 3
        case "info":
            return None


def _entry(
    code: str, severity: Severity, resolution: Resolution, *, escalates_to: Severity | None = None
) -> CatalogueEntry:
    return CatalogueEntry(
        code=code,
        severity=severity,
        escalates_to=escalates_to,
        auto_resolves=resolution == AUTO,
        pushed=push_priority(severity) is not None and code not in NEVER_PUSHED,
    )


def _index(entries: Iterable[CatalogueEntry]) -> Mapping[str, CatalogueEntry]:
    index: dict[str, CatalogueEntry] = {}
    for entry in entries:
        if entry.code in index:
            raise ValueError(f"the alert catalogue lists {entry.code} twice")
        index[entry.code] = entry
    return MappingProxyType(index)


# §17.6's rows in its order (L5636-5698): the code, its severity, how it resolves.
_SPEC: Final = (
    _entry("run.failed", "critical", AUTO),
    _entry("run.failed_invariant", "critical", MANUAL),
    _entry("run.partial", "warn", AUTO),
    _entry("run.resume_exhausted", "critical", MANUAL),
    _entry("run.orphaned_queue", "warn", AUTO),
    _entry("schedule.stale_data", "critical", AUTO),
    _entry("worker.heartbeat_stale", "critical", AUTO),
    _entry("embedder.unavailable", "warn", AUTO),
    _entry("embedder.checksum_mismatch", "critical", AUTO),  # §17.6 says manual (Deviation 8)
    _entry("source.breaker_open", "warn", AUTO),
    _entry("source.auth_failed", "critical", AUTO),
    _entry("source.contract_broken", "critical", AUTO),
    _entry("source.not_found", "warn", AUTO),
    _entry("ats.mass_disappearance", "warn", AUTO),
    _entry("quota.unaccounted_usage", "warn", MANUAL),
    _entry("quota.serpapi_foreign_usage", "warn", AUTO),
    _entry("quota.exhausted", "warn", AUTO),
    _entry("quota.limit_increased", "info", MANUAL),
    _entry("quota.fallback_active", "info", AUTO),
    _entry("quota.ambiguous_attempt", "warn", MANUAL),
    _entry("quota.limit_breach", "critical", MANUAL),
    _entry("coverage.too_broad", "info", AUTO),
    _entry("coverage.gap_abandoned", "warn", MANUAL),
    _entry("llm.cap_80pct", "warn", AUTO),
    _entry("llm.cap_reached", "critical", AUTO),
    _entry("llm.budget_counter_drift", "critical", AUTO),
    _entry("llm.reservation_bound_violated", "critical", MANUAL),  # resolved by its acknowledgement
    _entry("llm.auth_failed", "critical", AUTO),  # "successful call, or manual acknowledgement"
    _entry("llm.model_mismatch", "critical", MANUAL),
    _entry("llm.cache_miss_streak", "warn", AUTO),
    _entry("llm.refusal_rate", "warn", AUTO),
    _entry("scoring.stuck_target_repaired", "warn", AUTO),
    _entry("scoring.publication_invalid", "critical", MANUAL),
    _entry("scoring.critic_rate_high", "warn", AUTO),
    _entry("score_cycle.orphaned", "warn", AUTO),
    _entry("score_cycle.failed", "critical", AUTO),
    _entry("operation.failed", "warn", MANUAL),
    _entry("scoring.canary_drift", "warn", AUTO),
    _entry("scoring.feedback_disagreement", "warn", AUTO),
    _entry("scoring.distribution_shift", "info", AUTO),
    _entry("admission.exploration_hits", "warn", AUTO),
    _entry("admission.high_sim_expired", "warn", AUTO),
    _entry("admission.backlog_drain", "info", AUTO),
    _entry("batch.submit_ambiguous_unresolved", "critical", AUTO),
    _entry("batch.results_unreadable", "critical", MANUAL),
    _entry("batch.item_unprocessable", "critical", MANUAL),
    _entry("batch.retrieve_failing", "critical", AUTO),
    _entry("scope.apply_failed", "critical", AUTO),
    _entry("batch.custom_id_anomaly", "critical", MANUAL),
    _entry("batch.items_missing", "critical", MANUAL),
    _entry("dedup.identity_conflict", "info", AUTO),
    _entry("dedup.field_conflict", "info", AUTO),  # "— (info log)": resolved when the postings agree again
    _entry("auth.lockout", "critical", AUTO),
    _entry("backup.failed", "critical", AUTO),
    _entry("backup.stale", "warn", AUTO, escalates_to="critical"),
    _entry("backup.restore_test_failed", "critical", AUTO),
    _entry("system.disk_pressure", "warn", AUTO, escalates_to="critical"),
    _entry("system.table_growth", "info", AUTO),
    _entry("deadman.update_failed", "warn", AUTO),
    _entry("notify.delivery_failed", "warn", AUTO),  # in-app only: `NEVER_PUSHED`
    _entry("eval.gate_failed", "warn", MANUAL),
    _entry("privacy.purge_failed", "critical", AUTO),
    _entry("privacy.manifest_unshipped", "critical", AUTO),
)
# The codes raised without a §17.6 row (module docstring).
_BEYOND_SPEC: Final = (
    _entry("llm.unexpected_stop_reason", "warn", MANUAL),
    _entry("profile.extract_generation_mismatch", "warn", MANUAL),
    _entry("dedup.ambiguous_grouping", "info", AUTO),
    _entry("quota.serpapi_renewal_stale", "warn", AUTO),
    _entry("reconciler.failed", "warn", AUTO),
    _entry("retention.failed", "warn", AUTO),
    _entry("api.unhandled_exception", "warn", MANUAL),
    _entry("auth.totp_key_mismatch", "critical", MANUAL),
    _entry("docparse.leftover", "warn", AUTO),
)

CATALOGUE: Final[Mapping[str, CatalogueEntry]] = _index((*_SPEC, *_BEYOND_SPEC))


def is_manual(code: str) -> bool:
    """Whether an alert of `code` is resolved by the user rather than by its condition clearing; a code the
    catalogue lacks is manual."""
    entry = CATALOGUE.get(code)
    return entry is None or not entry.auto_resolves
