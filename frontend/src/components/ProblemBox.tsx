// The inline problem box (spec §15.8): the problem's translated title (its code, the server's title as the
// fallback), its detail as text where §15.9 shows it (`useServerMessage`: in English, or for a code the
// catalogue lacks), its request id, and [Retry] when the caller can retry.
import { useTranslation } from "react-i18next";
import { NETWORK_CODE, toProblem } from "../api/client";
import { useServerMessage, useServerText } from "../i18n/index";
import styles from "./ProblemBox.module.css";

export interface ProblemBoxProps {
  /** A `ProblemError`, or any other failure (shown as the network problem). */
  error: unknown;
  onRetry?: () => void;
}

export function ProblemBox({ error, onRetry }: ProblemBoxProps) {
  const { t } = useTranslation();
  const serverText = useServerText();
  const serverMessage = useServerMessage();
  const problem = toProblem(error);
  const title = problem.code === NETWORK_CODE ? t("problem_box.network") : serverText("problem", problem.code, problem.message);
  const detail = serverMessage("problem", problem.code, problem.detail);
  return (
    <div role="alert" className={styles.box}>
      <p className={styles.title}>{title}</p>
      {detail !== null && <p className={styles.detail}>{detail}</p>}
      {problem.requestId !== null && (
        <p className={styles.requestId}>{t("problem_box.request_id", { id: problem.requestId })}</p>
      )}
      {onRetry !== undefined && (
        <button type="button" className={styles.retry} onClick={onRetry}>{t("action.retry")}</button>
      )}
    </div>
  );
}
