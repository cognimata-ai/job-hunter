// System → Golden set (spec §11.13 L4610-4632, §14.2 L4978-4979, §15.7; plan M4 Task 31, resolution e): the
// golden cases by id, keyset-paged with Load more (`GET /api/golden-cases`). Each row shows its title and company
// (the labelled ad's, `title` and `company`, Task 15), its label, its source (a synthetic case of the eval seed
// says so), and its active and canary toggles; each toggle sends `PATCH {active}` or `PATCH {is_canary}` alone
// and is disabled while it runs (a third canary is refused, 422, shown at the row). [Delete] asks in a
// `ConfirmDialog`, whose confirm sends `DELETE` once and is busy until it is answered (plan M5 Task 20): its 202
// opens a deletion manifest the worker carries out (Deviation 20), so the row says "Deletion requested" instead of
// Delete while the case is still listed, and the requested deletions below the table show each manifest's state
// (`ManifestState`), read every 10 s until confirmed offsite; once the deletion has run, the set is read again,
// so the deleted case leaves the table. A requested case's flags rest. A refusal shows at the row by its code
// (`PrivacyProblem`). Every write reads the set and the golden runs' gate again.
import { useQueryClient } from "@tanstack/react-query";
import { Fragment, useEffect, useId, useState } from "react";
import { useTranslation } from "react-i18next";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { cx } from "../components/cx";
import { useAnnounce } from "../components/LiveRegion";
import { ProblemBox } from "../components/ProblemBox";
import { Skeleton } from "../components/Skeleton";
import { useServerText } from "../i18n/index";
import { GOLDEN_RUNS_KEY } from "../settings/EvalStatus";
import tables from "../settings/tables.module.css";
import { ManifestState } from "./DeletionStatus";
import styles from "./GoldenSet.module.css";
import { LoadMore } from "./lists";
import { PrivacyProblem } from "./PrivacyProblem";
import { useManifest, useSingleFlight } from "./privacyApi";
import { GOLDEN_KEY, type GoldenCaseOut, useDeleteGoldenCase, useGoldenCases, useSetGoldenFlags } from "./systemApi";
import parts from "./SystemSection.module.css";

const COLUMNS = 7;

/** A deletion the user asked for: the case (its title, as it is gone once deleted) and its manifest. */
interface Requested {
  caseId: number;
  title: string;
  manifestId: number;
}

function CaseRow({ golden, requested, onRequested }: { golden: GoldenCaseOut; requested: boolean; onRequested: (manifestId: number) => void }) {
  const { t } = useTranslation();
  const serverText = useServerText();
  const announce = useAnnounce();
  const flags = useSetGoldenFlags();
  const remove = useDeleteGoldenCase();
  const send = useSingleFlight(remove);
  const [asking, setAsking] = useState(false);
  const busy = flags.isPending || remove.isPending || requested;
  return (
    <Fragment>
      <tr>
        <td data-label={t("golden.col_title")} className={cx(tables.text, tables.wide)}>{golden.title}</td>
        <td data-label={t("golden.col_company")}>{golden.company}</td>
        <td data-label={t("golden.col_label")}>{serverText("feedback_label", golden.label)}</td>
        <td data-label={t("golden.col_source")}>{serverText("golden_source", golden.source)}</td>
        <td data-label={t("golden.col_active")}>
          <input type="checkbox" className={styles.toggle} checked={golden.active} disabled={busy}
            aria-label={t("golden.active_for", { title: golden.title })}
            onChange={(event) => { flags.mutate({ caseId: golden.id, flags: { active: event.target.checked } }); }} />
        </td>
        <td data-label={t("golden.col_canary")}>
          <input type="checkbox" className={styles.toggle} checked={golden.is_canary} disabled={busy}
            aria-label={t("golden.canary_for", { title: golden.title })} aria-describedby="golden-canary-hint"
            onChange={(event) => { flags.mutate({ caseId: golden.id, flags: { is_canary: event.target.checked } }); }} />
        </td>
        <td>
          {requested ? (
            <span className={styles.requested}>{t("golden.deletion_requested")}</span>
          ) : (
            <button type="button" className={tables.action} disabled={busy} onClick={() => { setAsking(true); }}>
              {t("golden.delete")}<span className="visually-hidden"> {golden.title}</span>
            </button>
          )}
          <ConfirmDialog open={asking} title={t("golden.delete_title")} confirmLabel={t("golden.delete")} busy={remove.isPending}
            onCancel={() => { if (!remove.isPending) { setAsking(false); } }}
            onConfirm={() => {
              send(golden.id, {
                onSuccess: (out) => {
                  setAsking(false);
                  onRequested(out.manifest_id);
                  announce(t("golden.deletion_requested"));
                },
                onError: () => { setAsking(false); },
              });
            }}>
            <p>{t("golden.delete_body", { title: golden.title, company: golden.company })}</p>
          </ConfirmDialog>
        </td>
      </tr>
      {(flags.error !== null || remove.error !== null) && (
        <tr className={tables.detail}>
          <td colSpan={COLUMNS} className={tables.wide}>
            {flags.error !== null && <ProblemBox error={flags.error} />}
            {remove.error !== null && <PrivacyProblem error={remove.error} />}
          </td>
        </tr>
      )}
    </Fragment>
  );
}

