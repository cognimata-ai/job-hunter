# M5 final whole-branch review — Codex GPT-6 Astra

### Verdict

**REQUEST CHANGES.** Two findings block approval: restore can resurrect a deletion already confirmed offsite, and the Adzuna purge retains source-derived scores in feedback snapshots. The shared purge protocol otherwise preserves the intended file/hash/delete ordering and transactional fence release. I also found a minor dead-man disable race. The conclusions below distinguish code inspection and offline probes from the production and browser drills that this review was prohibited from running.

### Focus questions 1–8

1. **Purge protocol end to end**

   Source purge disables the source and establishes its fence at admission; profile purge cancels outstanding proposals and establishes its profile fence; golden-case deletion uses its pending-deletion record and running-evaluation exclusion; delete-everything invalidates the bounded sessions during completion. See [source_purge.py:437](/home/user/job-hunter/backend/src/jobhunter/privacy/kinds/source_purge.py:437), [profile_purge.py:220](/home/user/job-hunter/backend/src/jobhunter/privacy/kinds/profile_purge.py:220), [golden_case.py:85](/home/user/job-hunter/backend/src/jobhunter/privacy/kinds/golden_case.py:85), and [delete_everything.py:934](/home/user/job-hunter/backend/src/jobhunter/privacy/kinds/delete_everything.py:934).

   I found no destructive phase that precedes the committed manifest file and hash: `_admit` writes or adopts the file, commits the content/hash, then permits phase B. Fence release, postcondition checks and completion share one transaction; a failed check rolls release back. Failed purges can deliberately retain fences indefinitely pending intervention, and the reconciler exposes/requeues recoverable stranded work. This is fail-closed behaviour, not premature release. See [purge.py:624](/home/user/job-hunter/backend/src/jobhunter/privacy/purge.py:624), [purge.py:685](/home/user/job-hunter/backend/src/jobhunter/privacy/purge.py:685), [purge.py:716](/home/user/job-hunter/backend/src/jobhunter/privacy/purge.py:716), and [reconcilers.py:159](/home/user/job-hunter/backend/src/jobhunter/privacy/reconcilers.py:159).

   In-batch deletion records positive evidence for subsequently returned results. Export publication rechecks purge state; target and embedding writers check fences under their job locks. I found no additional above-watermark writer escape in those paths. See [deletion_graph.py:320](/home/user/job-hunter/backend/src/jobhunter/privacy/deletion_graph.py:320), [batch_results.py:376](/home/user/job-hunter/backend/src/jobhunter/scoring/batch_results.py:376), [export.py:724](/home/user/job-hunter/backend/src/jobhunter/privacy/export.py:724), [targets.py:1169](/home/user/job-hunter/backend/src/jobhunter/scoring/targets.py:1169), and [embeddings.py:392](/home/user/job-hunter/backend/src/jobhunter/embeddings.py:392).

   The end-to-end guarantees nevertheless fail at **shipment/restore selection (C1)** and **source-purge coverage (I1)** below.

