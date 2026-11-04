// The dry-run panel (spec §13.5, §15.7; plan M4 Task 28, Rulings T28-a and T28-b): the end of every settings
// section. Every panel shows the one form's state, so a tick in one is a tick in all; only the panel of the
// section last changed or saved raises an alert, the others show the same text quietly.
//
// - The active panel's status line is a polite live region ("Checking…", "Unsaved changes", "Saving…"); a quiet
//   panel shows a problem with the same text, request id and Retry, but not as an alert.
// - With nothing dirty: "No unsaved changes", and the standing warnings of the base document's own dry-run.
// - Else, in order: the "Settings changed elsewhere" offer, a refused save (the eval-gate override, or the
//   problem), a saved version that could not be read again (Ruling T28-f: the problem with Retry, Save disabled),
//   the dry-run's failure, its errors in red (each linked to its field in its own section, and to the
//   section's anchor from any other), its warnings (amber with "I understand", or one compact line with its tick
//   for a standing one), the projections as compact rows, the effect chips, and Save. While the current draft's
//   dry-run is not in, the panel says "Checking…" over the last answer, and Save is disabled.
// - **The re-score confirmation** (spec §13.3, E15; plan M4 Task 30, Ruling T30-a): a Save whose dry-run effects
//   include `llm_tuple` first asks in a `ConfirmDialog`, with the re-score's ads and cost from the `llm_rescore`
//   warning's detail. It is the Save of every section: the document is one, and any section's Save sends it.
//   Cancel (or Esc) sends nothing; Confirm saves as Save does.
import type { TFunction } from "i18next";
import { type MouseEvent, type ReactNode, useId, useState } from "react";
import { useTranslation } from "react-i18next";
import { NETWORK_CODE, toProblem } from "../api/client";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { cx } from "../components/cx";
import { ProblemBox } from "../components/ProblemBox";
import i18n, { intlLocale, type TranslationKey, useServerMessage, useServerText } from "../i18n/index";
import type { SettingsSection } from "../pages/ProfileSettingsPage";
import { type Format, useFormat } from "../system/format";
import styles from "./DryRunPanel.module.css";
import fields from "./fields.module.css";
import {
  type Issue,
  type ProjectionsOut,
  type SettingsForm,
  type WarningOut,
  fieldId,
  issueId,
  sectionOf,
  useSection,
  useSettingsForm,
} from "./useSettingsForm";

type ServerText = ReturnType<typeof useServerText>;

/** A USD amount to a hundredth of a cent (`$0.0526`): an expected cost per ad, a bucket's rate. */
export function usdFine(locale: string): (amount: string) => string {
  const dollars = new Intl.NumberFormat(locale, {
    style: "currency",
    currency: "USD",
    currencyDisplay: "narrowSymbol",
    minimumFractionDigits: 2,
    maximumFractionDigits: 4,
  });
  return (amount) => dollars.format(Number(amount));
}

function num(value: unknown): number | null {
  return typeof value === "number" ? value : null;
}

