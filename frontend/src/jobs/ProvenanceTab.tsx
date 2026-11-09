// The Provenance tab (spec §15.4, §8.8; plan M4 Task 24, resolution f): the job's postings, each with its own
// link out; which observation supplied each canonical field, and by which rule; and the merge events into or from
// the job, oldest first. A merge into this job that is still reversible offers Unmerge, which asks first (naming
// the merged job), then posts `{merge_event_id}`: its success says so and reads the jobs again, its refusal (a
// 409: reversed already, or no longer the latest state) is shown under the events. The field names and the rules
// are the server's, verbatim in a <code> (no single backend set to translate them from).
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { OutboundLink } from "../components/OutboundLink";
import { ProblemBox } from "../components/ProblemBox";
import { useToast } from "../components/Toaster";
import { useServerText } from "../i18n/index";
import { useFormat } from "../system/format";
import { type JobDetailOut, type MergeEventOut, useUnmerge } from "./detailApi";
import { availabilityText } from "./format";
import styles from "./ProvenanceTab.module.css";
import { TabSection } from "./TabSection";

function Postings({ detail }: { detail: JobDetailOut }) {
  const { t } = useTranslation();
  const serverText = useServerText();
  const format = useFormat();
  return (
    <TabSection heading={t("provenance.postings")}>
      <table className={styles.table}>
        <thead>
          <tr>
            <th scope="col">{t("provenance.col.source")}</th>
            <th scope="col">{t("provenance.col.native_id")}</th>
            <th scope="col">{t("provenance.col.first_seen")}</th>
            <th scope="col">{t("provenance.col.last_seen")}</th>
            <th scope="col">{t("provenance.col.availability")}</th>
            <th scope="col">{t("provenance.col.link")}</th>
          </tr>
        </thead>
        <tbody>
          {detail.postings.map((posting) => (
            <tr key={posting.id}>
              <th scope="row">{serverText("source", posting.source_id)}</th>
              <td><code className={styles.code}>{posting.native_id}</code></td>
              <td>{format.dateTimeMinutes(posting.first_seen_at)}</td>
              <td>{format.dateTimeMinutes(posting.last_seen_at)}</td>
              <td>{availabilityText(posting.availability, t)}</td>
              <td>
                {posting.link === null ? (
                  t("drawer.no_value")
                ) : (
                  <OutboundLink href={posting.link}>{t("provenance.posting_link")}</OutboundLink>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </TabSection>
  );
}

function Fields({ detail }: { detail: JobDetailOut }) {
  const { t } = useTranslation();
  const serverText = useServerText();
  const format = useFormat();
  return (
    <TabSection heading={t("provenance.fields")}>
      <table className={styles.table}>
        <thead>
          <tr>
            <th scope="col">{t("provenance.col.field")}</th>
            <th scope="col">{t("provenance.col.source")}</th>
            <th scope="col">{t("provenance.col.rule")}</th>
            <th scope="col">{t("provenance.col.observed")}</th>
          </tr>
        </thead>
        <tbody>
          {Object.entries(detail.job.field_provenance).map(([field, entry]) => (
            <tr key={field}>
              <th scope="row"><code className={styles.code}>{field}</code></th>
              <td>{entry.source_id === null ? t("drawer.no_value") : serverText("source", entry.source_id)}</td>
              <td><code className={styles.code}>{entry.rule}</code></td>
              <td>{format.dateTimeMinutes(entry.observed_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </TabSection>
  );
}

function Merges({ detail }: { detail: JobDetailOut }) {
  const { t } = useTranslation();
  const serverText = useServerText();
  const format = useFormat();
  const toast = useToast();
  const jobId = detail.job.job_id;
  const unmerge = useUnmerge(jobId);
  const [confirming, setConfirming] = useState<MergeEventOut | null>(null);
  const merged = confirming === null ? "" : String(confirming.from_job_id);
  return (
    <TabSection heading={t("provenance.merges")}>
      {detail.merge_events.length === 0 ? (
        <p className={styles.muted}>{t("provenance.no_merges")}</p>
      ) : (
        <table className={styles.table}>
          <thead>
            <tr>
              <th scope="col">{t("provenance.col.kind")}</th>
              <th scope="col">{t("provenance.col.jobs")}</th>
              <th scope="col">{t("provenance.col.when")}</th>
              <th scope="col"><span className="visually-hidden">{t("provenance.col.action")}</span></th>
            </tr>
          </thead>
          <tbody>
            {detail.merge_events.map((event) => (
              <tr key={event.id}>
                <th scope="row">{serverText("merge_kind", event.kind)}</th>
                <td>{t("provenance.merge_jobs", { from: String(event.from_job_id), into: String(event.into_job_id) })}</td>
                <td>{format.dateTimeMinutes(event.created_at)}</td>
                <td>
                  {event.reversible && event.into_job_id === jobId && (
                    <button type="button" className={styles.button}
                      onClick={() => {
                        unmerge.reset();
                        setConfirming(event);
                      }}>
                      {t("provenance.unmerge")}
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {unmerge.error !== null && <ProblemBox error={unmerge.error} />}
      <ConfirmDialog open={confirming !== null} title={t("provenance.unmerge_title", { job: merged })}
        confirmLabel={t("provenance.unmerge")} busy={unmerge.isPending}
        onCancel={() => { setConfirming(null); }}
        onConfirm={() => {
          if (confirming === null) {
            return;
          }
          const from = String(confirming.from_job_id);
          unmerge.mutate(confirming.id, {
            onSuccess: () => { toast({ message: t("provenance.unmerged", { job: from }) }); },
            onSettled: () => { setConfirming(null); },
          });
        }}>
        {t("provenance.unmerge_body", { job: merged })}
      </ConfirmDialog>
    </TabSection>
  );
}

export function ProvenanceTab({ detail }: { detail: JobDetailOut }) {
  return (
    <div className={styles.tab}>
      <Postings detail={detail} />
      <Fields detail={detail} />
      <Merges detail={detail} />
    </div>
  );
}
