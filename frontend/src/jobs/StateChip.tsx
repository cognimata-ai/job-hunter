// The state chip of an unscored card (spec §15.3's table; plan M4 Task 22, Ruling e, B8, B24), by its target's
// state, in the app's own texts:
//
//   pending            "Pending — queued"
//   budget_deferred    "Budget deferred — resumes when budget frees up"
//   filtered_out       "Filtered: excluded keyword '<keyword>'" (else "Filtered: <reasons>"), with Score anyway
//   failed_*           its `target_state` name ("Failed (temporary error)"), its reason code beneath, with Retry
//   refused            "Declined by the model (<category>)", with Retry
//   expired_unscored   "Expired before scoring"
//   in_batch           "In batch (results within 24 h)"
//   deferred_duplicate "Represented by duplicate"
//   in_progress        "Scoring in progress"
//   no target          "Not queued yet"
//
// Any other state reads its `target_state.<state>` name. A card in the `not_a_job_ad` bucket (a verified "not a
// job ad" result: no score, a published target) reads "Not a job ad (verified)". Score anyway asks first (a
// `ConfirmDialog` naming the reason), then sends a keyed score-now with `force`; like Score now, it is disabled
// while the header's status has a `cap_reached` banner, with the same text beside it (`aria-describedby`).
// Retry sends a keyed retry. The reason code and the refusal category are the server's, shown verbatim.
import type { TFunction } from "i18next";
import { type ReactNode, useId, useState } from "react";
import { useTranslation } from "react-i18next";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { cx } from "../components/cx";
import { type TranslationKey, useServerText } from "../i18n/index";
import { type ScoringBucket, type TargetOut, useCapReached, useRetryTarget, useScoreNow } from "./api";
import { filteredText, type ServerText } from "./format";
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
  const capReached = useCapReached();
  const hintId = useId();
  const [confirming, setConfirming] = useState(false);
  return (
    <div className={styles.withHint}>
      <button type="button" className={styles.action} disabled={capReached}
        aria-describedby={capReached ? hintId : undefined} onClick={() => { setConfirming(true); }}>
        {t("card.score_anyway")}
      </button>
      {capReached && <span id={hintId} className={styles.hint}>{t("card.score_now_cap")}</span>}
      <ConfirmDialog open={confirming} title={t("card.score_anyway_title")} confirmLabel={t("card.score_anyway")}
        busy={scoreNow.isPending}
        onCancel={() => { setConfirming(false); }}
        onConfirm={() => {
          scoreNow.mutate({ jobId, force: true }, { onSettled: () => { setConfirming(false); } });
        }}>
        {t("card.score_anyway_body", { reason })}
      </ConfirmDialog>
    </div>
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

/** The chip's text (the module comment's table): the card's chip, and the drawer's for a job without a score. */
export function chipText(
  target: TargetOut | null,
  bucket: ScoringBucket,
  filterKeyword: string | null,
  filterReasons: readonly string[],
  t: TFunction,
  serverText: ServerText,
): string {
  const fixed = target === null ? undefined : FIXED.get(target.state);
  if (bucket === "not_a_job_ad") {
    return t("chip.not_a_job_ad");
  }
  if (target === null) {
    return t("chip.not_queued");
  }
  if (fixed !== undefined) {
    return t(fixed[0]);
  }
  if (target.state === "filtered_out") {
    return filteredText(filterKeyword, filterReasons, t, serverText);
  }
  if (target.state === "refused") {
    return target.refusal_category === null ? t("chip.refused_plain") : t("chip.refused", { category: target.refusal_category });
  }
  return serverText("target_state", target.state); // a failed one's too: "Failed (temporary error)", §15.3's "Failed: <reason>"
}

export function StateChip({ jobId, target, bucket, filterKeyword, filterReasons }: StateChipProps) {
  const { t } = useTranslation();
  const serverText = useServerText();
  const text = chipText(target, bucket, filterKeyword, filterReasons, t, serverText);
  let tone: Tone = "neutral";
  let action: ReactNode = null;
  let reason: string | null = null;
  if (bucket !== "not_a_job_ad" && target !== null) {
    const fixed = FIXED.get(target.state);
    if (fixed !== undefined) {
      tone = fixed[1];
    } else if (target.state === "filtered_out") {
      action = <ScoreAnyway jobId={jobId} reason={text} />;
    } else if (isFailed(target.state)) {
      tone = "critical";
      reason = target.state_reason;
      action = <Retry targetId={target.id} />;
    } else if (target.state === "refused") {
      tone = "critical";
      action = <Retry targetId={target.id} />;
    }
  }
  return (
    <div className={styles.state}>
      <p className={cx(styles.chip, styles[tone])}>{text}</p>
      {reason !== null && <code className={styles.reason}>{reason}</code>}
      {action !== null && <div className={styles.actions}>{action}</div>}
    </div>
  );
}
