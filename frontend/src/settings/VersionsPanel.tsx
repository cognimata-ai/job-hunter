// The shipped rubric and prompt/schema versions of Scoring & models (spec §13.3 L4916, §14.2, §15.7; plan M4
// Task 30, resolution e). A shipped version never activates itself: the user activates it here.
//
// - **The lists**: `GET /api/settings/rubric-versions` and `GET /api/settings/prompt-schema-versions`, by id.
//   Each row shows its version (a rubric's, with its hash) or its hash prefix (8 hex), its created date and the
//   active marker; a prompt/schema row also its schema dry-run (`passed`, `transport`, `finished_at`; "running"
//   while `passed` is null; "Not verified" without one).
// - **The notice**: each inactive version newer than its kind's active one (a higher id) shows "New rubric v<n>
//   available" or "New prompt/schema version available", with its eval result and re-score cost from its
//   `GET …/activation-preview`, read once. An older inactive version shows no notice; its row can still activate
//   it.
// - **Activate** (the notice's or a row's) opens the `ActivationDialog` of that version.
import { useQuery, type UseQueryResult } from "@tanstack/react-query";
import { type ReactNode, useId, useState } from "react";
import { useTranslation } from "react-i18next";
import { apiSend, type ProblemError } from "../api/client";
import type { components } from "../api/schema";
import { cx } from "../components/cx";
import { ProblemBox } from "../components/ProblemBox";
import { Skeleton } from "../components/Skeleton";
import { useServerText } from "../i18n/index";
import { useFormat } from "../system/format";
import { ActivationDialog, type ShippedKind, shippedListKey, shippedPath, useActivationPreview } from "./ActivationDialog";
import { useEvalResult } from "./EvalStatus";
import fields from "./fields.module.css";
import styles from "./VersionsPanel.module.css";

type RubricVersionOut = components["schemas"]["RubricVersionOut"];
type PromptSchemaVersionOut = components["schemas"]["PromptSchemaVersionOut"];
type SchemaDryRunOut = components["schemas"]["SchemaDryRunOut"];

/** The hex digits a row shows of a hash. */
const HASH_PREFIX = 8;

/** A version as the panel shows it, of either kind. */
interface Shipped {
  kind: ShippedKind;
  id: number;
  /** A rubric's version (`v2`), a prompt/schema version's hash prefix: what its title and Activate name. */
  name: string;
  hash: string;
  createdAt: string;
  active: boolean;
  /** A prompt/schema version's newest schema dry-run (`null` without one); `undefined` for a rubric. */
  dryRun?: SchemaDryRunOut | null;
}

function useShipped<T>(kind: ShippedKind): UseQueryResult<T[], ProblemError> {
  return useQuery({
    queryKey: shippedListKey(kind),
    queryFn: ({ signal }) => apiSend<T[]>(shippedPath(kind), { signal }),
  });
}

function rubric(row: RubricVersionOut): Shipped {
  return {
    kind: "rubric",
    id: row.id,
    name: row.version,
    hash: row.content_hash.slice(0, HASH_PREFIX),
    createdAt: row.created_at,
    active: row.active,
  };
}

function promptSchema(row: PromptSchemaVersionOut): Shipped {
  const hash = row.prompt_schema_hash.slice(0, HASH_PREFIX);
  return { kind: "prompt_schema", id: row.id, name: hash, hash, createdAt: row.created_at, active: row.active, dryRun: row.dry_run };
}

/** The inactive versions newer than the active one (a higher id). */
function newer(rows: readonly Shipped[]): Shipped[] {
  const active = Math.max(0, ...rows.filter((row) => row.active).map((row) => row.id));
  return rows.filter((row) => !row.active && row.id > active);
}

function useActivateLabel(): (version: Shipped) => string {
  const { t } = useTranslation();
  return (version) =>
    version.kind === "rubric"
      ? t("settings_scoring.activate_rubric", { version: version.name })
      : t("settings_scoring.activate_prompt", { hash: version.name });
}

function DryRunState({ dryRun }: { dryRun: SchemaDryRunOut | null }) {
  const { t } = useTranslation();
  const serverText = useServerText();
  const format = useFormat();
  if (dryRun === null) {
    return <span className={styles.unverified}>{t("settings_scoring.schema_not_verified")}</span>;
  }
  if (dryRun.passed === null) {
    return <span>{t("settings_scoring.schema_dry_run_running")}</span>;
  }
  const values = {
    transport: dryRun.transport === null ? t("settings_scoring.rate_none") : serverText("schema_transport", dryRun.transport),
    date: dryRun.finished_at === null ? t("settings_scoring.rate_none") : format.dayMonthTime(dryRun.finished_at),
  };
  return dryRun.passed ? (
    <span>{t("settings_scoring.schema_dry_run_passed", values)}</span>
  ) : (
    <span className={styles.unverified}>{t("settings_scoring.schema_dry_run_failed", values)}</span>
  );
}

