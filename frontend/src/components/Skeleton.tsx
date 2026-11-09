// Loading placeholders (spec §15.8: 8 skeleton cards for the list, one for the drawer). Hidden from assistive
// technology, which reads the one "Loading…" instead.
import { useTranslation } from "react-i18next";
import styles from "./Skeleton.module.css";

export interface SkeletonProps {
  variant: "card" | "line" | "drawer";
  count?: number;
}

export function Skeleton({ variant, count = 1 }: SkeletonProps) {
  const { t } = useTranslation();
  return (
    <div className={styles.group} role="status" aria-busy="true">
      <span className="visually-hidden">{t("app.loading")}</span>
      {Array.from({ length: count }, (_, i) => (
        <div key={i} className={styles[variant]} aria-hidden="true">
          {variant !== "line" && (
            <>
              <span className={styles.barWide} />
              <span className={styles.bar} />
              <span className={styles.barShort} />
            </>
          )}
        </div>
      ))}
    </div>
  );
}
