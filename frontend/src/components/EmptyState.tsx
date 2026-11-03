// An empty list or section (spec §15.8): what is missing, and what to do about it.
import type { ReactNode } from "react";
import styles from "./EmptyState.module.css";

export interface EmptyStateProps {
  title: string;
  children?: ReactNode;
  /** The way forward: a link or a button. */
  action?: ReactNode;
}

export function EmptyState({ title, children, action }: EmptyStateProps) {
  return (
    <div className={styles.empty}>
      <p className={styles.title}>{title}</p>
      {children !== undefined && <div className={styles.body}>{children}</div>}
      {action !== undefined && <div className={styles.action}>{action}</div>}
    </div>
  );
}