function RequestedItem({ requested }: { requested: Requested }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const manifest = useManifest(requested.manifestId);
  const status = manifest.data?.status;
  const ran = status !== undefined && status !== "executing";
  useEffect(() => {
    if (ran) {
      void queryClient.invalidateQueries({ queryKey: GOLDEN_KEY });
      void queryClient.invalidateQueries({ queryKey: GOLDEN_RUNS_KEY });
    }
  }, [ran, queryClient]);
  return (
    <li className={styles.requestedItem}>
      <strong className={styles.requestedTitle}>{requested.title}</strong>
      <span>{t("golden.deletion_requested")}</span>
      {manifest.data !== undefined && <ManifestState status={manifest.data.status} />}
      {manifest.isError && <PrivacyProblem error={manifest.error} onRetry={() => { void manifest.refetch(); }} />}
    </li>
  );
}

/** The deletions asked for here, each with its manifest's state. */
function RequestedDeletions({ requested }: { requested: Requested[] }) {
  const { t } = useTranslation();
  const id = useId();
  return (
    <section aria-labelledby={id} className={styles.deletions}>
      <h4 id={id} className={styles.deletionsTitle}>{t("golden.deletions")}</h4>
      <ul role="list" className={styles.requestedList}>
        {requested.map((item) => <RequestedItem key={item.manifestId} requested={item} />)}
      </ul>
    </section>
  );
}

export function GoldenSet() {
  const { t } = useTranslation();
  const cases = useGoldenCases();
  const [requested, setRequested] = useState<Requested[]>([]);
  const request = (golden: GoldenCaseOut) => (manifestId: number) => {
    setRequested((list) => [{ caseId: golden.id, title: golden.title, manifestId }, ...list.filter((r) => r.manifestId !== manifestId)]);
  };
  let body;
  if (cases.items === undefined) {
    body = cases.error === null ? <Skeleton variant="line" count={3} /> : <ProblemBox error={cases.error} onRetry={cases.retry} />;
  } else if (cases.items.length === 0) {
    body = <p className={parts.empty}>{t("golden.empty")}</p>;
  } else {
    body = (
      <>
        <div className={tables.scroll}>
          <table className={tables.table} aria-labelledby="golden-heading">
            <thead>
              <tr>
                <th scope="col">{t("golden.col_title")}</th>
                <th scope="col">{t("golden.col_company")}</th>
                <th scope="col">{t("golden.col_label")}</th>
                <th scope="col">{t("golden.col_source")}</th>
                <th scope="col">{t("golden.col_active")}</th>
                <th scope="col">{t("golden.col_canary")}</th>
                <th scope="col"><span className="visually-hidden">{t("golden.col_actions")}</span></th>
              </tr>
            </thead>
            <tbody>
              {cases.items.map((golden) => (
                <CaseRow key={golden.id} golden={golden} requested={requested.some((r) => r.caseId === golden.id)}
                  onRequested={request(golden)} />
              ))}
            </tbody>
          </table>
        </div>
        <p id="golden-canary-hint" className={tables.note}>{t("golden.canary_hint")}</p>
        <LoadMore list={cases} />
      </>
    );
  }
  return (
    <section id="golden-set" aria-labelledby="golden-heading" className={parts.part}>
      <h3 id="golden-heading" className={parts.title}>{t("settings_system.golden")}</h3>
      {body}
      {requested.length > 0 && <RequestedDeletions requested={requested} />}
    </section>
  );
}
