// The score badge of a scored card (spec §15.3): M as a large integer, the interval bar [L, U] with its
// numbers, the coverage, and a confidence dot with its text (colour is never the only signal).
//
// The bar is an inline SVG on a 0–100 scale whose rects take `x` and `width` as attributes: no `style` at all,
// so nothing for the CSP (`style-src 'self'`) to judge. It is an image with a text label ("Score 82, range
// 74 to 90").
import { useTranslation } from "react-i18next";
import { cx } from "../components/cx";
import { useServerText } from "../i18n/index";
import type { ScoreOut } from "./api";
import { percentInt, scoreInt } from "./format";
import styles from "./ScoreBadge.module.css";

const CONFIDENCE_CLASS: Record<ScoreOut["confidence"], string | undefined> = {
  low: styles.low,
  medium: styles.medium,
  high: styles.high,
};

export function ScoreBadge({ score }: { score: ScoreOut }) {
  const { t } = useTranslation();
  const serverText = useServerText();
  const mid = scoreInt(score.mid);
  const low = scoreInt(score.low);
  const high = Math.max(low, scoreInt(score.high));
  const values = { mid: String(mid), low: String(low), high: String(high) };
  return (
    <div className={styles.badge}>
      <span className={styles.mid}>{mid}</span>
      <div className={styles.interval}>
        <svg role="img" aria-label={t("score.label", values)} className={styles.bar} viewBox="0 0 100 10"
          preserveAspectRatio="none" focusable="false">
          <rect className={styles.track} x="0" y="3" width="100" height="4" rx="2" />
          <rect data-part="interval" className={styles.range} x={low} y="2" width={high - low} height="6" rx="1" />
          <rect className={styles.marker} x={Math.min(Math.max(mid - 0.75, 0), 98.5)} y="0" width="1.5" height="10" />
        </svg>
        <span className={styles.numbers}>{t("score.range", values)}</span>
      </div>
      <span className={styles.facts}>
        <span>{t("score.coverage", { percent: String(percentInt(score.coverage)) })}</span>
        <span className={styles.confidence}>
          <span className={cx(styles.dot, CONFIDENCE_CLASS[score.confidence])} aria-hidden="true" />
          <span className="visually-hidden">{t("score.confidence")} </span>
          <span>{serverText("confidence", score.confidence)}</span>
        </span>
      </span>
    </div>
  );
}