2. **Restore and replay**

   Identity advancement considers stored bounds and explicitly named IDs, raises sequences above both those bounds and existing rows, and does not lower them. Migration reconstructs missing recorded files before replay. Replay processes destructive operations before the combined rebuild and transactional release. These address the intended C37/C39 cases. See [identities.py:109](/home/user/job-hunter/backend/src/jobhunter/privacy/identities.py:109), [identities.py:154](/home/user/job-hunter/backend/src/jobhunter/privacy/identities.py:154), [migrate.py:190](/home/user/job-hunter/backend/src/jobhunter/db/migrate.py:190), and [replay.py:627](/home/user/job-hunter/backend/src/jobhunter/privacy/replay.py:627).

   A never-shipped manifest recoverable from the dump can be reconstructed/completed. A manifest absent from both dump and restored stream cannot be recovered; that is an expected limitation only for an **unconfirmed** deletion. **C1 makes it possible for a confirmed deletion too.** The current C32 drill tests a successful latest stream, not a regressing or incomplete latest snapshot. See [restore.py:209](/home/user/job-hunter/backend/src/jobhunter/backup/restore.py:209) and [test_prod_restore.py:379](/home/user/job-hunter/tests/stack/test_prod_restore.py:379).

   Restore verifies the loaded dump before migration and verifies replay afterwards. Remaining ready exports expire, running exports fail, and their volume is cleared. `data_key_ids` combines keys found in the database snapshot with CV-file key IDs. I found no additional defect in these mechanisms. See [restore.py:508](/home/user/job-hunter/backend/src/jobhunter/backup/restore.py:508), [restore.py:459](/home/user/job-hunter/backend/src/jobhunter/backup/restore.py:459), and [backup_set.py:198](/home/user/job-hunter/backend/src/jobhunter/backup/backup_set.py:198), [backup_set.py:288](/home/user/job-hunter/backend/src/jobhunter/backup/backup_set.py:288).

   Unknown formats fail closed. Older-release readability depends on T31-2’s documented reader-first expand/contract procedure; the current parser does not promise arbitrary future-format support. See [manifests.py:236](/home/user/job-hunter/backend/src/jobhunter/privacy/manifests.py:236) and [RUNBOOK.md:703](/home/user/job-hunter/docs/RUNBOOK.md:703).

3. **Alerts, dispatcher and dead-man switch**

   The catalogue audit matched all 72 spec entries, including severity, escalation, push eligibility and auto/manual resolution flags; producer-site inspection found no additional missing-code mismatch. Escalation clears delivery/acknowledgement state. The dispatcher bounds each pass, backs off after delivery failure, excludes `notify.delivery_failed` from delivery, and resolves that alert after recovery. See [catalogue.py:89](/home/user/job-hunter/backend/src/jobhunter/observability/catalogue.py:89), [alerts.py:50](/home/user/job-hunter/backend/src/jobhunter/observability/alerts.py:50), [ntfy.py:270](/home/user/job-hunter/backend/src/jobhunter/observability/ntfy.py:270), and [test_alert_catalogue.py:122](/home/user/job-hunter/tests/unit/test_alert_catalogue.py:122).

   Lock **7261005** serializes ping passes and synchronization using a dedicated connection with release in cleanup. Disabled/paused state suppresses pings when read, but that decision is cached for an entire pass: a concurrent disable can still be followed by another ping. **M1** gives the reproduced failure. See [deadman.py:382](/home/user/job-hunter/backend/src/jobhunter/observability/deadman.py:382), [deadman.py:441](/home/user/job-hunter/backend/src/jobhunter/observability/deadman.py:441), and [deadman.py:478](/home/user/job-hunter/backend/src/jobhunter/observability/deadman.py:478).

4. **Secrets, keys and rotation**

   I found no additional rotation defect beyond parked T28-7. Exit outcomes distinguish complete rotation, previous-key remnants/failure, missing previous key, and other-key remnants. `SEALED_COLUMNS` covers the two encrypted database columns found in the schema. CV resealing authenticates existing content and uses replacement with file/directory durability. See [crypto_rotate.py:429](/home/user/job-hunter/backend/src/jobhunter/crypto_rotate.py:429), [crypto.py:49](/home/user/job-hunter/backend/src/jobhunter/crypto.py:49), and [storage.py:119](/home/user/job-hunter/backend/src/jobhunter/profile/storage.py:119).

   The role-rotation script passes paths and role names in command arguments, reads the password inside the container, and uses quoted psql substitution. PostgreSQL suppresses statement and parameter logging. I found no password-on-command-line or configured-log path in that procedure. See [rotate-db-role.sh:38](/home/user/job-hunter/scripts/rotate-db-role.sh:38) and [postgresql.conf:20](/home/user/job-hunter/deploy/postgres/postgresql.conf:20).

   The recovery-kit instructions require earlier keys needed by retained sets, identified through `data_key_ids`. Host setup explicitly checks all five application roles: api, worker, migrate, backup and restore. See [RUNBOOK.md:570](/home/user/job-hunter/docs/RUNBOOK.md:570) and [host-setup.sh:203](/home/user/job-hunter/scripts/host-setup.sh:203).