function str(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

/** The path above `path` (`scope.locations[0]` for `scope.locations[0].radius_km`), `null` at the top. */
function parentPath(path: string): string | null {
  const cut = Math.max(path.lastIndexOf("."), path.lastIndexOf("["));
  return cut > 0 ? path.slice(0, cut) : null;
}

const FOCUSABLE = "input, select, textarea, button, [tabindex]";

/** Focuses the field holding `path`: the element of its own id, else of the nearest path above it; a group gives
 * focus to its first control. */
function focusField(path: string): void {
  for (let at: string | null = path; at !== null; at = parentPath(at)) {
    const element = document.getElementById(fieldId(at));
    if (element !== null) {
      const target = element.matches(FOCUSABLE) ? element : element.querySelector<HTMLElement>(FOCUSABLE);
      (target ?? element).scrollIntoView({ block: "center" });
      (target ?? element).focus();
      return;
    }
  }
}

function warningTitle(code: string, t: TFunction): ReactNode {
  const key = `warning.${code}`;
  return i18n.exists(key) ? t(key as TranslationKey) : <code>{code}</code>;
}

/** A warning's details as its text needs them (`settings/dryrun.py`, `quota/dryrun.py`); `null` for a warning
 * whose title says it all. */
function warningDetail(warning: WarningOut, t: TFunction, format: Format): string | null {
  const d = warning.detail;
  switch (warning.code) {
    case "adzuna_rotation": {
      const days = num(d.cadence_days);
      return days === null
        ? t("warning_detail.adzuna_rotation_never")
        : t("warning_detail.adzuna_rotation", { count: days, value: format.number(days) });
    }
    case "paid_refresh_slow": {
      const days = num(d.cadence_days);
      return days === null ? null : t("warning_detail.paid_refresh_slow", { count: days, value: format.number(days) });
    }
    case "llm_cap_binding": {
      const scored = num(d.expected_scored_per_month);
      return scored === null ? null : t("warning_detail.llm_cap_binding", { count: scored, value: format.number(scored) });
    }
    case "llm_rescore": {
      const targets = num(d.targets);
      const est = str(d.est_usd);
      const bulk = str(d.bulk_budget_usd);
      if (targets === null || est === null || bulk === null) {
        return null;
      }
      return t("warning_detail.llm_rescore", {
        count: targets,
        value: format.number(targets),
        est_usd: format.usd(est),
        bulk_budget_usd: format.usd(bulk),
      });
    }
    case "paid_run_cap_binding": {
      const cap = num(d.cap);
      const fair = num(d.fair_share);
      if (cap === null || fair === null) {
        return null;
      }
      return t("warning_detail.paid_run_cap_binding", { count: cap, cap: format.number(cap), fair_share: format.number(fair) });
    }
    case "stale_ttl_short": {
      const days = num(d.label_after_days);
      return days === null ? null : t("warning_detail.stale_ttl_short", { count: days, value: format.number(days) });
    }
    default:
      return null;
  }
}

interface Row {
  key: string;
  label: string;
  text: string;
}

/** The projections as compact rows (§13.5): one per part, then the parts an effect class adds. */
function projectionRows(p: ProjectionsOut, t: TFunction, format: Format, serverText: ServerText, locale: string): Row[] {
  const fine = usdFine(locale);
  const percent = new Intl.NumberFormat(locale, { style: "percent", maximumFractionDigits: 1 });
  // the projections' rates and days, to a hundredth (the dry-run sends up to three places)
  const decimal = new Intl.NumberFormat(locale, { maximumFractionDigits: 2 });
  const figure = (n: number) => decimal.format(n);
  const join = (parts: (string | null)[]) => parts.filter((part): part is string => part !== null).join(" · ");
  const window = (w: ProjectionsOut["jsearch"]["window"]) =>
    w === null
      ? null
      : t("dry_run.window", { used: format.number(w.used_units), limit: format.number(w.limit_units), reset: format.dayMonth(w.reset_at) });
  const cadence = (days: number | null) =>
    days === null ? t("dry_run.cadence_never") : t("dry_run.cadence", { count: days, value: figure(days) });
  const serpapi = p.serpapi_fallback;
  const llm = p.llm;
  const rows: Row[] = [
    {
      key: "jsearch",
      label: t("provider.jsearch"),
      text: join([
        t("dry_run.units_per_run", { value: figure(p.jsearch.units_per_run) }),
        cadence(p.jsearch.refresh_cadence_days),
        t("dry_run.cells", { value: format.number(p.jsearch.cells) }),
        window(p.jsearch.window),
      ]),
    },
    {
      key: "adzuna",
      label: t("provider.adzuna"),
      text: join([
        t("dry_run.calls_per_day", { calls: figure(p.adzuna.calls_per_day), allowance: figure(p.adzuna.allowance_per_day) }),
        cadence(p.adzuna.cadence_days),
        t("dry_run.cells", { value: format.number(p.adzuna.cells) }),
      ]),
    },
    {
      key: "arbeitnow",
      label: t("source.arbeitnow"),
      text: t("dry_run.pages", { expected: figure(p.arbeitnow.expected_pages), cap: format.number(p.arbeitnow.page_cap) }),
    },
    {
      key: "serpapi",
      label: t("dry_run.serpapi_fallback"),
      text:
        serpapi === null
          ? t("dry_run.serpapi_none")
          : join([
              t("dry_run.units_per_run", { value: figure(serpapi.units_per_run) }),
              t("dry_run.hourly_cap", { value: format.number(serpapi.hourly_cap) }),
              serverText("window_state", serpapi.state),
              window(serpapi.window),
            ]),
    },
    {
      key: "llm",
      label: t("dry_run.llm"),
      text: join([
        t("dry_run.llm_bucket", { rate: fine(llm.bucket_rate_per_h), capacity: format.usd(llm.bucket_capacity) }),
        t("dry_run.llm_c_hat", { value: fine(llm.c_hat) }),
        t("dry_run.llm_critic_rate", { value: percent.format(Number(llm.projected_critic_rate)) }),
        t("dry_run.llm_targets", { targets: format.number(llm.targets_per_month), arrival: format.number(llm.arrival_per_month) }),
        t("dry_run.llm_month", {
          projected: format.usd(llm.projected_month_spend),
          cap: format.usd(llm.cap_usd),
          counted: format.usd(llm.counted_usd),
          headroom: format.usd(llm.headroom_usd),
        }),
      ]),
    },
  ];
  if (typeof p.det_recompute === "number") {
    rows.push({
      key: "det_recompute",
      label: t("dry_run.det_recompute_label"),
      text: t("dry_run.det_recompute", { count: p.det_recompute, value: format.number(p.det_recompute) }),
    });
  }
  if (typeof p.critic_rate === "string") {
    rows.push({
      key: "critic_rate",
      label: t("dry_run.critic_rate_label"),
      text: t("dry_run.critic_rate", { value: percent.format(Number(p.critic_rate)) }),
    });
  }
  if (typeof p.eval_gate === "string") {
    rows.push({ key: "eval_gate", label: t("dry_run.eval_gate_label"), text: serverText("eval_gate", p.eval_gate) });
  }
  if (p.next_slot !== undefined) {
    rows.push({
      key: "next_slot",
      label: t("dry_run.next_slot_label"),
      text: p.next_slot === null ? t("dry_run.next_slot_off") : format.dayMonthTime(p.next_slot),
    });
  }
  return rows;
}

/** A problem: the `ProblemBox` (an alert) where `alert`, elsewhere the same title, detail, request id and Retry
 * quietly (the settings page shows one state in several places, and announces it once). */
export function Problem({ error, alert, onRetry }: { error: unknown; alert: boolean; onRetry?: () => void }) {
  const { t } = useTranslation();
  const serverText = useServerText();
  const serverMessage = useServerMessage();
  if (alert) {
    return <ProblemBox error={error} onRetry={onRetry} />;
  }
  const problem = toProblem(error);
  const title = problem.code === NETWORK_CODE ? t("problem_box.network") : serverText("problem", problem.code, problem.message);
  const detail = serverMessage("problem", problem.code, problem.detail);
  return (
    <div className={styles.quietProblem}>
      <p className={styles.problemTitle}>{title}</p>
      {detail !== null && <p>{detail}</p>}
      {problem.requestId !== null && (
        <p className={styles.requestId}>{t("problem_box.request_id", { id: problem.requestId })}</p>
      )}
      {onRetry !== undefined && (
        <button type="button" className={cx(fields.button, styles.retry)} onClick={onRetry}>{t("action.retry")}</button>
      )}
    </div>
  );
}

function IssueItem({ issue, index, section }: { issue: Issue; index: number; section: SettingsSection }) {
  const { t } = useTranslation();
  const serverText = useServerText();
  const serverMessage = useServerMessage();
  const label = serverText("issue", issue.code, issue.code);
  const message = serverMessage("issue", issue.code, issue.message);
  const text = message === null ? label : t("settings_form.issue", { issue: label, message });
  const home = sectionOf(issue.path);
  let content: ReactNode = text;
  if (home === section) {
    content = (
      <a href={`#${fieldId(issue.path)}`} className={styles.issueLink}
        onClick={(event: MouseEvent<HTMLAnchorElement>) => {
          event.preventDefault();
          focusField(issue.path);
        }}>
        {text}
      </a>
    );
  } else if (home !== null) {
    content = (
      <a href={`#${home}`} className={styles.issueLink}>
        {t("settings_form.in_section", { section: t(`settings.section.${home}`), text })}
      </a>
    );
  }
  return <li id={issueId(section, index)} className={styles.issue}>{content}</li>;
}

function WarningItem({ warning, standing, form }: { warning: WarningOut; standing: boolean; form: SettingsForm }) {
  const { t } = useTranslation();
  const format = useFormat();
  const title = warningTitle(warning.code, t);
  const ticked = form.ticks.has(warning.code);
  const onTick = (checked: boolean) => { form.tick(warning.code, checked); };
  if (standing) {
    return (
      <li className={styles.standing}>
        <label className={styles.tick}>
          <input type="checkbox" checked={ticked} onChange={(event) => { onTick(event.target.checked); }} />
          <span>
            <span className={styles.standingTitle}>{title}</span>
            <span className={styles.muted}> · {t("settings_form.standing")}</span>
          </span>
        </label>
      </li>
    );
  }
  const detail = warningDetail(warning, t, format);
  return (
    <li className={styles.warning}>
      <strong className={styles.warningTitle}>{title}</strong>
      {detail !== null && <p>{detail}</p>}
      <label className={styles.tick}>
        <input type="checkbox" checked={ticked} onChange={(event) => { onTick(event.target.checked); }} />
        <span>{t("settings_form.understand")}</span>
      </label>
    </li>
  );
}

/** Why Save is disabled, when the panel does not already say it ("Checking…", the offer, a problem). */
function blockedReason(form: SettingsForm, t: TFunction): string | null {
  if (
    form.dirty.size === 0 ||
    form.canSave ||
    form.checking ||
    form.saving ||
    form.stale ||
    form.dryRunError !== null ||
    form.rereadError !== null
  ) {
    return null;
  }
  if (form.issues.length > 0) {
    return t("settings_form.blocked_errors");
  }
  if (form.dryRun !== null && form.dryRun.warnings.some((warning) => !form.ticks.has(warning.code))) {
    return t("settings_form.blocked_warnings");
  }
  return null;
}

export function DryRunPanel() {
  const { t, i18n: { language } } = useTranslation();
  const form = useSettingsForm();
  const serverText = useServerText();
  const serverMessage = useServerMessage();
  const format = useFormat();
  const section = useSection() ?? "scope";
  const reasonId = useId();
  const alert = form.activeSection === section;
  const dirty = form.dirty.size > 0;
  const result = form.dryRun;
  const warnings = dirty ? (result?.warnings ?? []) : (form.baseDryRun?.warnings ?? []);
  let status: string;
  if (form.saving) {
    status = t("settings_form.saving");
  } else if (form.rereadError !== null) {
    status = t("settings_form.reread_failed");
  } else if (!dirty) {
    status = t("settings_form.no_changes");
  } else {
    status = form.checking ? t("settings_form.checking") : t("settings_form.unsaved");
  }
  const rescore = warnings.find((warning) => warning.code === "llm_rescore");
  const rescoreUsd = rescore === undefined ? null : str(rescore.detail.est_usd);
  const rescoreTargets = rescore === undefined ? null : num(rescore.detail.targets);
  const effects = dirty ? (result?.effects ?? []).filter((effect) => effect !== "none") : [];
  const reason = blockedReason(form, t);
  const [asking, setAsking] = useState(false);
  const asks = effects.includes("llm_tuple");
  const gateDetail = form.saveError === null ? null : serverMessage("problem", form.saveError.code, form.saveError.detail);
  return (
    <div className={cx(styles.panel, form.checking && styles.checking)}>
      <p className={styles.status} role={alert ? "status" : undefined}>{status}</p>
      {form.stale && (
        <div className={styles.offer} role={alert ? "alert" : undefined}>
          <p className={styles.offerTitle}>{t("settings_form.changed_elsewhere")}</p>
          <p>{t("settings_form.changed_elsewhere_detail")}</p>
          <button type="button" className={fields.button} onClick={() => { void form.reapply(); }}>
            {t("settings_form.reload_and_reapply")}
          </button>
        </div>
      )}
      {form.evalGateFailed && form.saveError !== null && (
        <div className={styles.offer} role={alert ? "alert" : undefined}>
          <p className={styles.offerTitle}>{serverText("problem", form.saveError.code, form.saveError.message)}</p>
          {gateDetail !== null && <p>{gateDetail}</p>}
          <label className={styles.tick}>
            <input type="checkbox" checked={form.evalOverride}
              onChange={(event) => { form.setEvalOverride(event.target.checked); }} />
            <span>{t("settings_form.eval_override")}</span>
          </label>
        </div>
      )}
      {!form.evalGateFailed && form.saveError !== null && <Problem error={form.saveError} alert={alert} />}
      {form.rereadError !== null && <Problem error={form.rereadError} alert={alert} onRetry={form.retryReread} />}
      {form.dryRunError !== null && <Problem error={form.dryRunError} alert={alert} onRetry={form.retryDryRun} />}
      {form.issues.length > 0 && (
        <ul role="list" className={styles.issues} aria-label={t("settings_form.errors")}>
          {form.issues.map((issue, index) => (
            <IssueItem key={`${issue.path}-${issue.code}-${String(index)}`} issue={issue} index={index} section={section} />
          ))}
        </ul>
      )}
      {warnings.length > 0 && (
        <ul role="list" className={styles.warnings} aria-label={t("settings_form.warnings")}>
          {warnings.map((warning) => (
            <WarningItem key={warning.code} warning={warning} standing={form.standing.has(warning.code)} form={form} />
          ))}
        </ul>
      )}
      {dirty && result?.projections != null && (
        <ul role="list" className={styles.rows} aria-label={t("settings_form.preview")}>
          {projectionRows(result.projections, t, format, serverText, intlLocale(language)).map((row) => (
            <li key={row.key} className={styles.row}>
              <span className={styles.part}>{row.label}</span>
              <span>{row.text}</span>
            </li>
          ))}
        </ul>
      )}
      {effects.length > 0 && (
        <>
          <ul role="list" className={styles.chips} aria-label={t("settings_form.effects")}>
            {effects.map((effect) => (
              <li key={effect} className={styles.chip}>
                {effect === "llm_tuple" && rescoreUsd !== null
                  ? t("settings_form.effect_llm_cost", { usd: format.usd(rescoreUsd) })
                  : serverText("effect_class", effect)}
              </li>
            ))}
          </ul>
          {rescoreUsd !== null && <p className={styles.muted}>{t("settings_form.cost_note")}</p>}
        </>
      )}
      <div className={styles.actions}>
        <button type="button" className={fields.primary} disabled={!form.canSave} aria-busy={form.saving || undefined}
          aria-describedby={reason === null ? undefined : reasonId}
          onClick={() => {
            if (asks) {
              setAsking(true);
            } else {
              form.save(section);
            }
          }}>
          {t("settings_form.save")}
        </button>
        {reason !== null && <p id={reasonId} className={styles.reason}>{reason}</p>}
      </div>
      <ConfirmDialog open={asking} title={t("settings_form.rescore_title")} confirmLabel={t("settings_form.rescore_confirm")}
        onCancel={() => { setAsking(false); }}
        onConfirm={() => {
          setAsking(false);
          form.save(section);
        }}>
        <p>
          {rescoreTargets !== null && rescoreUsd !== null
            ? t("settings_form.rescore_body", {
                count: rescoreTargets,
                value: format.number(rescoreTargets),
                est_usd: format.usd(rescoreUsd),
              })
            : t("settings_form.rescore_body_unknown")}
        </p>
      </ConfirmDialog>
    </div>
  );
}
