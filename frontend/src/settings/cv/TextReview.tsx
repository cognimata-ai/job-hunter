// The text review (spec §12.3, §15.7; plan M4 Task 27, resolution f, Ruling T27-e).
//
// - The strip report ("Removed: 1 name · 1 e-mail") is `strip_report`'s counts through `contact_category`.
// - `stripped_text_preview` is rendered once, as text, cut at the review map's boundaries and into paragraphs at
//   each "\n" (kept as the paragraph's last character, so the container's preview text is the preview itself):
//   a `replacement` is a <mark> of its token, titled with its category, a `removed` run a <mark> of
//   `[REMOVED]`; each restorable one is followed by its [Restore] button. Never HTML.
// - [Mark as contact] removes the selected text: the selection's boundaries are summed over the container's
//   preview text nodes (`data-preview`; a TreeWalker, so its buttons are not counted), converted to code points
//   and mapped to the raw text (`segments.ts`). Its mousedown is prevented, so pressing it keeps the selection.
//   Marking uses the native selection, which keyboard users reach with caret browsing; restoring is a button
//   (Ruling T27-e).
// - Every edit sends the complete set (`PUT …/text`) and reads the draft again; a 422 says "This edit could not
//   be applied" and the stored set stays.
// - A stored set the rules no longer apply (`segments` empty, `edits` not) is sent again (`useReapply`), without
//   the entries a 422 `no_replacement` names. In `text_review` that happens once per draft load by itself; in
//   `ready` only when the user asks (the section's [Re-apply the removals], behind a confirmation: re-sending
//   reopens the review and discards the extracted profile, Ruling T27-f). Until then marking and restoring are
//   disabled. A re-send that fails otherwise shows its problem and [Try again].
// - Each mark carries its category as a visually hidden prefix, outside the preview's text, so the offsets do not
//   count it (ARIA gives a <mark> no name of its own).
// - In `text_review`, [Approve text & extract profile (~$…)] sends the keyed approval, which starts the paid
//   extraction; in `ready` a text edit reopens the review (the server discards the extracted profile).
import { Fragment, type ReactNode, useCallback, useEffect, useId, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { isProblem, type ProblemError, toProblem } from "../../api/client";
import { cx } from "../../components/cx";
import { ProblemBox } from "../../components/ProblemBox";
import { useFormat } from "../../system/format";
import { type DraftOut, useApproveText, useEditText } from "./api";
import {
  editSetOf,
  MAX_SPAN_EDITS,
  type RawSpan,
  type SegmentOut,
  type SpanEdit,
  toCodePoints,
  toRawRange,
  withRemoval,
  withRestore,
} from "./segments";
import controls from "./controls.module.css";
import styles from "./TextReview.module.css";

type Category = NonNullable<SegmentOut["category"]>;

/** The strip report's order: the mock's (name, e-mail, phone, address), then the rest. */
const REPORT_ORDER = Object.keys({
  name: true,
  email: true,
  phone: true,
  address: true,
  url: true,
  personal: true,
} satisfies Record<Category, true>) as Category[];

const SPAN_EDIT_FIELD = /^body\.span_edits\.(\d+)(?:\.|$)/;

/** The positions of the set that a 422 names with `code`. */
function refusedEntries(error: ProblemError, code: string): Set<number> {
  const refused = new Set<number>();
  for (const e of error.errors) {
    const match = SPAN_EDIT_FIELD.exec(e.field);
    if (match !== null && e.code === code) {
      refused.add(Number(match[1]));
    }
  }
  return refused;
}

/** The UTF-16 offset in the preview of the DOM point `(node, offset)`: the lengths of the container's preview
 * text nodes before it, plus the offset inside one. */
function previewOffset(container: Node, node: Node, offset: number): number {
  const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT, {
    acceptNode: (n) =>
      n.parentElement?.closest("[data-preview]") === null ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT,
  });
  let sum = 0;
  for (let text = walker.nextNode(); text !== null; text = walker.nextNode()) {
    if (text === node) {
      return sum + offset;
    }
    const whole = document.createRange();
    whole.selectNodeContents(text);
    if (whole.comparePoint(node, offset) <= 0) {
      return sum;
    }
    sum += (text as Text).length;
  }
  return sum;
}

/** The raw span of the current selection inside `container`, or `null` (none, empty, or outside it). */
function selectedSpan(container: HTMLElement | null, preview: string, segments: readonly SegmentOut[]): RawSpan | null {
  const selection = window.getSelection();
  if (container === null || selection === null || selection.rangeCount === 0 || selection.isCollapsed) {
    return null;
  }
  const range = selection.getRangeAt(0);
  if (!container.contains(range.startContainer) || !container.contains(range.endContainer)) {
    return null;
  }
  const start = toCodePoints(preview, previewOffset(container, range.startContainer, range.startOffset));
  const end = toCodePoints(preview, previewOffset(container, range.endContainer, range.endOffset));
  return toRawRange(segments, start, end);
}

