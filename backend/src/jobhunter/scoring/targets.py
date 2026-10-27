"""Score targets (spec §7.11, §9.4, §9.7): the SQL that more than one module of the score lane shares.

`RESUMABLE_DRAFT_SQL` (Codex r2 B23, r4 B9, r5 B1) is the one predicate of "this target has a draft to resume
from": a `score_drafts` row on any attempt of the target under the evaluation policy `:policy`, whatever the
attempt's mode (an online attempt's draft or a batch draft). It is an SQL predicate correlated to the outer
alias `t` of `score_targets` and binds `:policy` only:
- `load_context` (the `score_target` subgraph, Task 9) reads it as a column of its own target row, under the
  claim predicate, and resumes the target's attempt chain when it holds;
- Task 16's `ELIGIBLE_SQL` negates it per candidate row, so a target that `load_context` would resume is
  never batched again.

Task 13 adds the target sweeps here.
"""

from typing import Final

RESUMABLE_DRAFT_SQL: Final = (
    "EXISTS (SELECT 1 FROM score_attempts a JOIN score_drafts d ON d.attempt_id = a.id "
    "WHERE a.target_id = t.id AND a.evaluation_policy_id = :policy)"
)
