// The inline problem box (spec §15.8): the problem's translated title (its code, the server's title as the
// fallback), its detail as `ProblemDetail` shows it, its request id, and [Retry] when the caller can retry. A
// caller that never shows the server's own text (the privacy pages, Ruling T20-1 (b): `system/PrivacyProblem`)
// gives its own `title` and `codeDetailOnly`, and may add a `note` of its own.
import { useTranslation } from "react-i18next";
import { NETWORK_CODE, toProblem } from "../api/client";
import { useServerMessage, useServerText } from "../i18n/index";
import styles from "./ProblemBox.module.css";

export interface ProblemBoxProps {
  /** A `ProblemError`, or any other failure (shown as the network problem). */
  error: unknown;
  onRetry?: () => void;
  /** The caller's own reading of the problem, in place of its code's translation or the server's title. */
  title?: string;
  /** The detail only when it is code-shaped, never the server's sentence. */
  codeDetailOnly?: boolean;
  /** A line of the caller's own, under the detail. */
  note?: string;
}

// A detail that is a code (`not_releasable`, `purge_in_progress`), not a sentence.
const CODE_SHAPED = /^[a-z][a-z0-9_]*$/;

/** A problem's `detail` as §15.9 shows it (Rulings F4-b, F4-c): a code-shaped one verbatim as `<code>` in every
 * language, a sentence where `useServerMessage` lets it (in English, or for a code the catalogue lacks); else
 * nothing. */
export function ProblemDetail({ code, detail, className, codeOnly = false }: {
  code: string;
  detail: string | null;
  className?: string;
  /** Only a code-shaped detail is shown. */
  codeOnly?: boolean;
}) {
  const serverMessage = useServerMessage();
  if (detail !== null && CODE_SHAPED.test(detail)) {
    return <p className={className}><code>{detail}</code></p>;
  }
  if (codeOnly) {
    return null;
  }
  const shown = serverMessage("problem", code, detail);
  return shown === null ? null : <p className={className}>{shown}</p>;
}

export function ProblemBox({ error, onRetry, title, codeDetailOnly = false, note }: ProblemBoxProps) {
  const { t } = useTranslation();
  const serverText = useServerText();
  const problem = toProblem(error);
  const shownTitle =
    title ?? (problem.code === NETWORK_CODE ? t("problem_box.network") : serverText("problem", problem.code, problem.message));
  return (
    <div role="alert" className={styles.box}>
      <p className={styles.title}>{shownTitle}</p>
      <ProblemDetail code={problem.code} detail={problem.detail} className={styles.detail} codeOnly={codeDetailOnly} />
      {note !== undefined && <p className={styles.detail}>{note}</p>}
      {problem.requestId !== null && (
        <p className={styles.requestId}>{t("problem_box.request_id", { id: problem.requestId })}</p>
      )}
      {onRetry !== undefined && (
        <button type="button" className={styles.retry} onClick={onRetry}>{t("action.retry")}</button>
      )}
    </div>
  );
}