5. **Production deployment surface**

   Static inspection found no additional least-privilege discrepancy: application defaults establish non-root execution, read-only filesystems and dropped capabilities; api uses isolated networks, docparse has no network, and provider/backup secrets are scoped by service. The explicit PostgreSQL/Caddy exceptions remain as ruled. See [compose.yaml:39](/home/user/job-hunter/deploy/compose.yaml:39), [compose.yaml:118](/home/user/job-hunter/deploy/compose.yaml:118), [compose.yaml:191](/home/user/job-hunter/deploy/compose.yaml:191), and [compose.yaml:292](/home/user/job-hunter/deploy/compose.yaml:292).

   I found no new workflow expression-to-shell injection or permission/pin defect in the reviewed jobs. CI/test workflows have read-only repository permissions; release publication and PR creation receive separate write permissions. See [ci.yml:16](/home/user/job-hunter/.github/workflows/ci.yml:16), [prodstack.yml:30](/home/user/job-hunter/.github/workflows/prodstack.yml:30), [embedder-image.yml:22](/home/user/job-hunter/.github/workflows/embedder-image.yml:22), and [release.yml:39](/home/user/job-hunter/.github/workflows/release.yml:39), [release.yml:128](/home/user/job-hunter/.github/workflows/release.yml:128).

   A fresh VPS still has the **explicitly unresolved operator-statement completion step**, and a broken nft rule does not prevent Docker startup. These are assessed under parked items 2 and 5. I found no further newly failing checklist step by inspection; deployment success was not exercised. See [RUNBOOK.md:92](/home/user/job-hunter/docs/RUNBOOK.md:92) and [RUNBOOK.md:317](/home/user/job-hunter/docs/RUNBOOK.md:317).

6. **Privacy statements versus behaviour**

   **I1 contradicts the statement’s absolute Adzuna-removal promise; C1 contradicts its restore guarantee.** See [PRIVACY.md:130](/home/user/job-hunter/docs/PRIVACY.md:130) and [PRIVACY.md:139](/home/user/job-hunter/docs/PRIVACY.md:139).

   Otherwise, the inspected retention contract agrees: delete-everything retains the disclosed account/TOTP and operational accounting records; export rows survive until 30 days after expiry; daily cleanup explains the disclosed extra availability after seven days; offsite lag is stated as about four months. Expired login-lockout retention is disclosed rather than silently presented as bounded. See [delete_everything.py:167](/home/user/job-hunter/backend/src/jobhunter/privacy/kinds/delete_everything.py:167), [export.py:870](/home/user/job-hunter/backend/src/jobhunter/privacy/export.py:870), and [PRIVACY.md:14](/home/user/job-hunter/docs/PRIVACY.md:14), [PRIVACY.md:118](/home/user/job-hunter/docs/PRIVACY.md:118).

   LangSmith construction enables hidden inputs/outputs and the anonymizer retains allowlisted metadata and sanitized error tags. No additional understatement was found in those inspected tracing controls. See [langsmith.py:201](/home/user/job-hunter/backend/src/jobhunter/observability/langsmith.py:201) and [langsmith.py:219](/home/user/job-hunter/backend/src/jobhunter/observability/langsmith.py:219).

