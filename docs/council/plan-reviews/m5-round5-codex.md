**Verdict: REJECT**

I reviewed the brief, round-4 review and response including the seven sweep fixes, revised plan, binding rulings, relevant specification and research notes, M4 conventions, and code at HEAD `6b035c5`. This was a static review: no network, Docker or sub-agents; no `.secrets/`, `.fixtures-raw/`, keys or credentials read; nothing changed.

**Round-4 findings — disposition**

| # | Finding | Resolved? |
|---|---|---|
| 1 | Late settlement restores redacted profile content | **Partially.** The marker protects calls included in the manifest, but a call created after computation escapes it. Finding 1 below. |
| 2 | Replacement purge operations lose their request | **Yes.** Admission persists `deletion_manifests.request`; replacement operations compute from it (P:335–336, P:407–410). |
| 3 | Migration replay cannot remove actual export files | **Yes.** Replay explicitly runs export cleanup; migrate and restore mount the volume, with a separate scratch root for restore tests (P:623, P:670, P:711, P:733). |
| 4 | Restore leaves ready exports without archives | **Partially.** Invalidation fixes the missing-file response, but its ordering breaks subsequent replay. Finding 2 below. |

**New findings**

1. **MAJOR — Calls created after manifest computation escape profile redaction.** P:410, P:464–466; S:6462, S:6473; `backend/src/jobhunter/graphs/profile/nodes/propose_queries.py:278`; `backend/src/jobhunter/llm/spend_ledger.py:288`, `:862`, `:911` — The manifest freezes explicit call IDs before phase B cancels the proposal operation. Between those steps, an existing proposal coroutine can reserve and dispatch another call from its cached prompt: reservation checks operation ownership and the **global** fence, not the profile fence. This new call is absent from the manifest and retains `content_purged_at = NULL`. Cancellation then prevents proposal publication, but its late settlement still stores profile-derived output, defeating the round-4 fix. — Stop and serialize affected call creation before computing the immutable call set, through admission-time cancellation or a profile-fence check under the appropriate locks. Test a reservation after A2 but before cancellation, followed by settlement after purge completion; no output may survive.

2. **MAJOR — Restore invalidation puts exports into states that replay cannot purge.** P:123, P:337, P:417, P:711, P:732–736; S:6251, S:6272; `backend/src/jobhunter/db/transitions.py:749` — The prescribed C32 scenario backs up a `ready` export, then ships a source-purge manifest naming that export with marker `status='purged'`. Restore first changes the row to `expired`; replay must subsequently change it to `purged`. Task 1 permits neither `expired → purged` nor the equivalent `failed → purged` for restored running exports. Applying the marker fails transition validation; skipping it fails the manifest postcondition. The C32 assertion that the row remains `expired` also contradicts that postcondition. — Make invalidation and replay compatible. For example, invalidate only remaining `ready`/`running` exports after replay, preserving rows already marked `purged`, and update C32’s expected status. Cover both ready and running exports named by a newer manifest.

3. **MAJOR — The local restic repository volume is unwritable by the shipper.** P:361, P:367–369, P:813–816; S:5932; `Dockerfile:47–52`; `deploy/compose.dev.yaml:230` — The shipper runs as `10001:10001` and initializes `/data/restic-repo` on a fresh named volume. The planned image creates owned mount points for manifests, exports and staging, but never `/data/restic-repo`. That volume therefore starts root-owned, and the non-root shipper cannot initialize its repository. Task 10’s local-repository test and E18 cannot pass on fresh volumes. — Create `/data/restic-repo` as `10001:10001`, mode `0700`, in the backup image before switching users. Include it in the fresh-volume ownership test and exercise initialization without pre-existing volumes.

### Challenges to rulings

None.