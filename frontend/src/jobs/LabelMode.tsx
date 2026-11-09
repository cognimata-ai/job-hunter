// Label mode (spec §15.6, §11.13; plan M4 Task 26, Rulings T26-a, T26-c, T26-e and T26-f): the week-1 labelling
// queue in the Jobs list's place, under a one-line note that it ignores the filters: the progress ("23 / 50
// labelled") and a batch of ads to label (`LabelCard`), their scores hidden.
//
// - A batch is taken from the queue's answer, put in a random order once (`shuffle`: the server's order follows
//   the score bands, Ruling T26-e), the jobs labelled in this session after the others, and never reshuffles
//   under the user. Each label reads the queue again, and that answer moves only the progress.
// - Only Next batch replaces the batch (Ruling T26-c): with the latest answer, or with the answer of the read
//   still running when it is pressed (the one that follows the last label). The first new card then has focus,
//   when focus was in Label mode. Once every card is labelled, the batch stays (each card's Reveal score and
//   Change label still work), "Batch done" says so beside Next batch, and focus moves to Next batch.
// - An empty batch is never kept (Ruling T26-f): the next answer with ads is taken as the batch. Meanwhile the
//   empty state offers Check again, and the queue is read again every 60 s in the background.
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
import { type Label, type LabelItemOut, shuffle, useLabellingQueue } from "./labelApi";
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
  /** Counts the batches taken, so a new one is told apart from the last even when its items are the same. */
  no: number;
}

/** A batch of `items`: shuffled, the jobs labelled in this session last (the server adds those only at the end). */
function arrange(items: readonly LabelItemOut[], labels: ReadonlyMap<number, Label>): LabelItemOut[] {
  const shuffled = shuffle(items);
  return [...shuffled.filter((item) => !labels.has(item.job_id)), ...shuffled.filter((item) => labels.has(item.job_id))];
}

export function LabelMode() {
  const { t } = useTranslation();
  const headingId = useId();
  const [batch, setBatch] = useState<Batch | null>(null);
  const { query: queue, checkAgain } = useLabellingQueue(batch !== null && batch.items.length === 0);
  const profile = useProfile();
  const [labels, setLabels] = useState<ReadonlyMap<number, Label>>(() => new Map());
  // the batch whose Next batch waits for the read that is running
  const [nextAfterRead, setNextAfterRead] = useState<number | null>(null);
  const [checking, setChecking] = useState(false);
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
  /** Takes the latest answer as the batch; `moveFocus`: its first card takes focus, when focus was in Label mode. */
  const takeLatest = useCallback(
    (moveFocus: boolean) => {
      if (latest === undefined) {
        return;
      }
      const shown = section.current;
      focusFirst.current = moveFocus && (focusDropped() || (shown !== null && shown.contains(document.activeElement)));
      const taken = arrange(latest.items, labelsNow.current);
      setBatch((current) => ({ items: taken, no: (current?.no ?? -1) + 1 }));
    },
    [latest],
  );

  // the first answer is the first batch; an empty batch takes the next answer that has ads
  useLayoutEffect(() => {
    if (latest === undefined) {
      return;
    }
    if (batch === null) {
      takeLatest(false);
    } else if (batch.items.length === 0 && latest.items.length > 0) {
      takeLatest(true);
    }
  }, [batch, latest, takeLatest]);

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

  // Next batch pressed while the queue is read: the answer of that read is the next batch
  const reading = queue.isFetching;
  useEffect(() => {
    if (nextAfterRead === null || reading) {
      return;
    }
    setNextAfterRead(null);
    if (batchNow.current?.no === nextAfterRead) {
      takeLatest(true);
    }
  }, [nextAfterRead, reading, takeLatest]);
  const nextBatch = () => {
    if (reading && batch !== null) {
      setNextAfterRead(batch.no);
    } else {
      takeLatest(true);
    }
  };

  const onLabelled = useCallback(
    (jobId: number, label: Label, moveFocus: boolean) => {
      const next = new Map(labelsNow.current).set(jobId, label);
      labelsNow.current = next;
      setLabels(next);
      const shown = batchNow.current;
      const at = shown === null ? -1 : shown.items.findIndex((item) => item.job_id === jobId);
      if (shown === null || at === -1 || !moveFocus) {
        return; // another batch is shown by now, or focus is elsewhere
      }
      const rest = [...shown.items.slice(at + 1), ...shown.items.slice(0, at)];
      const to = rest.find((item) => !next.has(item.job_id));
      if (to === undefined) {
        nextButton.current?.focus(); // the batch is done
      } else {
        focusCard(to.job_id);
      }
    },
    [focusCard],
  );

  let content: ReactNode;
  if (batch === null || queue.data === undefined) {
    content =
      queue.data === undefined && queue.isError ? (
        <ProblemBox error={queue.error} onRetry={() => { void queue.refetch(); }} />
      ) : (
        <Skeleton variant="card" count={SKELETON_CARDS} />
      );
  } else {
    const noProfile = profile.data !== undefined && profile.data.active === null;
    const { progress } = queue.data;
    const done = items.every((item) => labels.has(item.job_id));
    content = (
      <>
        <LabelProgress labelled={progress.labelled} target={progress.target} />
        {items.length === 0 ? (
          !noProfile &&
          !profile.isPending && (
            <EmptyState title={t("label_mode.empty")}
              action={
                <button type="button" className={styles.button} aria-disabled={checking}
                  onClick={() => {
                    if (!checking) {
                      setChecking(true);
                      void checkAgain().finally(() => { setChecking(false); });
                    }
                  }}>
                  {checking ? t("label_mode.checking") : t("label_mode.check_again")}
                </button>
              } />
          )
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
              <p role="status" className={styles.done}>{done ? t("label_mode.batch_done") : null}</p>
              <button ref={nextButton} type="button" className={styles.button} aria-busy={nextAfterRead !== null}
                onClick={nextBatch}>
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