7. **Frontend**

   I found no additional functional defect in the inspected privacy dialogs: exact confirmation phrases, password/TOTP handling, single-flight submission and status polling are present. “Confirmed offsite” remains dependent on the backend receipt guarantee broken by C1. See [PrivacyActions.tsx:115](/home/user/job-hunter/frontend/src/system/PrivacyActions.tsx:115) and [privacyApi.ts:170](/home/user/job-hunter/frontend/src/system/privacyApi.ts:170).

   Resolve is limited to manual unresolved alerts; backup health renders shipment/restore state; failed purge Retry has confirmation and single-flight handling. Privacy errors use code-based translation rather than server text. See [AlertsList.tsx:59](/home/user/job-hunter/frontend/src/system/AlertsList.tsx:59), [HealthPanel.tsx:85](/home/user/job-hunter/frontend/src/system/HealthPanel.tsx:85), [NeedsAttention.tsx:511](/home/user/job-hunter/frontend/src/system/NeedsAttention.tsx:511), and [PrivacyProblem.tsx:22](/home/user/job-hunter/frontend/src/system/PrivacyProblem.tsx:22).

   The CSP artifact guard and E18 axe assertions are present; I did not execute a browser/build check. E18’s transient-state race remains **M2**. All **1,765 en/de keys** matched in the offline comparison; the English statement with a German explanatory note follows the accepted decision. See [check-dist-csp.mjs:89](/home/user/job-hunter/frontend/scripts/check-dist-csp.mjs:89), [20-e18-privacy.spec.ts:169](/home/user/job-hunter/frontend/e2e/specs/20-e18-privacy.spec.ts:169), and [PrivacyStatement.tsx:85](/home/user/job-hunter/frontend/src/system/PrivacyStatement.tsx:85).

8. **Compatibility and rollback**

   I found no additional floor-classification defect. A newer unknown head needs a compatibility record, a known floor and a non-destructive declaration; otherwise it is refused. Migration 0011 only adds its index and correctly declares floor 0010. See [compat.py:190](/home/user/job-hunter/backend/src/jobhunter/db/compat.py:190) and [0011_m5_purge_operation_index.py:35](/home/user/job-hunter/backend/src/jobhunter/db/migrations/versions/0011_m5_purge_operation_index.py:35).

   The drill asserts successful rollback on the expand schema and refusal after marking it destructive. The runbook preserves the distinction between compatible image rollback and restore, including reader-first manifest-format changes. These are inspected assertions, not a drill result from this review. See [test_prod_drills.py:222](/home/user/job-hunter/tests/stack/test_prod_drills.py:222) and [RUNBOOK.md:703](/home/user/job-hunter/docs/RUNBOOK.md:703).

### Findings

**Critical**

**C1 — The newest manifest snapshot can omit an already-confirmed deletion.**

Locations: [shipper.py:139](/home/user/job-hunter/backend/src/jobhunter/backup/shipper.py:139), [shipper.py:17](/home/user/job-hunter/backend/src/jobhunter/backup/shipper.py:17), [reconcilers.py:57](/home/user/job-hunter/backend/src/jobhunter/privacy/reconcilers.py:57), [restore.py:203](/home/user/job-hunter/backend/src/jobhunter/backup/restore.py:203).

Concrete failure:

1. Shipper A enumerates `{M1}` and pauses at its database await.
2. A new deletion M2 completes. Shipper B uploads `{M1, M2}` and records a successful receipt; M2 becomes confirmed offsite.
3. A resumes and uploads its earlier `{M1}` list. This is now the newest snapshot.
4. The VPS is lost before another shipment. Restore uses a dump predating M2 and the newest stream, which lacks M2. Neither verification phase has evidence that M2 should exist. Deleted data returns.

The offline probe reproduced snapshot creation order **`[[1, 2], [1]]`**, with a successful receipt for manifest 2.

There is a second failure path: as the shipper docstring acknowledges, restic exit 3 can leave an incomplete snapshot tagged `manifests`. Restore selects it even though no successful shipment receipt names it. An omitted manifest newer than the chosen dump is likewise undetectable.

