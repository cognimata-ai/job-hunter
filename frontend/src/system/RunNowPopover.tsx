// Run now (spec §15.1; plan M4 Task 21, Rulings f and g): a non-modal Radix Popover with "Include paid
// sources (uses ~N JSearch units)" and Start (`POST /api/runs`, keyed). While a run is active the same button
// reads "Run in progress (view)" and opens the run's report in the drawer instead, polled every 10 s.
//
// Non-modal only: the modal Popover mounts react-remove-scroll, which injects a <style> element that the CSP
// (`style-src 'self'`) refuses. Radix positions the content through React's `style` prop (the CSSOM, which
// the CSP allows); this code sets no style itself.
//
// The trigger stays one element in both states (a click on it while a run is active opens the drawer and not
// the popover), so the drawer gives the focus back to it even after the run has ended and it reads Run now.
import * as Popover from "@radix-ui/react-popover";
import { useEffect, useId, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { cx } from "../components/cx";
import { Drawer } from "../components/Drawer";
import { useAnnounce } from "../components/LiveRegion";
import { ProblemBox } from "../components/ProblemBox";
import { Skeleton } from "../components/Skeleton";
import { useServerText } from "../i18n/index";
import { type RunNowIn, type RunStatus, type StatusOut, useRun, useRunNow } from "./api";
import { useFormat } from "./format";
import styles from "./RunNowPopover.module.css";
import { RunReport } from "./RunReport";

export interface RunNowPopoverProps {
  /** The header status, `undefined` until it is read (Run now works without it). */
  status: StatusOut | undefined;
  /** The popover's state, which the header owns: line 2's [Run now] opens this same popover. */
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

/** The drawer's body: the run, polled while it is active, and each change of its status announced once. */
function RunPanel({ runId, titleId }: { runId: number; titleId: string }) {
  const { t } = useTranslation();
  const announce = useAnnounce();
  const serverText = useServerText();
  const run = useRun(runId);
  const status = run.data?.run.status;
  const previous = useRef<RunStatus | undefined>(undefined);
  useEffect(() => {
    if (status === undefined) {
      return;
    }
    if (previous.current !== undefined && previous.current !== status) {
      announce(t("run_report.announce", { id: String(runId), status: serverText("run_status", status) }));
    }
    previous.current = status;
  }, [status, runId, announce, t, serverText]);
  return (
    <div className={styles.panel}>
      <h2 id={titleId} className={styles.title}>{t("run_report.title", { id: String(runId) })}</h2>
      {run.data !== undefined ? (
        <RunReport run={run.data.run} report={run.data.report} partialCauses={run.data.partial_causes} />
      ) : run.isError ? (
        <ProblemBox error={run.error} onRetry={() => { void run.refetch(); }} />
      ) : (
        <Skeleton variant="drawer" />
      )}
    </div>
  );
}

export function RunNowPopover({ status, open, onOpenChange }: RunNowPopoverProps) {
  const { t } = useTranslation();
  const announce = useAnnounce();
  const fmt = useFormat();
  const runNow = useRunNow();
  const [includePaid, setIncludePaid] = useState(false);
  const [reportRunId, setReportRunId] = useState<number | null>(null);
  const checkboxId = useId();
  const titleId = useId();
  const activeRunId = status?.active_run_id ?? null;
  const units = status?.run_now.jsearch_units;

  const start = (vars: RunNowIn) => {
    runNow.mutate(vars, {
      onSuccess: ({ created }) => {
        onOpenChange(false);
        announce(t(created ? "run_now.started" : "run_now.already_running"));
      },
    });
  };

  return (
    <>
      <Popover.Root open={open && activeRunId === null}
        onOpenChange={(next) => {
          if (next && !runNow.isPending) {
            runNow.reset(); // a new visit starts without the last failure; its key is kept by the action
          }
          onOpenChange(next);
        }}>
        <Popover.Trigger asChild>
          <button type="button" className={cx(styles.trigger, activeRunId !== null && styles.active)}
            onClick={(event) => {
              if (activeRunId !== null) {
                event.preventDefault(); // not the popover: the active run's report
                setReportRunId(activeRunId);
              }
            }}>
            {activeRunId !== null ? t("run_now.in_progress") : (
              <>{t("run_now.open")} <span aria-hidden="true" className={styles.caret}>▾</span></>
            )}
          </button>
        </Popover.Trigger>
        <Popover.Portal>
          <Popover.Content className={styles.popover} align="end" sideOffset={6} collisionPadding={16}
            aria-label={t("run_now.title")}>
            <form className={styles.form}
              onSubmit={(event) => {
                event.preventDefault();
                start({ include_paid: includePaid });
              }}>
              <div className={styles.check}>
                <input id={checkboxId} type="checkbox" className={styles.box} checked={includePaid}
                  onChange={(event) => { setIncludePaid(event.target.checked); }} />
                <label htmlFor={checkboxId}>
                  {units === undefined
                    ? t("run_now.include_paid_unknown")
                    : t("run_now.include_paid", { units: fmt.number(units) })}
                </label>
              </div>
              <button type="submit" className={styles.start} disabled={runNow.isPending}>
                {runNow.isPending ? t("run_now.starting") : t("run_now.start")}
              </button>
              {runNow.isError && (
                <ProblemBox error={runNow.error}
                  onRetry={() => {
                    start(runNow.variables); // the same action: its key again
                  }} />
              )}
            </form>
          </Popover.Content>
        </Popover.Portal>
      </Popover.Root>
      <Drawer open={reportRunId !== null} onClose={() => { setReportRunId(null); }} titleId={titleId}>
        {reportRunId !== null && <RunPanel runId={reportRunId} titleId={titleId} />}
      </Drawer>
    </>
  );
}
