// The draft's status (spec §12.3, §12.7, §15.7; plan M4 Task 27, resolutions c and d).
//
// `DraftStatus` is the mock's six steps as a stepper (an `ol`, `aria-current="step"` on the current one), each
// change announced politely. "Uploading" is the client's own state while the upload or paste is sent; the
// server's text phase covers both "Extracting text" and "Removing contact details" (`uploaded`,
// `extracting_text`: Ruling T27-a); "Extracting profile" names the active settings' `extract_profile` model,
// verbatim.
//
// `DraftFailure` is a failed draft: its `failure_code` through `draft_failure` (a code outside it: "The CV could
// not be processed" with the code), then its repairs in the server's order, and [Upload another CV]. Paste text
// opens the paste form (a paste opens a new draft, which discards this one); Send the original PDF shows the
// disclosure with a tick it needs; Extract again retries with the same input. Both retries are keyed; a 409
// shows the server's detail.
import { useEffect, useId, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { ProblemBox } from "../../components/ProblemBox";
import { useAnnounce } from "../../components/LiveRegion";
import i18n from "../../i18n/index";
import { type DraftOut, type DraftRepair, type DraftStatusCode, useRetryDraft } from "./api";
import controls from "./controls.module.css";
import styles from "./DraftStatus.module.css";

/** Where a draft is: the client's upload, or the server's status. */
export type DraftPhase = "uploading" | DraftStatusCode;

const STEPS = ["uploading", "extracting_text", "removing_contacts", "review", "extracting_profile", "ready"] as const;
type Step = (typeof STEPS)[number];

/** The steps current in `phase` (none for a failed or ended draft). */
function currentSteps(phase: DraftPhase): Step[] {
  switch (phase) {
    case "uploading":
      return ["uploading"];
    case "uploaded":
    case "extracting_text":
      return ["extracting_text", "removing_contacts"];
    case "text_review":
      return ["review"];
    case "extracting_profile":
      return ["extracting_profile"];
    case "ready":
      return ["ready"];
    default:
      return [];
  }
}

export interface DraftStatusProps {
  phase: DraftPhase;
  /** The active settings' `extract_profile` model. */
  model: string | undefined;
}

export function DraftStatus({ phase, model }: DraftStatusProps) {
  const { t } = useTranslation();
  const announce = useAnnounce();
  const current = currentSteps(phase);
  const first = current.length === 0 ? STEPS.length : STEPS.indexOf(current[0] ?? "ready");
  const label = (step: Step) =>
    step === "extracting_profile" && model !== undefined
      ? t("cv.step.extracting_profile", { model })
      : t(step === "extracting_profile" ? "cv.step.extracting_profile_plain" : `cv.step.${step}`);
  const said = current.map(label).join(", ");
  const previous = useRef(said);
  useEffect(() => {
    if (said !== previous.current && said !== "") {
      announce(t("cv.status.now", { step: said }));
    }
    previous.current = said;
  }, [said, announce, t]);
  return (
    <ol role="list" aria-label={t("cv.status.label")} className={styles.steps}>
      {STEPS.map((step, i) => {
        const now = current.includes(step);
        const done = i < first;
        return (
          <li key={step} aria-current={now ? "step" : undefined}
            className={now ? styles.current : done ? styles.done : styles.todo}>
            <span>{label(step)}</span>
            {done && <span className="visually-hidden">{t("cv.step.done")}</span>}
          </li>
        );
      })}
    </ol>
  );
}

export interface DraftFailureProps {
  draftId: number;
  draft: DraftOut;
  /** The paste repair: the paste form opens. */
  onPaste: () => void;
  /** [Upload another CV]: back to the section without a draft. */
  onStartAgain: () => void;
}

export function DraftFailure({ draftId, draft, onPaste, onStartAgain }: DraftFailureProps) {
  const { t } = useTranslation();
  const retry = useRetryDraft(draftId);
  const [native, setNative] = useState(false);
  const [ack, setAck] = useState(false);
  const titleId = useId();
  const ackId = useId();
  const disclosureId = useId();
  const code = draft.failure_code ?? "";
  const known = i18n.exists(`draft_failure.${code}`);
  const run = (repair: DraftRepair) => {
    if (repair === "paste_text") {
      onPaste();
    } else if (repair === "native_pdf") {
      setNative(true);
    } else if (!retry.isPending) {
      retry.mutate({ mode: "llm" });
    }
  };
  return (
    <div role="group" aria-labelledby={titleId} className={styles.failure}>
      <p id={titleId} className={styles.failureTitle}>{t("cv.failure.title")}</p>
      {known ? (
        <p>{t(`draft_failure.${code}` as "draft_failure.low_text")}</p>
      ) : (
        <p>{t("cv.failure.unknown")} <code className={styles.code}>{code}</code></p>
      )}
      <div className={styles.repairs}>
        {draft.repairs.map((repair) => (
          <button key={repair} type="button" className={repair === draft.repairs[0] ? controls.primary : controls.secondary}
            aria-expanded={repair === "native_pdf" ? native : undefined} disabled={retry.isPending && repair !== "paste_text"}
            onClick={() => { run(repair); }}>
            {t(`draft_repair.${repair}`)}
          </button>
        ))}
        <button type="button" className={controls.secondary} onClick={onStartAgain}>{t("cv.failure.start_again")}</button>
      </div>
      {native && (
        <div className={styles.native}>
          <p id={disclosureId}>{t("cv.native.disclosure")}</p>
          <div className={controls.check}>
            <input id={ackId} type="checkbox" checked={ack} aria-describedby={disclosureId}
              onChange={(event) => { setAck(event.target.checked); }} />
            <label htmlFor={ackId}>{t("cv.failure.native_ack")}</label>
          </div>
          <button type="button" className={controls.primary} disabled={!ack || retry.isPending} aria-busy={retry.isPending}
            onClick={() => { retry.mutate({ mode: "native_pdf", ack_native_pdf: true }); }}>
            {t("cv.failure.native_send")}
          </button>
        </div>
      )}
      {retry.error !== null && <ProblemBox error={retry.error} />}
    </div>
  );
}