interface Piece {
  segment: SegmentOut | null;
  text: string;
}

/** The preview cut into paragraphs at each "\n" (kept at the paragraph's end), each a list of pieces of one
 * segment (a whole text when there is no map). */
function paragraphs(preview: string, segments: readonly SegmentOut[]): Piece[][] {
  const points = Array.from(preview);
  const runs: Piece[] = segments.length === 0
    ? [{ segment: null, text: preview }]
    : segments.map((s) => ({ segment: s, text: points.slice(s.stripped_start, s.stripped_end).join("") }));
  const found: Piece[][] = [[]];
  for (const run of runs) {
    const lines = run.text.split("\n");
    lines.forEach((line, i) => {
      const last = i === lines.length - 1;
      const text = last ? line : `${line}\n`;
      if (text !== "") {
        found[found.length - 1]?.push({ segment: run.segment, text });
      }
      if (!last) {
        found.push([]);
      }
    });
  }
  return found.filter((paragraph, i) => paragraph.length > 0 || i < found.length - 1);
}

export interface Reapply {
  /** The stored set no longer applies: no map, and edits to send again. */
  stale: boolean;
  running: boolean;
  /** The last re-send failed otherwise than by naming refused entries (no answer, a 5xx, another refusal). */
  failure: ProblemError | null;
  run: () => void;
}

/**
 * The re-send of a stored set the rules no longer apply (`api/routes/profile_review.py`'s last paragraph): the
 * stored set, then, after a 422 that names entries `no_replacement`, the set without them. One at a time; the
 * draft is read again after the one that succeeds.
 */
export function useReapply(draftId: number, draft: DraftOut): Reapply {
  const { mutateAsync } = useEditText(draftId);
  const [running, setRunning] = useState(false);
  const [failure, setFailure] = useState<ProblemError | null>(null);
  const inFlight = useRef(false);
  const { segments, edits } = draft;
  const stale = segments.length === 0 && edits.restored.length + edits.removed.length > 0;
  const run = useCallback(() => {
    if (inFlight.current) {
      return;
    }
    inFlight.current = true;
    setRunning(true);
    setFailure(null);
    const set = editSetOf(edits);
    void (async () => {
      try {
        await mutateAsync(set);
      } catch (error) {
        const problem = toProblem(error);
        const refused = refusedEntries(problem, "no_replacement");
        if (refused.size === 0) {
          setFailure(problem);
        } else {
          await mutateAsync(set.filter((_, i) => !refused.has(i))).catch((again: unknown) => {
            setFailure(toProblem(again));
          });
        }
      } finally {
        inFlight.current = false;
        setRunning(false);
      }
    })();
  }, [edits, mutateAsync]);
  return { stale, running, failure, run };
}

/** "Removed: 1 name · 1 e-mail …", from the strip counts. */
export function StripReport({ report }: { report: DraftOut["strip_report"] }) {
  const { t } = useTranslation();
  const parts = REPORT_ORDER.filter((c) => (report?.[c] ?? 0) > 0).map((c) =>
    t(`contact_category.${c}`, { count: report?.[c] ?? 0 }),
  );
  const text = parts.length === 0 ? t("cv.text.removed_none") : t("cv.text.removed", { list: parts.join(" · ") });
  return <p className={styles.report}>{text}</p>;
}

export interface TextReviewProps {
  draftId: number;
  draft: DraftOut;
  /** What one profile extraction costs (`estimates.extract_profile_usd`), shown on the approval. */
  extractUsd: string | undefined;
  /** Show the strip report above the text (the profile review shows it itself). */
  report?: boolean;
}

