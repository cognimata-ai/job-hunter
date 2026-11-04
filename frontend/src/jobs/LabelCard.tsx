// A card of Label mode (spec §15.6; plan M4 Task 26, resolution c): the ad without its score, so the label has
// no anchor: its title, company and places, and the excerpt of its description (in its language, a paragraph
// per stored line, text only, an ellipsis where the server cut it); then three large buttons, Good · OK · Poor, each with its key, and the optional
// comment (`CommentField`).
//
// - The card is the keyboard target: an <article> in the queue's roving focus (`LabelMode`, `useListKeyboard`).
//   `1`, `2` and `3` label it when focus is on it or on one of its controls, under the list's rule of which keys
//   are not its own (`ignoredKey`): never a key typed in the comment, or one with a modifier held.
// - While the label saves, its buttons are `aria-disabled` (a disabled button would drop the focus it holds) and
//   a press or a key does nothing. A 409 `conflict_state` (no published score to label) says "No score to label
//   yet", any other failure is the problem with Retry (the same label again); the card stays to label.
// - Once saved, the card stays where it is, "Labelled: <label>", with Reveal score and Change label, and
//   `onLabelled` moves focus on (when it was in the card). Reveal score is a disclosure: it reads the job's
//   detail (`useJobDetail`, so nothing is read before it) and shows its score badge, or "No score". Change label
//   shows the buttons again, the label given pressed and focused, and the comment kept: a new label re-posts
//   (the server upserts the case); Cancel goes back.
import { type KeyboardEvent, useId, useLayoutEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { isProblem } from "../api/client";
import { ProblemBox } from "../components/ProblemBox";
import { Skeleton } from "../components/Skeleton";
import { useServerText } from "../i18n/index";
import type { Span } from "./codepoints";
import { CommentField, commentTooLong } from "./CommentField";
import { type FeedbackIn, useJobDetail } from "./detailApi";
import { EvidenceHighlighter } from "./EvidenceHighlighter";
import { type Label, type LabelItemOut, useRecordLabel } from "./labelApi";
import styles from "./LabelCard.module.css";
import { ScoreBadge } from "./ScoreBadge";
import { focusDropped, ignoredKey } from "./useListKeyboard";

/** The labels in their keys' order: `1` is Good, `2` OK, `3` Poor. */
const LABELS: readonly Label[] = ["good", "ok", "poor"];
const KEYS: ReadonlyMap<string, Label> = new Map(LABELS.map((label, i) => [String(i + 1), label]));
const NO_SPANS: readonly Span[] = [];
/** The length of an excerpt the server cut, in code points: `jobs/labelling.py`'s `EXCERPT_CHARS`, which
 * `tests/unit/test_label_excerpt_chars.py` holds this to. */
export const EXCERPT_CHARS = 600;

/** The excerpt as shown: one the server cut ends in an ellipsis. */
function excerptText(excerpt: string): string {
  return Array.from(excerpt).length >= EXCERPT_CHARS ? `${excerpt.trimEnd()}…` : excerpt;
}

/** The `lang` of an ad's text: its language when the UI knows it, else none (the page's applies). */
function textLang(adLanguage: string): "de" | "en" | null {
  return adLanguage === "de" || adLanguage === "en" ? adLanguage : null;
}

export interface LabelCardProps {
  item: LabelItemOut;
  /** The queue's one card in the tab order (roving focus). */
  focused: boolean;
  /** The label given to the job in this session, if any. */
  label: Label | undefined;
  /** A label was saved; `moveFocus` when focus was in the card (or had dropped), so it may move on. */
  onLabelled: (jobId: number, label: Label, moveFocus: boolean) => void;
}

/** The job's score, read once revealed. */
function RevealedScore({ jobId }: { jobId: number }) {
  const { t } = useTranslation();
  const detail = useJobDetail(jobId);
  if (detail.data !== undefined) {
    const { score } = detail.data;
    return score === null ? <p className={styles.muted}>{t("label_mode.no_score")}</p> : <ScoreBadge score={score} />;
  }
  if (detail.isError) {
    return <ProblemBox error={detail.error} onRetry={() => { void detail.refetch(); }} />;
  }
  return <Skeleton variant="line" />;
}

export function LabelCard({ item, focused, label, onLabelled }: LabelCardProps) {
  const { t } = useTranslation();
  const serverText = useServerText();
  const headingId = useId();
  const scoreId = useId();
  const article = useRef<HTMLElement>(null);
  const changeButton = useRef<HTMLButtonElement>(null);
  const { mutation: record, save: saveLabel } = useRecordLabel(item.job_id);
  const [comment, setComment] = useState("");
  const [changing, setChanging] = useState(false);
  const [revealed, setRevealed] = useState(false);
  // where focus goes once the card has switched parts: the label given (Change label), or Change label (Cancel)
  const focusTo = useRef<"label" | "change" | null>(null);
  const choosing = label === undefined || changing;
  const blocked = record.isPending || commentTooLong(comment);

  useLayoutEffect(() => {
    const to = focusTo.current;
    focusTo.current = null;
    if (to === "label") {
      article.current?.querySelector<HTMLElement>(`button[data-label="${label ?? LABELS[0]}"]`)?.focus();
    } else if (to === "change") {
      changeButton.current?.focus();
    }
  }, [changing, label]);

  // once the label is saved: the card shows it, and focus may move on
  const onSaved = (_out: unknown, body: FeedbackIn) => {
    const card = article.current;
    const moveFocus = focusDropped() || (card !== null && card.contains(document.activeElement));
    setChanging(false);
    onLabelled(item.job_id, body.label, moveFocus);
  };
  const save = (value: Label) => {
    if (!blocked) {
      saveLabel(value, comment, { onSuccess: onSaved });
    }
  };
  const onKeyDown = (event: KeyboardEvent<HTMLElement>) => {
    const value = KEYS.get(event.key);
    if (value === undefined || !choosing || ignoredKey(event.nativeEvent)) {
      return;
    }
    event.preventDefault();
    save(value);
  };

  const where = item.locations.join(" · ");
  return (
    <article ref={article} className={styles.card} aria-labelledby={headingId} tabIndex={focused ? 0 : -1}
      onKeyDown={onKeyDown}>
      <h3 id={headingId} className={styles.title}>{item.title}</h3>
      <p className={styles.company}>{item.company}</p>
      {where !== "" && <p className={styles.where}>{where}</p>}
      {item.excerpt !== "" && (
        <EvidenceHighlighter text={excerptText(item.excerpt)} lang={textLang(item.ad_language)} spans={NO_SPANS} />
      )}
      {choosing ? (
        <div className={styles.form}>
          <div role="group" aria-label={t("feedback.label")} aria-busy={record.isPending} className={styles.labels}>
            {LABELS.map((value, i) => (
              <button key={value} type="button" data-label={value} className={styles.choice}
                aria-pressed={label === value} aria-disabled={blocked} aria-keyshortcuts={String(i + 1)}
                onClick={() => { save(value); }}>
                <span className={styles.key} aria-hidden="true">{i + 1}</span>
                {serverText("feedback_label", value)}
              </button>
            ))}
          </div>
          <CommentField value={comment} onChange={setComment} />
          {record.error !== null &&
            (isProblem(record.error, "conflict_state") ? (
              <p role="alert" className={styles.error}>{t("feedback.no_score")}</p>
            ) : (
              <ProblemBox error={record.error} onRetry={() => { record.mutate(record.variables, { onSuccess: onSaved }); }} />
            ))}
          {changing && (
            <button type="button" className={styles.button}
              onClick={() => {
                focusTo.current = "change";
                record.reset();
                setChanging(false);
              }}>
              {t("action.cancel")}
            </button>
          )}
        </div>
      ) : (
        <div className={styles.done}>
          <p className={styles.given}>{t("label_mode.labelled", { label: serverText("feedback_label", label) })}</p>
          <div className={styles.actions}>
            <button type="button" className={styles.button} aria-expanded={revealed} aria-controls={scoreId}
              onClick={() => { setRevealed(!revealed); }}>
              {t("label_mode.reveal")}
              <span aria-hidden="true" className={styles.chevron}>{revealed ? "▴" : "▾"}</span>
            </button>
            <button ref={changeButton} type="button" className={styles.button}
              onClick={() => {
                focusTo.current = "label";
                setChanging(true);
              }}>
              {t("label_mode.change")}
            </button>
          </div>
          <div id={scoreId} className={styles.score}>{revealed && <RevealedScore jobId={item.job_id} />}</div>
        </div>
      )}
    </article>
  );
}
