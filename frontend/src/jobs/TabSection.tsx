// A titled part of a drawer tab: a <section> named by its heading, so each part is a landmark region.
import { type ReactNode, useId } from "react";
import styles from "./TabSection.module.css";

export function TabSection({ heading, children }: { heading: string; children: ReactNode }) {
  const id = useId();
  return (
    <section aria-labelledby={id} className={styles.section}>
      <h3 id={id} className={styles.heading}>{heading}</h3>
      {children}
    </section>
  );
}
