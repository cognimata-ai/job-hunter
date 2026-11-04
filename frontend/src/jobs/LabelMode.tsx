// Label mode (spec §15.6, §11.13; plan M4 Task 26, Ruling T26-a): the week-1 labelling queue in the Jobs list's
// place, under a one-line note that it ignores the filters: the progress ("23 / 50 labelled") and a batch of
// ads to label (`LabelCard`), their scores hidden.
//
// - The batch is a snapshot of the queue's items when Label mode opens (`labelApi`); it never reshuffles under
//   the user. Each label reads the queue again, and that answer moves only the progress. The batch is replaced
//   by the latest answer's items when the user presses Next batch, or by the answer that follows its last label
//   (focus waits on Next batch meanwhile); the first new card then has focus, when focus was in Label mode.
// - A labelled card stays in place with its label. The labels given in this session (`job_id → label`) outlive
//   a batch: the server fills a batch with labelled ads once the unlabelled ones run out, and those show theirs.
// - The cards are a list with `useListKeyboard`'s roving focus and `j`/`k`, but Enter opens nothing (the drawer
//   would show the score). After a label, focus moves to the next card still to label, round from the last card
//   to the first, else to Next batch.
// - While the first answer is read: three skeleton cards; its failure: the problem with Retry. An empty queue
//   says "Nothing to label yet — labels need scored jobs", unless no profile is active: then the Jobs page's
//   no-profile notice says what to do.
import { type ReactNode, useCallback, useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { EmptyState } from "../components/EmptyState";
import { ProblemBox } from "../components/ProblemBox";
import { Skeleton } from "../components/Skeleton";
import { useProfile } from "../profile/api";
import { LabelCard } from "./LabelCard";
import { type Label, type LabelItemOut, useLabellingQueue } from "./labelApi";
import styles from "./LabelMode.module.css";
import { LabelProgress } from "./LabelProgress";
import { focusDropped, useListKeyboard } from "./useListKeyboard";

const SKELETON_CARDS = 3;
const NO_ITEMS: readonly LabelItemOut[] = [];

/** Enter on a card opens nothing. */
function openNothing(): void {
  // the drawer would show the score
}

interface Batch {
  items: readonly LabelItemOut[];
  /** Counts the batches shown, so a new one is told apart from the last even when its items are the same. */
  no: number;
}

export function LabelMode() {
  const { t } = useTranslation();
  const headingId = useId();
  const queue = useLabellingQueue();
  const profile = useProfile();
  const [batch, setBatch] = useState<Batch | null>(null);
  if (batch === null && queue.data !== undefined) {
    setBatch({ items: queue.data.items, no: 0 }); // the first answer, before anything is shown
  }
  const [labels, setLabels] = useState<ReadonlyMap<number, Label>>(() => new Map());
  // the batch whose last label waits for the queue's next answer to replace it
  const [swapAfterRead, setSwapAfterRead] = useState<number | null>(null);
  const items = batch?.items ?? NO_ITEMS;
  const ids = useMemo(() => items.map((item) => item.job_id), [items]);
  const { cardProps, itemProps, headingRef, focusCard } = useListKeyboard(ids, openNothing);
  const section = useRef<HTMLElement>(null);
  const nextButton = useRef<HTMLButtonElement>(null);
  // the latest labels and batch, for the callbacks of a label saved after a later render
  const labelsNow = useRef(labels);
  const batchNow = useRef(batch);
  useLayoutEffect(() => {
    batchNow.current = batch;
  }, [batch]);
  const focusFirst = useRef(false);

  const latest = queue.data;
  const replaceBatch = useCallback(() => {
    if (latest === undefined) {
      return;
    }
    const shown = section.current;
    focusFirst.current = focusDropped() || (shown !== null && shown.contains(document.activeElement));
    setBatch((current) => ({ items: latest.items, no: (current?.no ?? 0) + 1 }));
  }, [latest]);

  useLayoutEffect(() => {
    if (!focusFirst.current) {
      return;
    }
    focusFirst.current = false;
    const first = ids.at(0);
    if (first === undefined) {
      headingRef.current?.focus(); // an empty batch: Next batch has gone
    } else {
      focusCard(first);
    }
  }, [batch, ids, focusCard, headingRef]);

  // the batch's last label read the queue again: once that read is over, its answer is the next batch
  const reading = queue.isFetching;
  useEffect(() => {
    if (swapAfterRead === null || reading) {
      return;
    }
    setSwapAfterRead(null);
    if (batchNow.current?.no === swapAfterRead) {
      replaceBatch();
    }
  }, [swapAfterRead, reading, replaceBatch]);

  const onLabelled = useCallback(
    (jobId: number, label: Label, moveFocus: boolean) => {
      const before = labelsNow.current;
      const next = new Map(before).set(jobId, label);
      labelsNow.current = next;
      setLabels(next);
      const shown = batchNow.current;
      const at = shown === null ? -1 : shown.items.findIndex((item) => item.job_id === jobId);
      if (shown === null || at === -1) {
        return; // another batch is shown by now
      }
      const rest = [...shown.items.slice(at + 1), ...shown.items.slice(0, at)];
      const to = rest.find((item) => !next.has(item.job_id));
      if (to !== undefined) {
        if (moveFocus) {
          focusCard(to.job_id);
        }
        return;
      }
      if (moveFocus) {
        nextButton.current?.focus();
      }
      if (!before.has(jobId)) {
        setSwapAfterRead(shown.no); // the batch's last label: every card is labelled
      }
    },
    [focusCard],
  );

  let content: ReactNode;
  if (queue.data === undefined) {
    content = queue.isError ? (
      <ProblemBox error={queue.error} onRetry={() => { void queue.refetch(); }} />
    ) : (
      <Skeleton variant="card" count={SKELETON_CARDS} />
    );
  } else {
    const noProfile = profile.data !== undefined && profile.data.active === null;
    const { progress } = queue.data;
    content = (
      <>
        <LabelProgress labelled={progress.labelled} target={progress.target} />
        {items.length === 0 ? (
          !noProfile && !profile.isPending && <EmptyState title={t("label_mode.empty")} />
        ) : (
          <>
            <ol role="list" className={styles.list} aria-label={t("label_mode.queue")}>
              {items.map((item) => (
                <li key={item.job_id} {...itemProps(item.job_id)}>
                  <LabelCard item={item} focused={cardProps(item.job_id).focused} label={labels.get(item.job_id)}
                    onLabelled={onLabelled} />
                </li>
              ))}
            </ol>
            <div className={styles.next}>
              <button ref={nextButton} type="button" className={styles.button} onClick={replaceBatch}>
                {t("label_mode.next_batch")}
              </button>
            </div>
          </>
        )}
      </>
    );
  }
  return (
    <section ref={section} className={styles.mode} aria-labelledby={headingId}>
      <div className={styles.head}>
        <h2 id={headingId} ref={headingRef} tabIndex={-1} className={styles.heading}>{t("label_mode.title")}</h2>
        <p className={styles.note}>{t("label_mode.ignores_filters")}</p>
      </div>
      {content}
    </section>
  );
}