export function TextReview({ draftId, draft, extractUsd, report = true }: TextReviewProps) {
  const { t } = useTranslation();
  const format = useFormat();
  const edit = useEditText(draftId);
  const approve = useApproveText(draftId);
  const container = useRef<HTMLDivElement>(null);
  const hintId = useId();
  const preview = draft.stripped_text_preview ?? "";
  const { segments, edits } = draft;
  const [selected, setSelected] = useState<RawSpan | null>(null);
  const [notice, setNotice] = useState<"refused" | "too_many" | null>(null);
  const reapply = useReapply(draftId, draft);
  const { stale, run: reapplyNow } = reapply;
  const reapplied = useRef<number | null>(null);
  const inReview = draft.status === "text_review";

  const readSelection = useCallback(() => {
    setSelected(selectedSpan(container.current, preview, segments));
  }, [preview, segments]);
  useEffect(() => {
    readSelection();
    document.addEventListener("selectionchange", readSelection);
    return () => { document.removeEventListener("selectionchange", readSelection); };
  }, [readSelection]);

  // In text review, a stored set the rules no longer apply is sent again by itself, once per draft load.
  useEffect(() => {
    if (inReview && stale && reapplied.current !== draftId) {
      reapplied.current = draftId;
      reapplyNow();
    }
  }, [inReview, stale, draftId, reapplyNow]);

  const send = (set: SpanEdit[]) => {
    if (edit.isPending) {
      return;
    }
    if (set.length > MAX_SPAN_EDITS) {
      setNotice("too_many");
      return;
    }
    setNotice(null);
    edit.mutate(set, {
      onSuccess: () => { window.getSelection()?.removeAllRanges(); },
      onError: (error) => { setNotice(isProblem(error, "validation_failed") ? "refused" : null); },
    });
  };
  const markSelection = () => {
    const span = selectedSpan(container.current, preview, segments);
    if (span !== null) {
      send(withRemoval(editSetOf(edits), span));
    }
  };
  const restore = (segment: SegmentOut) => {
    send(withRestore(editSetOf(edits), segment));
  };

  const piece = (p: Piece, key: number): ReactNode => {
    const s = p.segment;
    if (s === null || s.kind === "text") {
      return <span key={key} data-preview="">{p.text}</span>;
    }
    if (s.kind === "removed") {
      return (
        <Fragment key={key}>
          <mark className={styles.removed} title={t("cv.text.removed_mark")}>
            <span className="visually-hidden">{t("cv.text.removed_sr")}{" "}</span>
            <span data-preview="">{p.text}</span>
          </mark>
          <button type="button" className={styles.restore} aria-label={t("cv.text.restore_removed")}
            disabled={edit.isPending || stale} onClick={() => { restore(s); }}>
            <span aria-hidden="true">↺</span>
          </button>
        </Fragment>
      );
    }
    const category = t(`contact_category.${s.category ?? "personal"}`);
    return (
      <Fragment key={key}>
        <mark className={styles.replacement}
          title={s.restorable ? t("cv.text.replaced", { category }) : t("cv.text.not_restorable", { category })}>
          <span className="visually-hidden">{t("cv.text.mark_sr", { category })}{" "}</span>
          <span data-preview="">{p.text}</span>
        </mark>
        {s.restorable && (
          <button type="button" className={styles.restore} aria-label={t("cv.text.restore", { category })}
            disabled={edit.isPending || stale} onClick={() => { restore(s); }}>
            <span aria-hidden="true">↺</span>
          </button>
        )}
      </Fragment>
    );
  };

  const ready = draft.status === "ready";
  const usd = extractUsd === undefined ? undefined : format.usd(extractUsd);
  return (
    <div className={styles.review}>
      {report && <StripReport report={draft.strip_report} />}
      {inReview && (stale || reapply.running) && (
        <div className={styles.stale}>
          <p role="status" className={styles.note}>{t("cv.text.reapplying")}</p>
          {reapply.failure !== null && (
            <button type="button" className={controls.secondary} disabled={reapply.running} onClick={reapplyNow}>
              {t("cv.text.reapply_again")}
            </button>
          )}
        </div>
      )}
      {inReview && reapply.failure !== null && <ProblemBox error={reapply.failure} />}
      <div ref={container} className={styles.text} data-testid="cv-text">
        {paragraphs(preview, segments).map((paragraph, i) => (
          <p key={i} className={cx(styles.line, paragraph.every((p) => p.text.trim() === "") && styles.blank)}>
            {paragraph.map(piece)}
          </p>
        ))}
      </div>
      <div className={styles.tools}>
        <button type="button" className={controls.secondary} disabled={selected === null || edit.isPending || stale}
          aria-describedby={hintId} aria-busy={edit.isPending}
          onMouseDown={(event) => { event.preventDefault(); }} onClick={markSelection}>
          {t("cv.text.mark")}
        </button>
        <p id={hintId} className={styles.hint}>
          {stale ? t("cv.text.stale_hint") : selected === null ? t("cv.text.select_first") : t("cv.text.mark_hint")}
        </p>
      </div>
      {notice === "refused" && <p role="alert" className={controls.error}>{t("cv.text.refused")}</p>}
      {notice === "too_many" && (
        <p role="alert" className={controls.error}>{t("cv.text.too_many", { max: format.number(MAX_SPAN_EDITS) })}</p>
      )}
      {edit.error !== null && !isProblem(edit.error, "validation_failed") && <ProblemBox error={edit.error} />}
      {ready ? (
        <p className={styles.note}>{t("cv.text.reopens")}</p>
      ) : (
        <div className={styles.approve}>
          <p className={styles.note}>{t("cv.text.best_effort")}</p>
          <button type="button" className={controls.primary} disabled={approve.isPending || edit.isPending || stale}
            aria-busy={approve.isPending}
            onClick={() => { if (!approve.isPending) { approve.mutate(undefined); } }}>
            {usd === undefined ? t("cv.text.approve_plain") : t("cv.text.approve", { usd })}
          </button>
          {approve.error !== null && <ProblemBox error={approve.error} />}
        </div>
      )}
    </div>
  );
}
