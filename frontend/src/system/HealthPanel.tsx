// System → Health (spec §17.5; plan M4 Task 31, resolution e): `GET /api/system/health` as it gives it, in
// definition lists: readiness (with the embedder and the worker's tracing state, plan M5 Task 7), the runs and the
// collection's freshness, the backups (the latest set, the backup age state, the latest offsite manifest shipment,
// the manifests not shipped yet and the latest restore test with its age state; M5 Task 7), the worker's queues,
// the score lane, the month's LLM spend, the quota windows, the open alerts, the sizes (disk, database, tables) and
// the versions. Ages and sizes use `Intl.NumberFormat`'s units (`useUnits`); versions and the embedder's model id
// are shown verbatim in `<code>`, as are the table names (identifiers). Every closed set goes through its
// namespace: `readiness`, `run_status` (a backup set's and a restore test's status too), `run_age_state`,
// `backup_age_state`, `restore_test_age_state`, `queue`, `queue_status`, `pause`, `window_state`,
// `alert_severity`. A `warn` or `critical` age state, and a failed set or restore test, take their tone.
import type { TFunction } from "i18next";
import { type ReactNode, useId } from "react";
import { useTranslation } from "react-i18next";
import type { components } from "../api/schema";
import { cx } from "../components/cx";
import { ProblemBox } from "../components/ProblemBox";
import { Skeleton } from "../components/Skeleton";
import { intlLocale, useServerText } from "../i18n/index";
import { PROVIDERS } from "./api";
import { useFormat } from "./format";
import styles from "./HealthPanel.module.css";
import parts from "./SystemSection.module.css";
import { type SystemHealthOut, useHealth } from "./systemApi";
import { useUnits } from "./units";

type Versions = components["schemas"]["Versions"];
type Severity = keyof SystemHealthOut["alerts_open"];
type Backups = SystemHealthOut["backups"];
type BackupRun = components["schemas"]["BackupRunOut"];
type Tone = "ok" | "warn" | "critical";

/** The largest tables shown before "All n tables". */
const TOP_TABLES = 5;

const VERSION_LABELS = {
  app: "health.version.app",
  python: "health.version.python",
  fastapi: "health.version.fastapi",
  sqlalchemy: "health.version.sqlalchemy",
  psycopg: "health.version.psycopg",
  procrastinate: "health.version.procrastinate",
  langgraph: "health.version.langgraph",
} as const satisfies Record<keyof Versions, string>;

const SEVERITIES = ["critical", "warn", "info"] as const satisfies readonly Severity[];

function Group({ title, children }: { title: string; children: ReactNode }) {
  const id = useId();
  return (
    <section aria-labelledby={id} className={styles.group}>
      <h4 id={id} className={styles.groupTitle}>{title}</h4>
      <dl className={styles.list}>{children}</dl>
    </section>
  );
}

function Entry({ term, children, tone }: { term: ReactNode; children: ReactNode; tone?: Tone }) {
  return (
    <div className={styles.entry}>
      <dt className={styles.term}>{term}</dt>
      <dd className={cx(styles.value, tone !== undefined && styles[tone])}>{children}</dd>
    </div>
  );
}

function ago(t: TFunction, age: number | null, duration: (s: number) => string): string {
  return age === null ? t("value.none") : t("health.ago", { age: duration(age) });
}

/** The tone of an age state: `warn` and `critical` their own, any other none. */
function stateTone(state: string): Tone | undefined {
  return state === "critical" ? "critical" : state === "warn" ? "warn" : undefined;
}

/** The tracing state the worker published: off until it published one. */
function tracing(t: TFunction, state: SystemHealthOut["langsmith"]): string {
  if (state === null || !state.tracing) {
    return t("health.tracing_off");
  }
  return state.region === null ? t("health.tracing_on") : t("health.tracing_on_region", { region: state.region.toUpperCase() });
}

