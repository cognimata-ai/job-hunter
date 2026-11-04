// The preliminary similarity of an unscored card (spec §15.3): a native meter from 0 to 1, which needs no
// style, labelled "prelim. similarity — not a score", with its value to two decimals.
import { useId } from "react";
import { useTranslation } from "react-i18next";
import { intlLocale } from "../i18n/index";
import styles from "./SimilarityBar.module.css";

export function SimilarityBar({ value }: { value: number }) {
  const { t, i18n } = useTranslation();
  const labelId = useId();
  const text = new Intl.NumberFormat(intlLocale(i18n.language), {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(value);
  return (
    <div className={styles.similarity}>
      <span id={labelId}>{t("card.similarity")}</span>
      <span className={styles.value}>
        <meter className={styles.meter} min={0} max={1} value={value} aria-labelledby={labelId} aria-valuetext={text} />
        <span aria-hidden="true">{text}</span>
      </span>
    </div>
  );
}
