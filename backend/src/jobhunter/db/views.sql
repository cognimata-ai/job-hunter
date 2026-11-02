-- §7.18 The current-score views: the one source of both definitions (spec §4.2, `db/views.sql`).
--
-- Migration 0008 creates the views from this file, and a later change of a view replaces them from it, so
-- each statement is a CREATE OR REPLACE VIEW (the spec text reads CREATE VIEW). Every statement starts with
-- CREATE at the beginning of a line; the migration splits the file there and runs one statement at a time.
-- No comment names a bind parameter (a colon directly before a word), because the statements run as SQL text.
--
-- One predicate is added to the spec text: current_scores leaves out the synthetic golden-case jobs
-- (`NOT j.synthetic_eval`). Its target join is a LEFT JOIN, so an adversarial job of the golden set would
-- otherwise appear with NULL scores (Codex r1 #32; M3 plan Deviation 21; Task 25 writes it into §7.18).
-- display_scores reads current_scores, so it leaves them out too.

-- The view joins the active LLM tuple, the active scope and current score instances. It never selects the
-- newest result by timestamp.
CREATE OR REPLACE VIEW current_scores AS
SELECT j.id                    AS job_id,
       j.current_revision_id,
       t.id                    AS target_id,
       t.state                 AS target_state,
       t.similarity,
       r.id                    AS llm_result_id,
       r.status                AS result_status,          -- accepted | contested
       r.not_a_job_ad,
       d.id                    AS det_result_id,
       d.score_low, d.score_mid, d.score_high, d.coverage, d.confidence,
       hf.passed               AS passes_hard_filters,
       hf.reasons              AS filter_reasons
  FROM jobs j
  JOIN active_config ac ON ac.id = 1
  LEFT JOIN score_targets t
         ON t.job_revision_id = j.current_revision_id AND t.llm_tuple_id = ac.llm_tuple_id
  LEFT JOIN llm_results r ON r.id = t.published_result_id
  LEFT JOIN det_results d ON d.llm_result_id = r.id AND d.scope_version_id = ac.scope_version_id
                         AND d.is_current
  LEFT JOIN hard_filter_results hf ON hf.job_id = j.id AND hf.scope_version_id = ac.scope_version_id
 WHERE j.merged_into_job_id IS NULL
   AND j.current_revision_id IS NOT NULL                -- NULL only for a fenced job mid-replay (§19.5)
   AND NOT j.synthetic_eval;                            -- M3 plan Deviation 21 (Codex r1 #32)

-- Display view: a deferred group member shows its representative's score, labelled "scored via".
-- A member with its own published score keeps it and is labelled "possible duplicate of".
CREATE OR REPLACE VIEW display_scores AS
SELECT cs.job_id,
       COALESCE(cs.det_result_id, rep.det_result_id) AS det_result_id,
       COALESCE(cs.score_mid,  rep.score_mid)        AS score_mid,
       COALESCE(cs.score_low,  rep.score_low)        AS score_low,
       COALESCE(cs.score_high, rep.score_high)       AS score_high,
       COALESCE(cs.coverage,   rep.coverage)         AS coverage,
       COALESCE(cs.confidence, rep.confidence)       AS confidence,
       COALESCE(cs.result_status, rep.result_status) AS result_status,
       CASE WHEN cs.target_state = 'deferred_duplicate' AND rep.det_result_id IS NOT NULL
            THEN g.representative_job_id END          AS scored_via_job_id,
       CASE WHEN cs.det_result_id IS NOT NULL AND g.representative_job_id IS DISTINCT FROM cs.job_id
            THEN g.representative_job_id END          AS possible_duplicate_of_job_id,
       (cs.target_state = 'deferred_duplicate')       AS rendered_in_group_card,
       cs.target_state, cs.similarity, cs.passes_hard_filters, cs.filter_reasons
  FROM current_scores cs
  JOIN jobs j ON j.id = cs.job_id
  LEFT JOIN duplicate_groups g ON g.id = j.duplicate_group_id AND g.status = 'active'
  LEFT JOIN current_scores rep ON rep.job_id = g.representative_job_id
                              AND cs.target_state = 'deferred_duplicate';
