// The optional comment of a label (spec §15.4, §15.6; plan M4 Tasks 24 and 26): at most 2,000 characters,
// counted as the api counts them (code points, not UTF-16 units), with the count shown; past the limit the field
// is invalid and says so. Its label, field, count and hint are the parent's grid items (a fragment), as the
// Feedback tab's form and a label card lay them out.
import { useId } from "react";
import { useTranslation } from "react-i18next";
import { useFormat } from "../system/format";
import styles from "./CommentField.module.css";

/** The longest comment, in code points (`api/routes/golden.py`'s `Comment`). */
export const COMMENT_MAX = 2000;

/** Whether a comment is longer than the api takes. */
export function commentTooLong(text: string): boolean {
  return Array.from(text).length > COMMENT_MAX;
}

export interface CommentFieldProps {
  value: string;
  onChange: (value: string) => void;
}

export function CommentField({ value, onChange }: CommentFieldProps) {
  const { t } = useTranslation();
  const format = useFormat();
  const commentId = useId();
  const countId = useId();
  const hintId = useId();
  const length = Array.from(value).length;
  const tooLong = length > COMMENT_MAX;
  const max = format.number(COMMENT_MAX);
  return (
    <>
      <label htmlFor={commentId} className={styles.label}>{t("feedback.comment")}</label>
      <textarea id={commentId} className={styles.comment} rows={3} value={value} aria-invalid={tooLong}
        aria-describedby={tooLong ? `${countId} ${hintId}` : countId}
        onChange={(event) => { onChange(event.target.value); }} />
      <p id={countId} className={styles.count}>{t("feedback.count", { count: format.number(length), max })}</p>
      {tooLong && <p id={hintId} className={styles.error}>{t("feedback.too_long", { max })}</p>}
    </>
  );
}
