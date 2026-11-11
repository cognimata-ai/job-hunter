// A refusal of a privacy request (plan M5 Task 20, Ruling T20-1 (b)): a `ProblemBox` titled by the problem's code,
// never by the server's own text. An unknown code is the generic "The request could not be carried out"; only a
// code-shaped detail is shown (`purge_in_progress`); `throttled` says the server is busy, or when to try again
// after a lockout, as the Login page reads it (`throttleWait`); the source purge's refusal on
// `payload.sources.enabled` says to enable another source first; no answer at all is the network problem and,
// for a keyed request, that submitting again repeats the same request.
import { useTranslation } from "react-i18next";
import { NETWORK_CODE, type ProblemError } from "../api/client";
import { isTransient } from "../api/queryClient";
import { throttleWait } from "../api/throttle";
import { ProblemBox } from "../components/ProblemBox";
import { useServerText } from "../i18n/index";
import { SOURCES_ENABLED_FIELD } from "./privacyApi";

export interface PrivacyProblemProps {
  error: ProblemError;
  /** A keyed request: after no answer, submitting again repeats the same request. */
  keyed?: boolean;
  onRetry?: () => void;
}

export function PrivacyProblem({ error, keyed = false, onRetry }: PrivacyProblemProps) {
  const { t } = useTranslation();
  const serverText = useServerText();
  let title: string;
  if (error.code === NETWORK_CODE) {
    title = t("problem_box.network");
  } else if (error.code === "throttled") {
    const wait = throttleWait(error);
    if (wait.kind === "busy") {
      title = t("settings_privacy.busy");
    } else if (wait.kind === "minutes") {
      title = t("login.throttled", { minutes: wait.minutes });
    } else {
      title = t("problem.throttled");
    }
  } else if (error.code === "validation_failed" && error.errors.some((e) => e.field === SOURCES_ENABLED_FIELD)) {
    title = t("settings_privacy.enable_another_source");
  } else {
    title = serverText("problem", error.code, t("settings_privacy.error_generic"));
  }
  const note = keyed && isTransient(error) ? t("settings_privacy.retry_same") : undefined;
  return <ProblemBox error={error} title={title} codeDetailOnly note={note} onRetry={onRetry} />;
}