function BackupsGroup({ backups }: { backups: Backups }) {
  const { t } = useTranslation();
  const serverText = useServerText();
  const format = useFormat();
  const units = useUnits();
  const run = (r: BackupRun | null) => {
    if (r === null) {
      return t("value.none");
    }
    const status = serverText("run_status", r.status);
    return r.age_s === null
      ? t("health.backup_unfinished", { id: String(r.id), status })
      : t("health.backup_value", { id: String(r.id), status, age: units.duration(r.age_s) });
  };
  const runTone = (r: BackupRun | null): Tone | undefined => (r?.status === "failed" ? "critical" : undefined);
  const shipment = backups.latest_shipment;
  const oldest = backups.oldest_unshipped_completed_at;
  return (
    <Group title={t("health.backups")}>
      <Entry term={t("health.latest_set")} tone={runTone(backups.latest_set)}>{run(backups.latest_set)}</Entry>
      <Entry term={t("health.backup_age")} tone={stateTone(backups.backup_age_state)}>
        {serverText("backup_age_state", backups.backup_age_state)}
      </Entry>
      <Entry term={t("health.latest_shipment")}>
        {shipment === null
          ? t("value.none")
          : t("health.shipment_value", {
              id: String(shipment.id),
              count: shipment.manifests,
              value: format.number(shipment.manifests),
              time: format.dayMonthTime(shipment.shipped_at),
            })}
      </Entry>
      <Entry term={t("health.unshipped")}>
        {oldest === null
          ? format.number(backups.unshipped_manifests)
          : t("health.unshipped_value", { value: format.number(backups.unshipped_manifests), time: format.dayMonthTime(oldest) })}
      </Entry>
      <Entry term={t("health.latest_restore_test")} tone={runTone(backups.latest_restore_test)}>
        {run(backups.latest_restore_test)}
      </Entry>
      <Entry term={t("health.restore_test_age")} tone={stateTone(backups.restore_test_age_state)}>
        {serverText("restore_test_age_state", backups.restore_test_age_state)}
      </Entry>
    </Group>
  );
}

