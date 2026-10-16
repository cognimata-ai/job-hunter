**Final vote: CONFIRM**

- **Prior blocker — RESOLVED.** A7 now keeps incomplete reconciliation quarantined with spend reserved. The 25-hour deadline raises an alert and cannot trigger `not_submitted`, release the reservation, or automatically requeue targets. This closes the previously identified outage-at-deadline failure.
- **Settlement and manual release:** Automatic release requires exhaustive, successful reconciliation with no matching `custom_id`s. Explicit user-requested re-scoring retains the original reservation as counted `ambiguous` spend.
- **M1 — consistent.** Its amended invariant requires an alert for unresolved submissions and explicitly prohibits timeout settlement.

**No new concrete blocking defect identified in the reviewed amendment.**