function Notice({ version, onActivate }: { version: Shipped; onActivate: () => void }) {
  const { t } = useTranslation();
  const serverText = useServerText();
  const format = useFormat();
  const result = useEvalResult();
  const label = useActivateLabel();
  const titleId = useId();
  const preview = useActivationPreview(version.kind, version.id);
  let body: ReactNode;
  if (preview.isPending) {
    body = <Skeleton variant="line" count={2} />;
  } else if (preview.isError) {
    body = <ProblemBox error={preview.error} onRetry={() => { void preview.refetch(); }} />;
  } else {
    const p = preview.data;
    const state = serverText("eval_gate", p.gate_state);
    body = (
      <>
        <p>{t("settings_scoring.notice_eval", { result: result(state, p.latest_golden_run?.spearman_rho ?? null) })}</p>
        <p>{t("settings_scoring.notice_cost", { count: p.targets, value: format.number(p.targets), usd: format.usd(p.est_usd_online) })}</p>
      </>
    );
  }
  return (
    <div role="group" aria-labelledby={titleId} className={styles.notice}>
      <p id={titleId} className={styles.noticeTitle}>
        {version.kind === "rubric" ? t("settings_scoring.new_rubric", { version: version.name }) : t("settings_scoring.new_prompt")}
      </p>
      {body}
      <button type="button" className={cx(fields.primary, styles.noticeAction)} aria-label={label(version)} onClick={onActivate}>
        {t("settings_scoring.activate")}
      </button>
    </div>
  );
}

function VersionList({
  title,
  query,
  rows,
  onActivate,
}: {
  title: string;
  query: UseQueryResult<unknown, ProblemError>;
  rows: readonly Shipped[];
  onActivate: (version: Shipped) => void;
}) {
  const { t } = useTranslation();
  const format = useFormat();
  const label = useActivateLabel();
  const headingId = useId();
  let body: ReactNode;
  if (query.isPending) {
    body = <Skeleton variant="line" count={2} />;
  } else if (query.isError) {
    body = <ProblemBox error={query.error} onRetry={() => { void query.refetch(); }} />;
  } else {
    body = (
      <ul role="list" className={styles.list} aria-labelledby={headingId}>
        {rows.map((row) => (
          <li key={row.id} className={cx(styles.row, row.active && styles.active)}>
            <div className={styles.what}>
              <span className={styles.name}>
                {row.kind === "rubric" ? t("settings_scoring.rubric_name", { version: row.name, hash: row.hash }) : row.hash}
              </span>
              <span className={styles.meta}>
                {t("settings_scoring.version_created", { date: format.dateTimeMinutes(row.createdAt) })}
                {row.dryRun !== undefined && <> · <DryRunState dryRun={row.dryRun} /></>}
              </span>
            </div>
            {row.active ? (
              <span className={styles.badge}>{t("settings_scoring.version_active")}</span>
            ) : (
              <button type="button" className={fields.button} aria-label={label(row)} onClick={() => { onActivate(row); }}>
                {t("settings_scoring.activate")}
              </button>
            )}
          </li>
        ))}
      </ul>
    );
  }
  return (
    <div className={styles.kind}>
      <h4 id={headingId} className={fields.subheading}>{title}</h4>
      {body}
    </div>
  );
}

export function VersionsPanel() {
  const { t } = useTranslation();
  const headingId = useId();
  const rubrics = useShipped<RubricVersionOut>("rubric");
  const schemas = useShipped<PromptSchemaVersionOut>("prompt_schema");
  const [activating, setActivating] = useState<Shipped | null>(null);
  const rubricRows = (rubrics.data ?? []).map(rubric);
  const schemaRows = (schemas.data ?? []).map(promptSchema);
  const notices = [...newer(rubricRows), ...newer(schemaRows)];
  return (
    <section aria-labelledby={headingId} className={styles.versions}>
      <h3 id={headingId} className={fields.heading}>{t("settings_scoring.versions")}</h3>
      {notices.map((version) => (
        <Notice key={`${version.kind}-${String(version.id)}`} version={version} onActivate={() => { setActivating(version); }} />
      ))}
      <div className={styles.kinds}>
        <VersionList title={t("settings_scoring.rubric_versions")} query={rubrics} rows={rubricRows} onActivate={setActivating} />
        <VersionList title={t("settings_scoring.prompt_versions")} query={schemas} rows={schemaRows} onActivate={setActivating} />
      </div>
      {activating !== null && (
        <ActivationDialog kind={activating.kind} versionId={activating.id} name={activating.name}
          onClose={() => { setActivating(null); }} />
      )}
    </section>
  );
}