**Fix:** serialize shipment enumeration through publication, and make restore consume a verifiably successful, complete offsite generation—or a verified, conflict-checked union of retained streams. A receipt available only in the lost database is insufficient. Add restore coverage for both reversed ship ordering and a failed partial snapshot following a successful shipment.

**Important**

**I1 — Adzuna-derived scores survive in feedback snapshots and subsequent exports.**

Locations: [source_purge.py:503](/home/user/job-hunter/backend/src/jobhunter/privacy/kinds/source_purge.py:503), [source_purge.py:885](/home/user/job-hunter/backend/src/jobhunter/privacy/kinds/source_purge.py:885), [golden.py:319](/home/user/job-hunter/backend/src/jobhunter/scoring/golden.py:319), [0008_m3_scoring.py:520](/home/user/job-hunter/backend/src/jobhunter/db/migrations/versions/0008_m3_scoring.py:520), [export.py:394](/home/user/job-hunter/backend/src/jobhunter/privacy/export.py:394).

Concrete failure: label an Adzuna-derived score or submit “wrong score,” then purge Adzuna. `score_feedback.score_snapshot` contains copied L/M/U, coverage and confidence. The purge deletes associated results/revisions but neither deletes nor redacts feedback. Its foreign keys become NULL; the copied scores remain and a later export includes them. Replay repeats the omission.

This violates [spec §20.5:6717](/home/user/job-hunter/docs/superpowers/specs/2026-10-16-job-hunter-design.md:6717) and [PRIVACY.md:140](/home/user/job-hunter/docs/PRIVACY.md:140).

**Fix:** freeze affected feedback IDs before provenance is detached, include bounded snapshot redaction/deletion in the manifest, and verify it during live execution and replay. Explicitly address already-detached feedback whose provenance can no longer be established.

**Minor**

- **M1 — A disable does not invalidate an ongoing dead-man pass.** [deadman.py:478](/home/user/job-hunter/backend/src/jobhunter/observability/deadman.py:478) caches `may_ping`; [save.py:335](/home/user/job-hunter/backend/src/jobhunter/settings/save.py:335) updates the schedule independently. With two pending runs, disabling during the first send still permits the second `/fail` send. The offline probe reproduced this. **Fix:** synchronize disable with ping decisions and invalidate unsent work/retries; add a concurrent-disable test. This is a transient false-notification risk until synchronization pauses the check.

- **M2 — E18 requires observing an uncontrolled transient state.** [20-e18-privacy.spec.ts:177](/home/user/job-hunter/frontend/e2e/specs/20-e18-privacy.spec.ts:177). A fast worker can finish and log the browser out before the executing-state/axe assertions, producing a false gate failure. **Fix:** hold worker progress around those assertions and reliably release it in cleanup.

- **M3 — Kind-specific manifest bounds are not validated at parsing.** [manifests.py:236](/home/user/job-hunter/backend/src/jobhunter/privacy/manifests.py:236), [source_purge.py:894](/home/user/job-hunter/backend/src/jobhunter/privacy/kinds/source_purge.py:894). A structurally accepted manifest missing `max_ids["postings"]` reaches a bare `KeyError`. **Fix:** validate required bounds before replay starts, with a named refusal identifying manifest UUID, kind and missing bound. The current stop is fail-closed, so this is diagnostic/recovery hardening.

### Challenges to rulings

- **T24-2(i)/T26-1 and §19.5’s ship-lock exemption:** C1 demonstrates that scheduler serialization and restic’s ordering against prune do not serialize two shippers’ enumeration/publication. The exemption needs a separate shipment serialization mechanism. See [progress.md:405](/home/user/job-hunter/.superpowers/sdd/2026-11-09-m5-operations-privacy-production/progress.md:405), [progress.md:426](/home/user/job-hunter/.superpowers/sdd/2026-11-09-m5-operations-privacy-production/progress.md:426).

