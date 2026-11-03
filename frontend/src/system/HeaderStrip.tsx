// The header strip (spec §15.1, §15.8; plan M4 Task 21, Ruling c), on every signed-in page, from the
// status (`GET /api/system/status`, read every 60 s in the background):
//
//   Job Hunter · [Jobs] [Profile & Settings] · [Run now ▾] · alerts ⚠<n> · Logout
//   Last run <age> · <status> (<partial causes>) · Next <slot> · JSearch <used>/<limit> (resets <date>)
//   SerpApi <used>/<limit> · Adzuna <used>/<limit> · LLM $<counted> / $<cap> (pace <ok|ahead>) · Coverage gaps <n> · Pending <n>
//
// then the banners. Without a run, line 2 begins "First run at <next slot> — [Run now]". The items wrap at
// their boundaries; the navigation, Run now and Logout work without a status (loading, or a failed read).
import { Link } from "@tanstack/react-router";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { useDisplay } from "../app/AppShell";
import { cx } from "../components/cx";
import { ProblemBox } from "../components/ProblemBox";
import { RelativeTime } from "../components/RelativeTime";
import { Skeleton } from "../components/Skeleton";
import { useServerText } from "../i18n/index";
import { type HeaderQuotaOut, type Provider, quotaOf, type StatusOut, useLogout, useStatus } from "./api";
import { Banners } from "./Banners";
import { useFormat } from "./format";
import styles from "./HeaderStrip.module.css";
import { RunNowPopover } from "./RunNowPopover";

/** Product names, never translated. A member added to `Provider` fails the typecheck here until it is named. */
const PROVIDER_NAMES: Record<Provider, string> = { jsearch: "JSearch", serpapi: "SerpApi", adzuna: "Adzuna" };

function Usage({ window }: { window: HeaderQuotaOut | null }) {
  const { t } = useTranslation();
  const fmt = useFormat();
  return (
    <span className={styles.value}>
      {window === null ? t("value.none") : `${fmt.number(window.used)}/${fmt.number(window.limit)}`}
    </span>
  );
}

function AlertsLink({ alerts }: { alerts: StatusOut["alerts"] }) {
  const { t } = useTranslation();
  const fmt = useFormat();
  const total = fmt.number(alerts.total_unacked);
  const critical = fmt.number(alerts.critical_unacked);
  return (
    <Link to="/settings" hash="system" activeOptions={{ includeHash: true }}
      className={cx(styles.alerts, alerts.critical_unacked > 0 && styles.alertsCritical)}
      aria-label={t("header.alerts_name", { count: alerts.total_unacked, total, critical })}>
      <span>{t("header.alerts")}</span>
      <span className={styles.alertsCount}><span aria-hidden="true">⚠</span>{total}</span>
      {alerts.critical_unacked > 0 && <span>{t("header.alerts_critical", { critical })}</span>}
    </Link>
  );
}

