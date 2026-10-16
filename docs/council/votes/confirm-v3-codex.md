**Final vote: BLOCK**

- **B checkpoint fencing — RESOLVED.** B3 gives each attempt an isolated checkpoint thread, reconstructs recovery from durable receipts, and protects domain writes with the attempt CAS. A zombie’s checkpoint writes remain confined to its old thread. G2 also prevents scoring recovery from resuming old checkpoints across process restarts.

- **Ambiguous batch-submission recovery — NOT RESOLVED.** A7 replaces unavailable metadata matching with completed-result `custom_id` matching, resolving that part. However, it then treats “no match after 25 hours” as proof of non-submission, releases the reservation, and makes targets eligible for another attempt.

  **Concrete failure:** the provider accepts and bills the batch, but the submit response is lost. Results cannot be downloaded before the 25-hour deadline because of a reconciliation outage. No candidate has been confirmed, so A7 releases the original reservation and requeues the targets. A subsequent attempt incurs another charge while the original charge is absent from budget accounting, potentially exceeding the cap. Batch expiry does not prove that no requests executed or were billed.

  **Required correction:** keep unresolved submissions quarantined with spend reserved until positive evidence permits settlement. The 25-hour deadline should trigger an alert, not `not_submitted`; update M1’s corresponding invariant. This also restores consistency with E3’s prohibition on refunds without positive evidence.

**New v3 blocking defects:** the unsafe timeout transition above is newly introduced within the still-open batch blocker. No additional independent concrete blocker identified.