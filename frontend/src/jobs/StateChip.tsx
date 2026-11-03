// The state chip of an unscored card (spec §15.3's table; plan M4 Task 22, Ruling e, B8, B24), by its target's
// state, in the app's own texts:
//
//   pending            "Pending — queued"
//   budget_deferred    "Budget deferred — resumes when budget frees up"
//   filtered_out       "Filtered: excluded keyword '<keyword>'" (else "Filtered: <reasons>"), with Score anyway
//   failed_*           "Failed: <state>", its reason code beneath, with Retry
//   refused            "Declined by the model (<category>)", with Retry
//   expired_unscored   "Expired before scoring"
//   in_batch           "In batch (results within 24 h)"
//   deferred_duplicate "Represented by duplicate"
//   in_progress        "Scoring in progress"
//   no target          "Not queued yet"
//
// Any other state reads its `target_state.<state>` name. A card in the `not_a_job_ad` bucket (a verified "not a
// job ad" result: no score, a published target) reads "Not a job ad (verified)". Score anyway asks first (a
// `ConfirmDialog` naming the reason), then sends a keyed score-now with `force`; Retry sends a keyed retry.
// The reason code and the refusal category are the server's, shown verbatim.
import { type ReactNode, useState } from "react";
import { useTranslation } from "react-i18next";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { cx } from "../components/cx";
import { type TranslationKey, useServerText } from "../i18n/index";
import { type ScoringBucket, type TargetOut, useRetryTarget, useScoreNow } from "./api";
import styles from "./StateChip.module.css";

export interface StateChipProps {
  jobId: number;
  target: TargetOut | null;
  bucket: ScoringBucket;
  filterKeyword: string | null;
  filterReasons: readonly string[];
}

type Tone = "info" | "warn" | "neutral" | "critical";

// The states whose chip is one fixed text.
const FIXED: ReadonlyMap<string, readonly [TranslationKey, Tone]> = new Map([
  ["pending", ["chip.pending", "info"]],
  ["budget_deferred", ["chip.budget_deferred", "warn"]],
  ["expired_unscored", ["chip.expired_unscored", "warn"]],
  ["in_batch", ["chip.in_batch", "info"]],
  ["deferred_duplicate", ["chip.deferred_duplicate", "neutral"]],
  ["in_progress", ["chip.in_progress", "info"]],
] as const);

function isFailed(state: string): boolean {
  return state.startsWith("failed_");
}

function ScoreAnyway({ jobId, reason }: { jobId: number; reason: string }) {
  const { t } = useTranslation();
  const scoreNow = useScoreNow();
  const [confirming, setConfirming] = useState(false);
  return (
    <>
      <button type="button" className={styles.action} onClick={() => { setConfirming(true); }}>
        {t("card.score_anyway")}
      </button>
      <ConfirmDialog open={confirming} title={t("card.score_anyway_title")} confirmLabel={t("card.score_anyway")}
        busy={scoreNow.isPending}
        onCancel={() => { setConfirming(false); }}
        onConfirm={() => {
          scoreNow.mutate({ jobId, force: true }, { onSettled: () => { setConfirming(false); } });
        }}>
        {t("card.score_anyway_body", { reason })}
      </ConfirmDialog>
    </>
  );
}

function Retry({ targetId }: { targetId: number }) {
  const { t } = useTranslation();
  const retry = useRetryTarget();
  return (
    <button type="button" className={styles.action} disabled={retry.isPending} aria-busy={retry.isPending}
      onClick={() => { retry.mutate({ targetId }); }}>
      {t("action.retry")}
    </button>
  );
}

export function StateChip({ jobId, target, bucket, filterKeyword, filterReasons }: StateChipProps) {
  const { t } = useTranslation();
  const serverText = useServerText();
  const fixed = target === null ? undefined : FIXED.get(target.state);
  let text: string;
  let tone: Tone = "neutral";
  let action: ReactNode = null;
  let reason: string | null = null;
  if (bucket === "not_a_job_ad") {
    text = t("chip.not_a_job_ad");
  } else if (target === null) {
    text = t("chip.not_queued");
  } else if (fixed !== undefined) {
    text = t(fixed[0]);
    tone = fixed[1];
  } else if (target.state === "filtered_out") {
    text =
      filterKeyword !== null
        ? t("chip.filtered_keyword", { keyword: filterKeyword })
        : filterReasons.length > 0
          ? t("chip.filtered_reasons", { reasons: filterReasons.map((r) => serverText("filter_reason", r)).join(", ") })
          : serverText("target_state", target.state);
    action = <ScoreAnyway jobId={jobId} reason={text} />;
  } else if (isFailed(target.state)) {
    text = t("chip.failed", { reason: serverText("target_state", target.state) });
    tone = "critical";
    reason = target.state_reason;
    action = <Retry targetId={target.id} />;
  } else if (target.state === "refused") {
    text =
      target.refusal_category === null
        ? t("chip.refused_plain")
        : t("chip.refused", { category: target.refusal_category });
    tone = "critical";
    action = <Retry targetId={target.id} />;
  } else {
    text = serverText("target_state", target.state);
  }
  return (
    <div className={styles.state}>
      <p className={cx(styles.chip, styles[tone])}>{text}</p>
      {reason !== null && <code className={styles.reason}>{reason}</code>}
      {action !== null && <div className={styles.actions}>{action}</div>}
    </div>
  );
}
