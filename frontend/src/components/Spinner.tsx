// A spinner with its text, for a long action (spec §15.8).
import styles from "./Spinner.module.css";

export function Spinner({ label }: { label: string }) {
  return (
    <span role="status" className={styles.spinner}>
      <span className={styles.ring} aria-hidden="true" />
      <span>{label}</span>
    </span>
  );
}