- **T10-4’s acceptance of incomplete snapshots until the next run:** the consequence is worse than a temporary incomplete backup. Restore can select that snapshot and reverse an earlier confirmed deletion. Successful-stream selection must survive loss of the live database. See [progress.md:172](/home/user/job-hunter/.superpowers/sdd/2026-11-09-m5-operations-privacy-production/progress.md:172).

### Parked list, section 2

1. **T28-7 export hash repair — acceptable as parked.** The hash repair does not authenticate ciphertext, but downloads independently authenticate it; the repaired hash is not currently a trust decision. Authenticate before repair when addressing this hardening item. See [crypto_rotate.py:321](/home/user/job-hunter/backend/src/jobhunter/crypto_rotate.py:321), [privacy.py:375](/home/user/job-hunter/backend/src/jobhunter/api/routes/privacy.py:375).

2. **Operator fields — acceptable only as an explicit deployment carry.** M5’s local scope may retain the decision, but the public deployment checklist cannot be completed with a stock bundled image. Provide either a deployment override or a documented private build path, with tests permitting completed values. See [PrivacyStatement.tsx:12](/home/user/job-hunter/frontend/src/system/PrivacyStatement.tsx:12), [test_privacy_md.py:212](/home/user/job-hunter/tests/unit/test_privacy_md.py:212), [RUNBOOK.md:92](/home/user/job-hunter/docs/RUNBOOK.md:92).

3. **E18 race — unsuitable as a permanent required gate.** Make it deterministic as M2 describes. It is a nonblocking test-reliability defect, not evidence that deletion itself fails.

4. **0011 generic plans — real eligibility gap, no demonstrated production-size blocker.** The repeatedly executed reconciler binds `kind`; a generic plan cannot infer the partial-index predicate. The migration documents precisely this limitation. I did not run PostgreSQL or establish which plan production chooses. Accept as parked at the documented scale; using literal `kind = 'purge'` in these fixed-purpose readers is a straightforward improvement. See [reconcilers.py:69](/home/user/job-hunter/backend/src/jobhunter/privacy/reconcilers.py:69), [0011_m5_purge_operation_index.py:14](/home/user/job-hunter/backend/src/jobhunter/db/migrations/versions/0011_m5_purge_operation_index.py:14).

5. **Fail-open nft unit — acceptable as parked for the current topology.** Api’s isolated networks and docparse’s absent network remain independent barriers. The nft unit is additional protection, and the runbook explicitly requires checking it after boot. This judgment depends on retaining that topology. See [jobhunter-egress.service:2](/home/user/job-hunter/deploy/firewall/jobhunter-egress.service:2), [compose.yaml:292](/home/user/job-hunter/deploy/compose.yaml:292), [RUNBOOK.md:317](/home/user/job-hunter/docs/RUNBOOK.md:317).

6. **Malformed `max_ids` — acceptable as nonblocking hardening, but implement named validation.** The bare stop does not silently approve a restore; it makes diagnosis unnecessarily difficult. M3 specifies the change.

### What I ran

- Read-only git history, diff/stat inspection, module/source searches, and the specified brief, authorities and parked decisions.
- An in-memory probe of the actual shipper orchestration with fake database/restic boundaries: reproduced a later snapshot omitting an already-receipted manifest.
- An in-memory dead-man probe: reproduced `success` while enabled followed by `fail` while disabled in one pass.
- Direct in-process assertions for the 72-entry alert catalogue, notification policy, privacy retention mappings and en/de retention strings; all passed. Full en/de key comparison passed for **1,765 keys**.
- Attempted the prescribed focused `uv run --group docparse pytest -W error -q -p no:cacheprovider …` invocation. It stopped **before collection** because `uv` could not create its cache-lock temporary file on the read-only filesystem. No pytest pass is claimed.
- No network, paid calls, Docker, Playwright, full suites, secret reads or repository writes. The review remains scoped to `69689d1..077e7c3`; two documentation-only commits appeared during review, without changing the reviewed implementation.