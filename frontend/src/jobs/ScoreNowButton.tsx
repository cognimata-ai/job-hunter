// Score now (spec §15.3, §9.6.4; plan M4 Task 22, Ruling f): "Score now ~$<c_hat>", the estimated cost of one
// scored ad (`GET /api/system/spend`, read once for the page), "<$0.01" below a cent, "Score now" without the
// spend. While the header's status has a `cap_reached` banner the button is disabled, and its explanation is a
// text beside it, linked by `aria-describedby` (a disabled button takes no focus, so no hover-only tooltip).
// A click is a keyed `POST …/score-now`; a second click after a 5xx or no answer is the manual retry, with the
// same key. Its answers are the card's (`useCardFailure`).
import { useId } from "react";
import { useTranslation } from "react-i18next";
import { useFormat } from "../system/format";
import { useCapReached, useScoreNow, useSpend } from "./api";
import styles from "./ScoreNowButton.module.css";

const CENT = 0.01;

export function ScoreNowButton({ jobId }: { jobId: number }) {
  const { t } = useTranslation();
  const fmt = useFormat();
  const spend = useSpend();
  const capReached = useCapReached();
  const scoreNow = useScoreNow();
  const hintId = useId();
  const cHat = spend.data?.c_hat;
  const label =
    cHat === undefined
      ? t("card.score_now")
      : Number(cHat) < CENT
        ? t("card.score_now_below", { cost: fmt.usd(String(CENT)) })
        : t("card.score_now_cost", { cost: fmt.usd(cHat) });
  return (
    <div className={styles.scoreNow}>
      <button type="button" className={styles.button} disabled={capReached || scoreNow.isPending}
        aria-busy={scoreNow.isPending} aria-describedby={capReached ? hintId : undefined}
        onClick={() => { scoreNow.mutate({ jobId, force: false }); }}>
        {label}
      </button>
      {capReached && <span id={hintId} className={styles.hint}>{t("card.score_now_cap")}</span>}
    </div>
  );
}