function Health({ health }: { health: SystemHealthOut }) {
  const { t, i18n } = useTranslation();
  const serverText = useServerText();
  const format = useFormat();
  const units = useUnits();
  const month = new Intl.DateTimeFormat(intlLocale(i18n.language), { month: "long", year: "numeric", timeZone: "UTC" });
  const { ready, embedder, scoring, llm } = health;
  const tables = Object.entries(health.table_bytes).sort(([a, x], [b, y]) => y - x || a.localeCompare(b));
  const tableRow = ([name, bytes]: [string, number]) => (
    <Entry key={name} term={<code className={styles.code}>{name}</code>}>{units.bytes(bytes)}</Entry>
  );
  return (
    <div className={styles.groups}>
      <Group title={t("health.readiness")}>
        <Entry term={t("health.state")} tone={ready.ok ? "ok" : "critical"}>
          {ready.ok
            ? t("health.ready")
            : t("health.not_ready", { failing: ready.failing.map((code) => serverText("readiness", code)).join(", ") })}
        </Entry>
        <Entry term={t("health.embedder")} tone={embedder.ok ? undefined : "critical"}>
          {embedder.ok ? t("health.embedder_ok") : t("health.embedder_down")}
          {embedder.checksum_ok !== null && <> · {embedder.checksum_ok ? t("health.checksum_ok") : t("health.checksum_failed")}</>}
        </Entry>
        <Entry term={t("health.embedder_model")}>
          {embedder.model_id === null ? t("value.none") : <code className={styles.code}>{embedder.model_id}</code>}
        </Entry>
        <Entry term={t("health.tracing")}>{tracing(t, health.langsmith)}</Entry>
      </Group>

      <Group title={t("health.runs")}>
        <Entry term={t("health.last_run")}>
          {health.last_run === null || health.last_run_age_s === null
            ? t("value.none")
            : t("health.last_run_value", {
                id: String(health.last_run.id),
                status: serverText("run_status", health.last_run.status),
                age: units.duration(health.last_run_age_s),
              })}
        </Entry>
        <Entry term={t("health.last_success")}>{ago(t, health.last_successful_run_age_s, units.duration)}</Entry>
        <Entry term={t("health.last_full_success")}>{ago(t, health.last_fully_successful_run_age_s, units.duration)}</Entry>
        <Entry term={t("health.run_age")} tone={stateTone(health.run_age_state)}>{serverText("run_age_state", health.run_age_state)}</Entry>
      </Group>

      <BackupsGroup backups={health.backups} />

      <Group title={t("health.queues")}>
        {Object.entries(health.queue_depths).map(([queue, statuses]) => (
          <Entry key={queue} term={serverText("queue", queue)}>
            {Object.entries(statuses)
              .map(([status, n]) => t("health.queue_count", { status: serverText("queue_status", status), value: format.number(n) }))
              .join(" · ")}
          </Entry>
        ))}
      </Group>

      <Group title={t("health.score_lane")}>
        <Entry term={t("health.running_cycle")}>
          {scoring.running_cycle_id === null ? t("value.none") : t("health.cycle_id", { id: String(scoring.running_cycle_id) })}
        </Entry>
        <Entry term={t("health.pause")} tone={scoring.pause_reason === null ? undefined : "warn"}>
          {scoring.pause_reason === null ? t("health.not_paused") : serverText("pause", scoring.pause_reason)}
        </Entry>
        <Entry term={t("health.oldest_pending")}>
          {scoring.oldest_pending_target_age_s === null ? t("value.none") : units.duration(scoring.oldest_pending_target_age_s)}
        </Entry>
        <Entry term={t("health.pending")}>{format.number(scoring.pending_targets)}</Entry>
        <Entry term={t("health.in_batch")}>{format.number(scoring.in_batch_targets)}</Entry>
        <Entry term={t("settings_budget.quarantined")}>{format.usd(scoring.quarantined_batch_usd)}</Entry>
      </Group>

      {llm !== null && (
        <Group title={t("health.llm")}>
          <Entry term={t("health.month")}>{month.format(new Date(`${llm.month}T00:00:00Z`))}</Entry>
          <Entry term={t("settings_budget.counted")}>
            {t("settings_budget.of_cap", { value: format.usd(llm.counted), cap: format.usd(llm.cap) })}
          </Entry>
          <Entry term={t("settings_budget.headroom")}>{format.usd(llm.headroom)}</Entry>
          <Entry term={t("settings_budget.bucket")}>{format.usd(llm.bucket_usd)}</Entry>
        </Group>
      )}

      <Group title={t("health.quota")}>
        {PROVIDERS.map((provider) => {
          const window = health.quota[provider] ?? null;
          return (
            <Entry key={provider} term={serverText("provider", provider)}>
              {window === null
                ? t("settings_budget.quota_none")
                : t("health.quota_value", {
                    used: format.number(window.used_units),
                    limit: format.number(window.limit_units),
                    state: serverText("window_state", window.state),
                    end: format.dayMonth(window.window_end),
                  })}
            </Entry>
          );
        })}
      </Group>

      <Group title={t("health.alerts")}>
        {SEVERITIES.map((severity) => (
          <Entry key={severity} term={serverText("alert_severity", severity)}
            tone={severity === "critical" && health.alerts_open.critical > 0 ? "critical" : undefined}>
            {format.number(health.alerts_open[severity])}
          </Entry>
        ))}
      </Group>

      <Group title={t("health.sizes")}>
        <Entry term={<>{t("health.disk")} <code className={styles.code}>{health.disk.path}</code></>}>
          {t("health.disk_value", { free: units.bytes(health.disk.free_bytes), total: units.bytes(health.disk.total_bytes) })}
        </Entry>
        <Entry term={t("health.database")}>{units.bytes(health.database_bytes)}</Entry>
        <div className={cx(styles.entry, styles.nest)}>
          <dt className={styles.term}>{t("health.tables")}</dt>
          <dd className={styles.nested}>
            <dl className={styles.tables}>{tables.slice(0, TOP_TABLES).map(tableRow)}</dl>
            {tables.length > TOP_TABLES && (
              <details className={styles.details}>
                <summary>{t("health.all_tables", { count: tables.length, value: format.number(tables.length) })}</summary>
                <dl className={styles.tables}>{tables.map(tableRow)}</dl>
              </details>
            )}
          </dd>
        </div>
      </Group>

      <Group title={t("health.versions")}>
        {Object.entries(VERSION_LABELS).map(([name, label]) => (
          <Entry key={name} term={t(label)}><code className={styles.code}>{health.versions[name as keyof Versions]}</code></Entry>
        ))}
      </Group>
    </div>
  );
}

export function HealthPanel() {
  const { t } = useTranslation();
  const health = useHealth();
  let body: ReactNode;
  if (health.isPending) {
    body = <Skeleton variant="line" count={4} />;
  } else if (health.isError) {
    body = <ProblemBox error={health.error} onRetry={() => { void health.refetch(); }} />;
  } else {
    body = <Health health={health.data} />;
  }
  return (
    <section id="health" aria-labelledby="health-heading" className={parts.part}>
      <h3 id="health-heading" className={parts.title}>{t("settings_system.health")}</h3>
      {body}
    </section>
  );
}
