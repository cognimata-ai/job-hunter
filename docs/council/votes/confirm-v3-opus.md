# Confirmation of consensus-v3: Claude Opus 5.5

## (1) My blocking items

- **N1 (claim heartbeat during long LLM calls) is RESOLVED.** G2 now refreshes the claim heartbeat from a background task every 30 s for as long as the claim is held, including during LLM calls that last up to 600 s. Each call also re-checks the claim before it runs, and publish uses a compare-and-swap (CAS) check.
- **N2 (stranded deferred-duplicate members) is RESOLVED.** F4 adds representative re-election whenever the representative becomes non-active, expires unscored, fails or is refused. F4 and M1 add a reconciler invariant: every duplicate group with an active member has a live representative.

## (2) New blocking defects in the v3 changes

I found **none**.
- **A4:** a single retry layer, with a reservation per attempt and conservative settlement, is sound.
- **A7:** a `custom_id`-set match after the batch ends, plus the 25 h `not_submitted` rule, is sound. Batches end by 24 h, so a submit that did happen cannot be misclassified.
- **G2** is fixed as above.
- **F5:** the sighting rule is consistent with the TTL and the ATS closure rule.

Two non-blocking items, both editorial fixes:

1. **B7 contradicts B3.**
   - B7 says "A heartbeat stale for more than 3 min triggers a resume from the checkpoint".
   - B3 says "A resumed attempt starts on a new thread … and does not continue the old thread".
   - Fix: change B7 to "triggers a new attempt (new thread, CAS-fenced), rebuilt from DB receipts".
2. **The B3 takeover test uses the *run* heartbeat as its liveness signal.**
   - A healthy worker that is idle between runs always has a stale run heartbeat.
   - So if two worker processes ever exist at once (for example a mistaken `docker compose run worker …` or `--scale`), each kills the other's lock session in turn.
   - Both watchdogs then `os._exit` and the workers flap indefinitely, interrupting runs, instead of the second one simply waiting for the lock.
   - Normal single-replica operation never triggers this.
   - Fix: base takeover on a **worker-liveness heartbeat** that the lock holder writes every 10 s, independent of runs.

## (3) Vote

**CONFIRM.** I have no blocking items. I recommend applying the two editorial fixes above when the final document is written.
