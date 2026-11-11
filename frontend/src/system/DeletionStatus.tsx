// Privacy & data's deletions (spec §15.7 "each deletion's status (executing → completed → confirmed offsite)",
// §19.5; plan M5 Task 20, Deviation 22, D18): the newest deletion manifests (`GET /api/privacy/manifests`), read
// again every 10 s in the background while one is not shipped. Each shows its kind and id, when it was asked for,
// and its state: `executing` "Deleting…", `completed` "Completed — not yet confirmed offsite", `shipped`
// "Confirmed offsite", the only state that counts as done. An Adzuna purge lists the golden cases it deleted once
// it has recorded them (by id: the cases are gone), or that it deleted none.
//
// The list survives delete-everything, so after its 202 the deletion shows here; its completion ends the session
// (the next read leads to the login), and after a new login the list finds it again. `ManifestState` is the
// state's badge, which the golden set's requested deletions share.
import { useId } from "react";
import { useTranslation } from "react-i18next";
import { cx } from "../components/cx";
import { Skeleton } from "../components/Skeleton";
import { intlLocale } from "../i18n/index";
import styles from "./DeletionStatus.module.css";
import { useFormat } from "./format";
import { PrivacyProblem } from "./PrivacyActions";
import { type ManifestOut, type ManifestStatus, useManifests } from "./privacyApi";

/** A deletion's state, as a badge: confirmed offsite (done), completed (not yet), deleting. */
export function ManifestState({ status }: { status: ManifestStatus }) {
  const { t } = useTranslation();
  return <span className={cx(styles.state, styles[status])}>{t(`settings_privacy.status.${status}`)}</span>;
}

function DeletedCases({ manifest }: { manifest: ManifestOut }) {
  const { t, i18n } = useTranslation();
  const ids = manifest.deleted_golden_case_ids;
  if (manifest.kind !== "source_purge" || (ids.length === 0 && manifest.status === "executing")) {
    return null; // another kind deletes no case by this list; an executing purge may not have recorded its cases yet
  }
  if (ids.length === 0) {
    return <p className={styles.meta}>{t("settings_privacy.no_deleted_cases")}</p>;
  }
  const list = new Intl.ListFormat(intlLocale(i18n.language), { type: "conjunction" });
  return <p className={styles.cases}>{t("settings_privacy.deleted_cases", { count: ids.length, ids: list.format(ids.map((id) => `#${String(id)}`)) })}</p>;
}

function ManifestItem({ manifest }: { manifest: ManifestOut }) {
  const { t } = useTranslation();
  const format = useFormat();
  return (
    <li className={styles.item}>
      <div className={styles.what}>
        <p className={styles.title}>
          {t("settings_privacy.deletion_title", { kind: t(`settings_privacy.kind.${manifest.kind}`), id: String(manifest.id) })}
        </p>
        <p className={styles.meta}>{t("settings_privacy.requested_at", { time: format.dateTimeMinutes(manifest.created_at) })}</p>
        <DeletedCases manifest={manifest} />
      </div>
      <ManifestState status={manifest.status} />
    </li>
  );
}

export function DeletionStatus() {
  const { t } = useTranslation();
  const id = useId();
  const manifests = useManifests();
  let body;
  if (manifests.isPending) {
    body = <Skeleton variant="line" count={2} />;
  } else if (manifests.isError) {
    body = <PrivacyProblem error={manifests.error} onRetry={() => { void manifests.refetch(); }} />;
  } else if (manifests.data.items.length === 0) {
    body = <p className={styles.empty}>{t("settings_privacy.no_deletions")}</p>;
  } else {
    body = (
      <ul role="list" className={styles.list}>
        {manifests.data.items.map((manifest) => <ManifestItem key={manifest.id} manifest={manifest} />)}
      </ul>
    );
  }
  return (
    <section id="deletion-status" aria-labelledby={id} className={styles.status}>
      <h3 id={id} className={styles.heading}>{t("settings_privacy.deletions")}</h3>
      {body}
    </section>
  );
}
