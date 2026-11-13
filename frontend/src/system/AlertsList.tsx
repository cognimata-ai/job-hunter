// System → Alerts (spec §14.2 L5047-5048, §17.6; plan M4 Task 31, resolution e): Radix Tabs of the open, the
// acknowledged and the resolved alerts, each tab reading `GET /api/alerts?status=<s>` (newest first, Load more).
// An alert shows by its code (`alert.<code>`, the code itself for one the catalogue lacks; never the server's
// message), with its severity, when it was first and last seen, and how often, and its delivery (plan M5 Task 2):
// "Notified <time>" once it was pushed, "Delivery failed n×" once a push of it failed for good. [Acknowledge] on
// an open alert sends `POST /api/alerts/{id}/ack`; the alert leaves the open tab, since every tab and the header's
// status are read again. On the server it stays open, unless its code is resolved by its acknowledgement
// (`llm.reservation_bound_violated`), which then shows under Resolved. [Resolve] on an unresolved alert the user
// resolves (`manual`: its code does not resolve itself) asks first, then sends `POST /api/alerts/{id}/resolve`,
// and every tab is read again; a refusal (404, 409 `not_manual`) shows in the row, and the tabs are read again
// too.
import * as Tabs from "@radix-ui/react-tabs";
import { useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { cx } from "../components/cx";
import { ProblemBox } from "../components/ProblemBox";
import { Skeleton } from "../components/Skeleton";
import { useFocusFallback } from "../components/useFocusFallback";
import { useToast } from "../components/Toaster";
import { useServerText } from "../i18n/index";
import { closedSet } from "../settings/fields";
import fields from "../settings/fields.module.css";
import styles from "./AlertsList.module.css";
import { useFormat } from "./format";
import { LoadMore } from "./lists";
import { type AlertOut, type AlertStatus, useAckAlert, useAlerts, useResolveAlert } from "./systemApi";
import parts from "./SystemSection.module.css";

/** The tabs, in the lifecycle's order (`observability.lists.AlertStatus`). */
const STATUSES = closedSet<AlertStatus>({ open: true, acknowledged: true, resolved: true });

const EMPTY = {
  open: "alerts.empty_open",
  acknowledged: "alerts.empty_acknowledged",
  resolved: "alerts.empty_resolved",
} as const satisfies Record<AlertStatus, string>;

const SEVERITY = { info: styles.info, warn: styles.warn, critical: styles.critical } satisfies Record<AlertOut["severity"], string | undefined>;

/** When the alert was pushed, and how often its push failed for good; nothing for one never pushed. */
function Delivery({ alert }: { alert: AlertOut }) {
  const { t } = useTranslation();
  const format = useFormat();
  if (alert.last_notified_at === null && alert.notify_failures === 0) {
    return null;
  }
  return (
    <p className={styles.meta}>
      {alert.last_notified_at !== null && t("alerts.notified", { time: format.dayMonthTime(alert.last_notified_at) })}
      {alert.last_notified_at !== null && alert.notify_failures > 0 && " · "}
      {alert.notify_failures > 0 && (
        <span className={styles.failed}>{t("alerts.delivery_failed", { value: format.number(alert.notify_failures) })}</span>
      )}
    </p>
  );
}

function AlertItem({ alert }: { alert: AlertOut }) {
  const { t } = useTranslation();
  const serverText = useServerText();
  const format = useFormat();
  const toast = useToast();
  const ack = useAckAlert();
  const resolve = useResolveAlert();
  const [asking, setAsking] = useState(false);
  const title = serverText("alert", alert.code);
  const resolvable = alert.manual && alert.status !== "resolved";
  return (
    <li className={cx(styles.alert, SEVERITY[alert.severity])}>
      <div className={styles.what}>
        <p className={styles.title}>
          <span className={cx(styles.badge, SEVERITY[alert.severity])}>{serverText("alert_severity", alert.severity)}</span>
          <span>{title}</span>
        </p>
        <p className={styles.meta}>
          {t("alerts.seen", { first: format.dayMonthTime(alert.first_at), last: format.dayMonthTime(alert.last_at) })}
          {" · "}
          {t("alerts.times", { count: alert.occurrences, value: format.number(alert.occurrences) })}
        </p>
        <Delivery alert={alert} />
      </div>
      {(alert.status === "open" || resolvable) && (
        <div className={styles.actions}>
          {alert.status === "open" && (
            <button type="button" className={fields.button} disabled={ack.isPending}
              onClick={() => { ack.mutate(alert.id, { onSuccess: () => { toast({ message: t("alerts.acknowledged") }); } }); }}>
              {t("alerts.ack")}
            </button>
          )}
          {resolvable && (
            <button type="button" className={fields.button} disabled={resolve.isPending} onClick={() => { setAsking(true); }}>
              {t("alerts.resolve")}
            </button>
          )}
        </div>
      )}
      {ack.isError && <div className={styles.problem}><ProblemBox error={ack.error} /></div>}
      {resolve.isError && <div className={styles.problem}><ProblemBox error={resolve.error} /></div>}
      <ConfirmDialog open={asking} title={t("alerts.resolve_title")} confirmLabel={t("alerts.resolve")}
        onCancel={() => { setAsking(false); }}
        onConfirm={() => {
          setAsking(false);
          resolve.mutate(alert.id, { onSuccess: () => { toast({ message: t("alerts.resolved") }); } });
        }}>
        <p>{t("alerts.resolve_body", { alert: title })}</p>
      </ConfirmDialog>
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
  const section = useRef<HTMLElement>(null);
  const heading = useRef<HTMLHeadingElement>(null);
  useFocusFallback(section, heading); // an acknowledged or resolved alert leaves its tab with its buttons
  return (
    <section ref={section} id="alerts" aria-labelledby="alerts-heading" className={parts.part}>
      <h3 ref={heading} id="alerts-heading" tabIndex={-1} className={parts.title}>{t("settings_system.alerts")}</h3>
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
