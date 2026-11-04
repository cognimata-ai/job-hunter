// The CV & profile section (spec §12, §15.7; plan M4 Task 27, resolution b): the first section of Profile &
// Settings.
//
// - The draft shown is the profile's open draft (`open_draft`, so a reload finds it again) or, newer than that
//   or alone, the one this page opened: a `failed` draft is not open, and its failure and repairs stay until the
//   user starts again. A draft read as `confirmed` or `discarded`, or gone (404), reads the profile again and
//   shows the section without a draft.
// - Without a draft: the upload, and the active profile's version and when it was confirmed. While the upload or
//   paste is sent: "Uploading". While the server works: the stepper, the controls disabled. In `text_review`:
//   the text review; in `ready`: the profile review, with the stripped text behind a disclosure; both with the
//   upload, which asks before it replaces them. `failed`: the failure and its repairs.
import { useIsMutating, useQueryClient } from "@tanstack/react-query";
import { type ReactNode, useEffect, useId, useState } from "react";
import { useTranslation } from "react-i18next";
import { ProblemBox } from "../../components/ProblemBox";
import { Skeleton } from "../../components/Skeleton";
import { PROFILE_KEY, useProfile } from "../../profile/api";
import { useFormat } from "../../system/format";
import { CV_UPLOAD_KEY, type DraftOut, isOver, useDraft, useExtractModel } from "./api";
import styles from "./CvSection.module.css";
import { CvUpload } from "./CvUpload";
import { DraftFailure, type DraftPhase, DraftStatus } from "./DraftStatus";
import { ProfileReview } from "./ProfileReview";
import { StripReport, TextReview } from "./TextReview";

export function CvSection() {
  const { t } = useTranslation();
  const format = useFormat();
  const queryClient = useQueryClient();
  const profile = useProfile();
  const model = useExtractModel();
  const uploading = useIsMutating({ mutationKey: CV_UPLOAD_KEY }) > 0;
  // The draft this page opened, and whether its paste form is open (the paste repair opens it).
  const [opened, setOpened] = useState<number | null>(null);
  const [pasteOpen, setPasteOpen] = useState(false);
  const open = profile.data?.open_draft ?? null;
  const draftId = open !== null && (opened === null || open.id > opened) ? open.id : opened;
  const draft = useDraft(draftId);
  const gone = draft.error?.status === 404 || isOver(draft.data?.status);
  useEffect(() => {
    if (gone) {
      setOpened(null);
      void queryClient.invalidateQueries({ queryKey: PROFILE_KEY, exact: true });
    }
  }, [gone, queryClient]);
  const shown: DraftOut | undefined = gone ? undefined : draft.data;
  const status = shown?.status;
  const created = (id: number) => {
    setOpened(id);
    setPasteOpen(false);
  };
  const startAgain = () => {
    setOpened(null);
    setPasteOpen(false);
  };

  if (profile.isPending) {
    return <Skeleton variant="line" count={2} />;
  }
  if (profile.isError) {
    return <ProblemBox error={profile.error} onRetry={() => { void profile.refetch(); }} />;
  }
  const active = profile.data.active;
  const phase: DraftPhase | null = uploading ? "uploading" : status === "failed" ? null : (status ?? null);
  const upload = (
    <CvUpload key={`${String(draftId)}-${String(pasteOpen)}`} openStatus={status ?? null} pasteOpen={pasteOpen}
      onCreated={created} />
  );
  return (
    <div className={styles.section}>
      {active !== null && (status === undefined || status === "failed") && (
        <p className={styles.active}>
          {t("cv.active", { version: active.version_no, date: format.dateTimeMinutes(active.confirmed_at) })}
        </p>
      )}
      {phase !== null && <DraftStatus phase={phase} model={model} />}
      {draftId !== null && !gone && draft.isError && (
        <ProblemBox error={draft.error} onRetry={() => { void draft.refetch(); }} />
      )}
      {draftId !== null && shown !== undefined && status === "failed" && (
        <>
          <DraftFailure draftId={draftId} draft={shown} onPaste={() => { setPasteOpen(true); }} onStartAgain={startAgain} />
          {pasteOpen && upload}
        </>
      )}
      {status !== "failed" && upload}
      {draftId !== null && shown !== undefined && status === "text_review" && (
        <Review title={t("cv.text.title")}>
          <TextReview draftId={draftId} draft={shown} extractUsd={profile.data.estimates.extract_profile_usd} />
        </Review>
      )}
      {draftId !== null && shown !== undefined && status === "ready" && (
        <Review title={t("cv.profile.title")}>
          <ReadyText draftId={draftId} draft={shown} />
          <ProfileReview draftId={draftId} draft={shown} onPaste={() => { setPasteOpen(true); }} />
        </Review>
      )}
    </div>
  );
}

function Review({ title, children }: { title: string; children: ReactNode }) {
  const headingId = useId();
  return (
    <section aria-labelledby={headingId} className={styles.review}>
      <h3 id={headingId} className={styles.reviewTitle}>{title}</h3>
      {children}
    </section>
  );
}

/** A ready draft's strip report, and its stripped text behind "Show stripped text": an edit there reopens the
 * text review. */
function ReadyText({ draftId, draft }: { draftId: number; draft: DraftOut }) {
  const { t } = useTranslation();
  return (
    <>
      <StripReport report={draft.strip_report} />
      <details className={styles.text}>
        <summary className={styles.textSummary}>{t("cv.text.show")}</summary>
        <TextReview draftId={draftId} draft={draft} extractUsd={undefined} report={false} />
      </details>
    </>
  );
}
