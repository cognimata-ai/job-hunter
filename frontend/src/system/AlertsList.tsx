// System → Alerts (spec §14.2 L5047-5048, §17.6; plan M4 Task 31, resolution e): Radix Tabs of the open, the
// acknowledged and the resolved alerts, each tab reading `GET /api/alerts?status=<s>` (newest first, Load more).
// An alert shows by its code (`alert.<code>`, the code itself for one the catalogue lacks; never the server's
// message), with its severity, when it was first and last seen, and how often. [Acknowledge] on an open alert
// sends `POST /api/alerts/{id}/ack`; the alert stays open on the server but leaves the open tab, since every tab
// and the header's status are read again.
import * as Tabs from "@radix-ui/react-tabs";
import { useTranslation } from "react-i18next";
import { cx } from "../components/cx";
import { ProblemBox } from "../components/ProblemBox";
import { Skeleton } from "../components/Skeleton";
import { useToast } from "../components/Toaster";
import { useServerText } from "../i18n/index";
import { closedSet } from "../settings/fields";
import fields from "../settings/fields.module.css";
import styles from "./AlertsList.module.css";
import { useFormat } from "./format";
import { LoadMore } from "./RunsList";
import parts from "./SystemSection.module.css";
import { type AlertOut, type AlertStatus, useAckAlert, useAlerts } from "./systemApi";

/** The tabs, in the lifecycle's order (`observability.lists.AlertStatus`). */
const STATUSES = closedSet<AlertStatus>({ open: true, acknowledged: true, resolved: true });

const EMPTY = {
  open: "alerts.empty_open",
  acknowledged: "alerts.empty_acknowledged",
  resolved: "alerts.empty_resolved",
} as const satisfies Record<AlertStatus, string>;

const SEVERITY = { info: styles.info, warn: styles.warn, critical: styles.critical } satisfies Record<AlertOut["severity"], string | undefined>;

function AlertItem({ alert }: { alert: AlertOut }) {
  const { t } = useTranslation();
  const serverText = useServerText();
  const format = useFormat();
  const toast = useToast();
  const ack = useAckAlert();
  return (
    <li className={cx(styles.alert, SEVERITY[alert.severity])}>
      <div className={styles.what}>
        <p className={styles.title}>
          <span className={cx(styles.badge, SEVERITY[alert.severity])}>{serverText("alert_severity", alert.severity)}</span>
          <span>{serverText("alert", alert.code)}</span>
        </p>
        <p className={styles.meta}>
          {t("alerts.seen", { first: format.dayMonthTime(alert.first_at), last: format.dayMonthTime(alert.last_at) })}
          {" · "}
          {t("alerts.times", { count: alert.occurrences, value: format.number(alert.occurrences) })}
        </p>
      </div>
      {alert.status === "open" && (
        <div className={styles.actions}>
          <button type="button" className={fields.button} disabled={ack.isPending}
            onClick={() => { ack.mutate(alert.id, { onSuccess: () => { toast({ message: t("alerts.acknowledged") }); } }); }}>
            {t("alerts.ack")}
          </button>
        </div>
      )}
      {ack.isError && <div className={styles.problem}><ProblemBox error={ack.error} /></div>}
    </li>
  );
}

function AlertTab({ status }: { status: AlertStatus }) {
  const { t } = useTranslation();
  const alerts = useAlerts(status);
  if (alerts.items === undefined) {
    return alerts.error === null ? <Skeleton variant="line" count={2} /> : <ProblemBox error={alerts.error} onRetry={alerts.retry} />;
  }
  if (alerts.items.length === 0) {
    return <p className={parts.empty}>{t(EMPTY[status])}</p>;
  }
  return (
    <>
      <ul role="list" className={styles.list}>
        {alerts.items.map((alert) => <AlertItem key={alert.id} alert={alert} />)}
      </ul>
      <LoadMore list={alerts} />
    </>
  );
}

export function AlertsList() {
  const { t } = useTranslation();
  const serverText = useServerText();
  return (
    <section id="alerts" aria-labelledby="alerts-heading" className={parts.part}>
      <h3 id="alerts-heading" className={parts.title}>{t("settings_system.alerts")}</h3>
      <Tabs.Root defaultValue="open" className={styles.tabs}>
        <Tabs.List aria-label={t("settings_system.alerts_tabs")} className={styles.tabList}>
          {STATUSES.map((status) => (
            <Tabs.Trigger key={status} value={status} className={styles.tab}>{serverText("alert_status", status)}</Tabs.Trigger>
          ))}
        </Tabs.List>
        {STATUSES.map((status) => (
          <Tabs.Content key={status} value={status} className={styles.panel}><AlertTab status={status} /></Tabs.Content>
        ))}
      </Tabs.Root>
    </section>
  );
}
