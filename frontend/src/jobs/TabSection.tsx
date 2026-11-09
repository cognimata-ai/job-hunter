// A titled part of a drawer tab: a <section> named by its heading, so each part is a landmark region. With
// `headingRef`, the heading can take focus (`tabIndex` -1), for when a control inside the part goes away.
import { type ReactNode, type Ref, useId } from "react";
import styles from "./TabSection.module.css";

export interface TabSectionProps {
  heading: string;
  headingRef?: Ref<HTMLHeadingElement>;
  children: ReactNode;
}

export function TabSection({ heading, headingRef, children }: TabSectionProps) {
  const id = useId();
  return (
    <section aria-labelledby={id} className={styles.section}>
      <h3 id={id} ref={headingRef} tabIndex={headingRef === undefined ? undefined : -1} className={styles.heading}>
        {heading}
      </h3>
      {children}
    </section>
  );
}
