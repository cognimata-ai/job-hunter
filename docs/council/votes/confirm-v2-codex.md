**1. My objections and MISSING items**

| Item | Verdict | Assessment |
|---|---|---|
| **A — Spend accounting** | **RESOLVED** | A4 specifies per-attempt settlement, retained ambiguous charges, fresh outer-retry reservations, and model/cache-write pricing. |
| **B — Checkpoint fencing** | **NOT RESOLVED** | The singleton lock does not fence checkpoint writes after its connection is lost; see below. |
| **D — Coverage gaps** | **RESOLVED** | D2 separates attempted from verified coverage, preserves gaps, and prevents unjustified watermark advancement. |
| **E — Fractional-credit starvation** | **RESOLVED** | E4 preserves fractional accrual and sets capacity to `max(1, 2 × fair_share)`. |
| **F — Probable duplicates** | **RESOLVED** | F4 binds results to the representative revision, maintains deferred member targets, evaluates filters across members, and activates independent scoring on splits/conflicts. |
| **G — Detached scoring ownership** | **RESOLVED** | G2 defines atomic claims, owner/attempt identifiers, heartbeats, bounded reclamation, and publication CAS; M references claim heartbeats. |
| **H — Invalid contested ratings** | **RESOLVED** | Every revision is validated; unresolved evidence failures produce null ratings and recomputed intervals, with rejected drafts separate. |
| **J — Working-language policy** | **RESOLVED** | J explicitly separates advertisement language, candidate proficiency, and hard/soft mandatory-language policy. |
| **O — Dependency-aware retention** | **RESOLVED** | Referenced profile JSON survives retirement; eventual purges cancel dependent work, prevent late publication, and remove dependent artifacts. Equivalent handling covers job revisions. |
| **MISSING 1 — Durable batches** | **NOT RESOLVED** | Durable records and item reconciliation are present, but ambiguous-submit recovery relies on unavailable matching data; see below. |
| **MISSING 2 — Rescoring triggers** | **RESOLVED** | G8 covers active-tuple/material-revision changes and reconciliation; H9 handles scope changes deterministically. |
| **MISSING 3 — Finite deadlines** | **RESOLVED** | A2 provides connect/read-idle limits and overall node deadlines; A4 retains ambiguous spend. |
| **MISSING 4 — Canonical fields** | **RESOLVED** | F6 specifies deterministic precedence, provenance, full-description preservation, and salary semantics. |

**B: remaining failure and minimal fix.** V2 states:

> “This fences checkpoint writes as well as domain writes.”

The advisory-lock session can be disconnected while the old worker remains alive and its separate checkpointer pool remains usable. A replacement acquires the released lock and resumes; the old invocation subsequently writes checkpoints. Domain CAS cannot reject those checkpoint writes.

Minimal fix: transactionally fence **every checkpoint mutation** against the current ownership epoch, or give each attempt an isolated checkpoint namespace initialized from the last committed checkpoint, with an ownership-checked authoritative pointer. Retain the singleton lock and cancellation rules as additional protection.

**MISSING 1: remaining failure and minimal fix.** A7 states:

> “An ambiguous submit is resolved by listing batches and matching metadata, never by blind resubmission.”

The documented batch-list response exposes neither the submitted request hash nor caller correlation metadata/custom IDs. Two submissions with equal counts and nearby timestamps cannot be reliably distinguished after a lost submit response. [Anthropic batch-list reference](https://platform.claude.com/docs/en/api/messages/batches/list)

Minimal fix: retain ambiguous submissions without resubmission; reconcile candidate batches through their completed results using the persisted target/attempt `custom_id` set. Keep unresolved cases explicitly quarantined with spend reserved.

**2. New blocking defects**

**Yes:** A7’s newly specified metadata-matching recovery is not implementable as written, as described under MISSING 1. B remains the original fencing defect. No additional new blocker identified.

**3. Final vote: BLOCK**

Blocking items: **B checkpoint fencing** and **MISSING 1 ambiguous batch-submission recovery**.