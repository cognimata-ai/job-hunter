// The Feedback tab (spec §15.4, §11.13; plan M4 Task 24, resolution f): "wrong score" with the label the user
// expected, Good · OK · Poor, and an optional comment of at most 2,000 characters, counted as the api counts them
// (code points, not UTF-16 units), with the count shown. It posts `{kind: "wrong_score", label, comment?}`; a 409
// (no published score to label) says "No score to label yet" in the form. Below, the job's feedback newest first,
// as served, each with Delete (no confirmation: the golden case it made stays, §11.13). Both writes read the
// detail again. A comment is the user's own text, shown as text.
import { type SyntheticEvent, useId, useState } from "react";
import { useTranslation } from "react-i18next";
import { isProblem } from "../api/client";
import { ProblemBox } from "../components/ProblemBox";
import { useToast } from "../components/Toaster";
import { useServerText } from "../i18n/index";
import { useFormat } from "../system/format";
import { type FeedbackIn, type JobFeedbackOut, useDeleteFeedback, useRecordFeedback } from "./detailApi";
import styles from "./FeedbackTab.module.css";
import { TabSection } from "./TabSection";

type Label = FeedbackIn["label"];

/** The longest comment, in code points (`api/routes/golden.py`'s `Comment`). */
export const COMMENT_MAX = 2000;
const LABELS: readonly Label[] = ["good", "ok", "poor"];

function FeedbackForm({ jobId }: { jobId: number }) {
  const { t } = useTranslation();
  const serverText = useServerText();
  const format = useFormat();
  const toast = useToast();
  const record = useRecordFeedback(jobId);
  const [label, setLabel] = useState<Label | null>(null);
  const [comment, setComment] = useState("");
  const headingId = useId();
  const groupId = useId();
  const commentId = useId();
  const countId = useId();
  const hintId = useId();
  const length = Array.from(comment).length;
  const tooLong = length > COMMENT_MAX;
  const max = format.number(COMMENT_MAX);
  const submit = (event: SyntheticEvent) => {
    event.preventDefault();
    if (label === null || tooLong || record.isPending) {
      return;
    }
    const text = comment.trim();
    record.mutate(
      { kind: "wrong_score", label, ...(text === "" ? {} : { comment: text }) },
      {
        onSuccess: () => {
          toast({ message: t("feedback.sent") });
          setLabel(null);
          setComment("");
        },
      },
    );
  };
  return (
    <form aria-labelledby={headingId} className={styles.form} onSubmit={submit} noValidate>
      <h3 id={headingId} className={styles.heading}>{t("feedback.title")}</h3>
      <p id={groupId} className={styles.label}>{t("feedback.label")}</p>
      <div role="radiogroup" aria-labelledby={groupId} className={styles.labels}>
        {LABELS.map((value) => (
          <label key={value} className={styles.choice}>
            <input type="radio" name={`feedback-${String(jobId)}`} value={value} checked={label === value}
              onChange={() => { setLabel(value); }} />
            {serverText("feedback_label", value)}
          </label>
        ))}
      </div>
      <label htmlFor={commentId} className={styles.label}>{t("feedback.comment")}</label>
      <textarea id={commentId} className={styles.comment} rows={3} value={comment} aria-invalid={tooLong}
        aria-describedby={tooLong ? `${countId} ${hintId}` : countId}
        onChange={(event) => { setComment(event.target.value); }} />
      <p id={countId} className={styles.count}>{t("feedback.count", { count: format.number(length), max })}</p>
      {tooLong && <p id={hintId} className={styles.error}>{t("feedback.too_long", { max })}</p>}
      {record.error !== null &&
        (isProblem(record.error, "conflict_state") ? (
          <p role="alert" className={styles.error}>{t("feedback.no_score")}</p>
        ) : (
          <ProblemBox error={record.error} />
        ))}
      <button type="submit" className={styles.submit} disabled={label === null || tooLong || record.isPending}
        aria-busy={record.isPending}>
        {t("feedback.submit")}
      </button>
    </form>
  );
}

function FeedbackList({ jobId, feedback }: { jobId: number; feedback: readonly JobFeedbackOut[] }) {
  const { t } = useTranslation();
  const serverText = useServerText();
  const format = useFormat();
  const toast = useToast();
  const remove = useDeleteFeedback(jobId);
  return (
    <TabSection heading={t("feedback.list")}>
      {feedback.length === 0 ? (
        <p className={styles.muted}>{t("feedback.none")}</p>
      ) : (
        <ul role="list" className={styles.list} aria-label={t("feedback.list")}>
          {feedback.map((item) => (
            <li key={item.id} className={styles.item}>
              <p>
                <span className={styles.given}>{serverText("feedback_label", item.label)}</span>
                {" · "}
                <time dateTime={item.created_at}>{format.dateTimeMinutes(item.created_at)}</time>
              </p>
              {item.comment !== null && <p className={styles.text}>{item.comment}</p>}
              <button type="button" className={styles.delete} disabled={remove.isPending}
                onClick={() => {
                  remove.mutate(item.id, { onSuccess: () => { toast({ message: t("feedback.deleted") }); } });
                }}>
                {t("feedback.delete")}
              </button>
            </li>
          ))}
        </ul>
      )}
      {remove.error !== null && <ProblemBox error={remove.error} />}
    </TabSection>
  );
}

export function FeedbackTab({ jobId, feedback }: { jobId: number; feedback: readonly JobFeedbackOut[] }) {
  return (
    <div className={styles.tab}>
      <FeedbackForm jobId={jobId} />
      <FeedbackList jobId={jobId} feedback={feedback} />
    </div>
  );
}
