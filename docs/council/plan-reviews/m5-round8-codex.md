**Verdict: REJECT**

I reviewed the brief, round-7 review and response, revised plan, binding decisions, relevant specification and research digests, M4 conventions, and supporting code at HEAD `d04bedf`. This was a static review: no network, Docker, sub-agents, or tests; no `.secrets/`, `.fixtures-raw/`, keys, or credentials read; nothing changed.

**Round-7 findings — disposition**

| # | Finding | Resolved? |
|---|---|---|
| 1 | Golden-case activation can invalidate profile-purge eligibility after admission | **Yes, for `set_flags`.** Its first lock now conflicts with A0, and subsequent activation checks the profile fence and purge marker. Both transaction orders are tested (P:472, P:483). |
| 2 | The shared privacy seed requires three mutually exclusive in-flight submissions | **Yes.** The seed accepts one live state; the mapping tests use separate databases with the real constraints enabled (P:446–450, P:452, P:525). |

**New findings**

1. **MAJOR — Delete-everything can erase a batch cancellation before it is sent.** P:383–385, P:393, P:517–525; S:6525, S:6536; `backend/src/jobhunter/scoring/batch_results.py:140–143, 199–207, 820–825` — Task 11 relies on the next poll to send the cancellation recorded by `record_cancel`. Task 16 reuses that helper, then deletes the submission. If deletion finishes before the next poll, the poll finds no row and never calls the provider; processing continues remotely despite the required cancellation attempt. The revised mapping test checks the database mapping and ledger settlement, but not delivery. — Explicitly send the cancellation after its recording transaction commits and before deleting the submission, with the specified three attempts. Preserve restartability across that boundary. Test delete-everything with the poller withheld, including a crash after recording the request.

2. **MAJOR — Task 14’s synthetic-profile eligibility contradicts its promised exclusion.** P:467–471, P:479, P:1015; S:6470–6475; `backend/src/jobhunter/scoring/golden.py:574–576, 615–619, 647–649, 797–813` — Deviation 25 excludes every synthetic profile, but Task 14 excludes only those referenced by active cases. Under its task-local rule, deleting a synthetic profile’s cases makes it eligible. After A0 commits, `seed-adversarial` can acquire `active_config`, reuse that still-unpurged profile without checking its fence, and recreate an active case. A2 excludes the active case while redacting its profile, leaving an unusable case. Merely acquiring `active_config` does not prevent this later transaction. — Put the unconditional `NOT synthetic_eval` eligibility predicate directly in Task 14, matching Deviation 25, and test synthetic versions with inactive cases and with no remaining cases.

### Challenges to rulings

None.