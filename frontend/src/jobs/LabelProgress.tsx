// The labelling progress (spec §15.6; plan M4 Task 26, resolution d): "23 / 50 labelled" over a bar, as a
// `progressbar` whose value is the count of labels and whose maximum is the target; at or past the target the
// text says so. Past it, the value stays at the maximum (a progressbar's value lies within its range) and the
// text carries the count (`aria-valuetext`). The bar is an SVG on a 0–100 scale whose fill takes `width` as an
// attribute: no `style` (CSP `style-src 'self'`).
import { useTranslation } from "react-i18next";
import { useFormat } from "../system/format";
import styles from "./LabelProgress.module.css";

export interface LabelProgressProps {
  labelled: number;
  target: number;
}

export function LabelProgress({ labelled, target }: LabelProgressProps) {
  const { t } = useTranslation();
  const format = useFormat();
  const reached = labelled >= target;
  const values = { labelled: format.number(labelled), target: format.number(target) };
  const text = reached ? t("label_mode.progress_reached", values) : t("label_mode.progress", values);
  const now = Math.min(Math.max(labelled, 0), target);
  const fill = target > 0 ? Math.round((now / target) * 100) : 0;
  return (
    <div role="progressbar" className={styles.progress} aria-label={t("label_mode.progress_label")}
      aria-valuemin={0} aria-valuemax={target} aria-valuenow={now} aria-valuetext={text}>
      <span className={styles.text}>{text}</span>
      <svg className={styles.bar} viewBox="0 0 100 8" preserveAspectRatio="none" aria-hidden="true" focusable="false">
        <rect className={styles.track} x="0" y="0" width="100" height="8" />
        <rect data-part="fill" className={reached ? styles.reached : styles.fill} x="0" y="0" width={fill} height="8" />
      </svg>
    </div>
  );
}