/** Lines 2 and 3. */
function Facts({ status, onRunNow }: { status: StatusOut; onRunNow: () => void }) {
  const { t } = useTranslation();
  const serverText = useServerText();
  const fmt = useFormat();
  const { timezone } = useDisplay();
  const last = status.last_run;
  const next = status.next_slots.at(0); // none while the schedule is off
  const jsearch = quotaOf(status, "jsearch");
  return (
    <div className={styles.facts}>
      <ul role="list" className={styles.line}>
        {last === null ? (
          <li className={styles.item}>
            {next === undefined ? t("header.first_run_none") : t("header.first_run", { slot: fmt.slot(next) })}{" "}
            <button type="button" className={styles.inlineAction} onClick={onRunNow}>{t("run_now.open")}</button>
          </li>
        ) : (
          <>
            <li className={styles.item}>
              {t("header.last_run")}{" "}
              <span className={styles.value}>
                {/* the age the server measured: its now is queued_at + age_s */}
                <RelativeTime value={last.run.queued_at} tz={timezone}
                  now={new Date(Date.parse(last.run.queued_at) + last.age_s * 1000)} />
              </span>
            </li>
            <li className={styles.item}>
              <span className={styles.value}>{serverText("run_status", last.run.status)}</span>
              {last.partial_causes.length > 0 && (
                <>
                  {" "}
                  <span className={styles.causes}>
                    ({last.partial_causes.map((cause) => serverText("partial", cause)).join(", ")})
                  </span>
                </>
              )}
            </li>
            <li className={styles.item}>
              {next === undefined ? t("header.next_none") : (
                <>{t("header.next")} <span className={styles.value}>{fmt.slot(next)}</span></>
              )}
            </li>
          </>
        )}
        <li className={styles.item}>
          {PROVIDER_NAMES.jsearch} <Usage window={jsearch} />
          {jsearch !== null && <> {t("header.resets", { date: fmt.dayMonth(jsearch.reset_at) })}</>}
        </li>
      </ul>
      <ul role="list" className={styles.line}>
        {(["serpapi", "adzuna"] as const).map((provider) => (
          <li key={provider} className={styles.item}>
            {PROVIDER_NAMES[provider]} <Usage window={quotaOf(status, provider)} />
          </li>
        ))}
        {status.llm !== null && (
          <li className={styles.item}>
            {t("header.llm")}{" "}
            <span className={styles.value}>{fmt.usd(status.llm.counted)} / {fmt.usd(status.llm.cap)}</span>{" "}
            {t("header.pace", { pace: serverText("pace", status.llm.pace) })}
          </li>
        )}
        <li className={styles.item}>
          {t("header.coverage_gaps")} <span className={styles.value}>{fmt.number(status.coverage_gaps_open)}</span>
        </li>
        <li className={styles.item}>
          {t("header.pending")} <span className={styles.value}>{fmt.number(status.pending_targets)}</span>
        </li>
      </ul>
    </div>
  );
}

function PowerIcon() {
  return (
    <svg aria-hidden="true" className={styles.icon} viewBox="0 0 16 16" width="14" height="14" fill="none"
      stroke="currentColor" strokeWidth="1.8" strokeLinecap="round">
      <path d="M8 1.75v5.5" />
      <path d="M4.6 3.9a5.25 5.25 0 1 0 6.8 0" />
    </svg>
  );
}

export function HeaderStrip() {
  const { t } = useTranslation();
  const status = useStatus();
  const logout = useLogout();
  const [runNowOpen, setRunNowOpen] = useState(false);
  const data = status.data;
  return (
    <div className={styles.strip}>
      <div className={styles.bar}>
        <span className={styles.brand}>{t("app.name")}</span>
        <nav aria-label={t("nav.label")} className={styles.nav}>
          <ul role="list" className={styles.navList}>
            <li>
              <Link to="/" className={styles.navLink} activeOptions={{ exact: true, includeSearch: false }}>
                {t("nav.jobs")}
              </Link>
            </li>
            <li>
              <Link to="/settings" className={styles.navLink}>{t("nav.settings")}</Link>
            </li>
          </ul>
        </nav>
        <div className={styles.actions}>
          <RunNowPopover status={data} open={runNowOpen} onOpenChange={setRunNowOpen} />
          {data !== undefined && <AlertsLink alerts={data.alerts} />}
          <button type="button" className={styles.logout} disabled={logout.isPending}
            onClick={() => { logout.mutate(undefined); }}>
            <PowerIcon />
            {t("header.logout")}
          </button>
        </div>
      </div>
      {data !== undefined ? (
        <>
          <Facts status={data} onRunNow={() => { setRunNowOpen(true); }} />
          <Banners banners={data.banners} />
        </>
      ) : status.isError ? (
        <div className={styles.problem}>
          <ProblemBox error={status.error} onRetry={() => { void status.refetch(); }} />
        </div>
      ) : (
        <div className={styles.loading}><Skeleton variant="line" /></div>
      )}
    </div>
  );
}
