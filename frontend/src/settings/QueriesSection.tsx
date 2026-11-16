// Queries & sources (spec §5.5, §6, §8.11, §15.7; plan M4 Task 29, resolution b): the queries table with each
// row's cells, adding and editing a query, the query proposal, the source switches, the quota limits and the
// stale period. Every field and toggle writes the draft; the save is the section's dry-run panel's.
//
// - **The table**: one row per live query (`GET /api/queries`): its text, language, origin, whether the draft
//   enables it (a checkbox bound to the draft's `queries[]` entry by `query_id`), its exploration flag, its last
//   coverage (in the display time zone), open gaps, yield, refresh cadence ("—" for none or 0, Carry T13) and a
//   "too broad" badge. A query the draft does not list reads "Not in the settings", unticked: ticking adds it
//   enabled (`addEntry`), and unticking an entry the draft added takes it back (`removeEntry`). Its rows read as
//   cards whenever its box is narrower than 60rem (`tables.boxCards`, M5 follow-ups Task F5), so it never scrolls
//   sideways.
// - **A row's cells** (`QueryStats`): a disclosure below the row, from one read of the stats for the section.
// - **Add query**: a text of 2 to 120 characters (code points, trimmed) and its language, `POST /api/queries`,
//   then the query enters the draft enabled (`queriesApi.useAddQuery`).
// - **Edit**: the text and language, `PATCH /api/queries/{id}`, which retires the query and saves the settings
//   with the new id itself (an internal save): the form rebases (Ruling T29-a: with unsaved changes too, which
//   the rebase keeps by id). The row's own draft entry follows it to the new id: a toggle, or an entry the draft
//   had added (which no settings version names, so the server moved nothing). That follow-up is the edit
//   mutation's own (`queriesApi.useEditQuery`), so it runs also once the editor is gone; while the PATCH is out,
//   Cancel and every row's Edit are disabled.
// - **The other fields**: the source switches (`sources.enabled.*`), the quota limits (`quota.*`; the paid bound
//   automatic or a number) and the stale period (`collection.stale_ttl_days`, 7 to 90 days, or the maximum ad
//   age, D31). Only the stale period's range is written out (the brief's); every other bound is the dry-run's.
import { type SyntheticEvent, useId, useState } from "react";
import { useTranslation } from "react-i18next";
import { cx } from "../components/cx";
import { ProblemBox } from "../components/ProblemBox";
import { Skeleton } from "../components/Skeleton";
import { type TranslationKey, intlLocale, useServerText } from "../i18n/index";
import { useFormat } from "../system/format";
import { NumberField, ToggleField, closedSet } from "./fields";
import fields from "./fields.module.css";
import { ProposalPanel } from "./ProposalPanel";
import styles from "./QueriesSection.module.css";
import { EntryToggle } from "./EntryToggle";
import {
  QUERY_LANGS,
  type QueryLang,
  type QueryListOut,
  queryTextValid,
  useAddQuery,
  useEditQuery,
  useQueries,
  useQueryStats,
} from "./queriesApi";
import { QueryStats, cadenceText, decimals } from "./QueryStats";
import tables from "./tables.module.css";
import { type Settings, fieldId, getAt, useSettingsForm } from "./useSettingsForm";

type SourceSwitch = keyof Settings["sources"]["enabled"];
type Column =
  | "enabled"
  | "text"
  | "lang"
  | "origin"
  | "exploration"
  | "last_covered"
  | "open_gaps"
  | "yield"
  | "cadence"
  | "hint"
  | "actions";
type QuotaField = Exclude<keyof Settings["quota"], "paid_max_requests_per_run">;

/** The source switches in the model's order; `ats` (every watchlist board) has a label of its own. */
const SOURCE_SWITCHES = closedSet<SourceSwitch>({ jsearch: true, serpapi: true, adzuna: true, arbeitnow: true, ats: true });
const ATS_SWITCH = "ats" satisfies SourceSwitch;
/** The quota limits but the paid bound, in the model's order, each with its unit. */
const QUOTA_UNITS = {
  jsearch_monthly_limit: "settings_queries.unit.requests",
  serpapi_monthly_limit: "settings_queries.unit.searches",
  reserve_pct: "settings_queries.unit.percent",
  serpapi_hourly_cap: "settings_queries.unit.searches",
  adzuna_cap_minute: "settings_queries.unit.calls",
  adzuna_cap_day: "settings_queries.unit.calls",
  adzuna_cap_week: "settings_queries.unit.calls",
  adzuna_cap_month: "settings_queries.unit.calls",
} as const satisfies Record<QuotaField, TranslationKey>;
const QUOTA_FIELDS = closedSet<QuotaField>(QUOTA_UNITS);
/** `auto`'s floor (`max(10, ceil(2 × fair_share))`, §6.4): where an explicit bound starts. */
const PAID_MAX_START = 10;
/** The brief's range of the stale period (`settings/model.py` `Collection.stale_ttl_days`, ge=7, le=90). */
const STALE_TTL_DAYS = [7, 90] as const;
const COLUMNS = 11;

type Edit = ReturnType<typeof useEditQuery>;

function Row({
  query,
  expanded,
  onToggleCells,
  editing,
  onEdit,
  edit,
  stats,
}: {
  query: QueryListOut;
  expanded: boolean;
  onToggleCells: () => void;
  editing: boolean;
  onEdit: (open: boolean) => void;
  edit: Edit;
  stats: ReturnType<typeof useQueryStats>;
}) {
  const { t, i18n } = useTranslation();
  const serverText = useServerText();
  const format = useFormat();
  const id = useId();
  const number = decimals(intlLocale(i18n.language));
  const label = (column: Column) => t(`settings_queries.col.${column}`);
  return (
    <>
      <tr role="row">
        <td role="cell" data-label={label("enabled")}>
          <EntryToggle list="queries" id={query.id} label={t("settings_queries.enable", { text: query.text })} />
        </td>
        <td role="cell" data-label={label("text")} className={cx(tables.text, tables.wide)}>
          <span id={`${id}-text`} lang={query.lang}>{query.text}</span>
        </td>
        <td role="cell" data-label={label("lang")}>{serverText("language_code", query.lang)}</td>
        <td role="cell" data-label={label("origin")}>{serverText("query_origin", query.origin)}</td>
        <td role="cell" data-label={label("exploration")}>{t(query.exploration ? "value.yes" : "value.no")}</td>
        <td role="cell" data-label={label("last_covered")} className={tables.nowrap}>
          {query.last_covered_at === null ? t("settings_queries.never") : format.dayMonthTime(query.last_covered_at)}
        </td>
        <td role="cell" data-label={label("open_gaps")} className={tables.number}>{format.number(query.open_gaps)}</td>
        <td role="cell" data-label={label("yield")} className={tables.number}>
          {query.yield_ema === null ? t("value.none") : number(query.yield_ema)}
        </td>
        <td role="cell" data-label={label("cadence")} className={tables.nowrap}>{cadenceText(query.refresh_cadence_days, t, number)}</td>
        <td role="cell" data-label={label("hint")}>
          {query.too_broad && <span className={tables.warn}>{t("settings_queries.too_broad")}</span>}
        </td>
        <td role="cell" className={tables.wide}>
          <div className={tables.actions}>
            <button type="button" className={tables.action} aria-expanded={expanded} aria-controls={`${id}-cells`}
              aria-describedby={`${id}-text`} onClick={onToggleCells}>
              {t(expanded ? "settings_queries.hide_cells" : "settings_queries.show_cells")}
            </button>
            <button type="button" className={tables.action} aria-expanded={editing} aria-describedby={`${id}-text`}
              disabled={edit.isPending} onClick={() => { onEdit(!editing); }}>
              {t("settings_queries.edit")}
            </button>
          </div>
        </td>
      </tr>
      <tr role="row" id={`${id}-cells`} className={tables.detail} hidden={!expanded}>
        <td role="cell" colSpan={COLUMNS}>{expanded && <QueryStats queryId={query.id} stats={stats} />}</td>
      </tr>
      {editing && (
        <tr role="row" className={tables.detail}>
          <td role="cell" colSpan={COLUMNS}>
            <EditQuery query={query} edit={edit} onClose={() => { onEdit(false); }} />
          </td>
        </tr>
      )}
    </>
  );
}

/** The inline edit of a query: the PATCH with the query's draft entry, its follow-up the mutation's own. */
function EditQuery({ query, edit, onClose }: { query: QueryListOut; edit: Edit; onClose: () => void }) {
  const { t } = useTranslation();
  const serverText = useServerText();
  const form = useSettingsForm();
  const [text, setText] = useState(query.text);
  const [lang, setLang] = useState<QueryLang>(query.lang);
  const id = useId();
  const valid = queryTextValid(text);
  const mine = edit.variables?.id === query.id;
  const submit = (event: SyntheticEvent) => {
    event.preventDefault();
    if (!valid || edit.isPending) {
      return;
    }
    if (text.trim() === query.text && lang === query.lang) {
      onClose();
      return;
    }
    const entry = form.draft.queries.find((candidate) => candidate.query_id === query.id);
    edit.mutate(
      { id: query.id, text: text.trim(), lang, entry: entry === undefined ? null : { enabled: entry.enabled } },
      { onSuccess: onClose },
    );
  };
  return (
    <form onSubmit={submit} className={styles.edit} noValidate>
      <fieldset className={fields.group}>
        <legend className={fields.legend}>{t("settings_queries.edit_heading", { text: query.text })}</legend>
        <div className={styles.editFields}>
          <div className={cx(fields.field, styles.textField)}>
            <label htmlFor={`${id}-text`} className={fields.label}>{t("settings_queries.edit_text")}</label>
            <input id={`${id}-text`} type="text" className={fields.input} value={text} lang={lang}
              aria-describedby={`${id}-hint`} onChange={(event) => { setText(event.target.value); }} />
          </div>
          <div className={fields.field}>
            <label htmlFor={`${id}-lang`} className={fields.label}>{t("settings_queries.edit_lang")}</label>
            <select id={`${id}-lang`} className={fields.select} value={lang}
              onChange={(event) => { setLang(QUERY_LANGS.find((code) => code === event.target.value) ?? lang); }}>
              {QUERY_LANGS.map((code) => <option key={code} value={code}>{serverText("language_code", code)}</option>)}
            </select>
          </div>
        </div>
        <p id={`${id}-hint`} className={fields.hint}>{t("settings_queries.edit_hint")}</p>
        <div className={styles.buttons}>
          <button type="submit" className={fields.primary} disabled={!valid || edit.isPending} aria-busy={edit.isPending || undefined}>
            {t("settings_queries.edit_save")}
          </button>
          <button type="button" className={fields.button} disabled={edit.isPending} onClick={onClose}>{t("action.cancel")}</button>
        </div>
        {mine && edit.error !== null && <ProblemBox error={edit.error} />}
      </fieldset>
    </form>
  );
}

/** Add query: the text and its language, then `POST /api/queries` and the draft (`useAddQuery`). */
function AddQuery() {
  const { t, i18n } = useTranslation();
  const serverText = useServerText();
  const adding = useAddQuery();
  const [text, setText] = useState("");
  const [lang, setLang] = useState<QueryLang>(i18n.language === "de" ? "de" : "en");
  const id = useId();
  const valid = queryTextValid(text);
  const outcome = adding.outcome;
  let notice: string | null = null;
  if (outcome?.kind === "added") {
    notice = t("settings_queries.added", { text: outcome.text });
  } else if (outcome?.kind === "enabled") {
    notice = t("settings_queries.exists_enabled");
  } else if (outcome?.kind === "exists") {
    notice = t("settings_queries.exists");
  }
  const submit = (event: SyntheticEvent) => {
    event.preventDefault();
    if (valid && !adding.pending) {
      adding.add({ text: text.trim(), lang }, () => { setText(""); });
    }
  };
  return (
    <form onSubmit={submit} className={styles.add} noValidate>
      <fieldset className={fields.group}>
        <legend className={fields.legend}>{t("settings_queries.add_heading")}</legend>
        <div className={styles.editFields}>
          <div className={cx(fields.field, styles.textField)}>
            <label htmlFor={`${id}-text`} className={fields.label}>{t("settings_queries.new_text")}</label>
            <input id={`${id}-text`} type="text" className={fields.input} value={text} lang={lang}
              aria-describedby={`${id}-hint`} onChange={(event) => { setText(event.target.value); }} />
          </div>
          <div className={fields.field}>
            <label htmlFor={`${id}-lang`} className={fields.label}>{t("settings_queries.new_lang")}</label>
            <select id={`${id}-lang`} className={fields.select} value={lang}
              onChange={(event) => { setLang(QUERY_LANGS.find((code) => code === event.target.value) ?? lang); }}>
              {QUERY_LANGS.map((code) => <option key={code} value={code}>{serverText("language_code", code)}</option>)}
            </select>
          </div>
          <button type="submit" className={cx(fields.button, styles.addButton)} disabled={!valid || adding.pending}
            aria-busy={adding.pending || undefined}>
            {t("settings_queries.add")}
          </button>
        </div>
        <p id={`${id}-hint`} className={fields.hint}>{t("settings_queries.text_hint")}</p>
        <p role="status" className={styles.notice}>{notice}</p>
        {adding.error !== null && <ProblemBox error={adding.error} />}
      </fieldset>
    </form>
  );
}

function QueryTable() {
  const { t } = useTranslation();
  const queries = useQueries();
  const [expanded, setExpanded] = useState<ReadonlySet<number>>(new Set());
  const [editing, setEditing] = useState<number | null>(null);
  // one edit mutation for the table, held here: its follow-up reads the draft of this section's last render
  const edit = useEditQuery();
  const stats = useQueryStats(expanded.size > 0);
  const headingId = useId();
  const toggleCells = (id: number) => {
    setExpanded((current) => {
      const next = new Set(current);
      if (!next.delete(id)) {
        next.add(id);
      }
      return next;
    });
  };
  let body;
  if (queries.isPending) {
    body = <Skeleton variant="line" count={3} />;
  } else if (queries.isError) {
    body = <ProblemBox error={queries.error} onRetry={() => { void queries.refetch(); }} />;
  } else if (queries.data.length === 0) {
    body = <p className={tables.empty}>{t("settings_queries.no_queries")}</p>;
  } else {
    const column = (name: Column, numeric = false, hidden = false) => (
      <th role="columnheader" scope="col" className={numeric ? tables.number : undefined}>
        {hidden ? <span className="visually-hidden">{t(`settings_queries.col.${name}`)}</span> : t(`settings_queries.col.${name}`)}
      </th>
    );
    body = (
      <div className={cx(tables.scroll, tables.boxCards)}>
        <table role="table" className={tables.table} aria-labelledby={headingId}>
          <thead role="rowgroup">
            <tr role="row">
              {column("enabled")}
              {column("text")}
              {column("lang")}
              {column("origin")}
              {column("exploration")}
              {column("last_covered")}
              {column("open_gaps", true)}
              {column("yield", true)}
              {column("cadence")}
              {column("hint")}
              {column("actions", false, true)}
            </tr>
          </thead>
          <tbody role="rowgroup">
            {queries.data.map((query) => (
              <Row key={query.id} query={query} stats={stats} expanded={expanded.has(query.id)}
                onToggleCells={() => { toggleCells(query.id); }} editing={editing === query.id} edit={edit}
                onEdit={(open) => {
                  edit.reset();
                  setEditing(open ? query.id : null);
                }} />
            ))}
          </tbody>
        </table>
      </div>
    );
  }
  return (
    <div className={styles.group}>
      <h3 id={headingId} className={fields.heading}>{t("settings_queries.heading")}</h3>
      {body}
      <AddQuery />
    </div>
  );
}

/** A number that has an "off" value of its own (the paid bound's `auto`, the stale period's `null`): a checkbox
 * for the off value, and the number while it is not. */
function OptionalNumber({
  path,
  legend,
  offLabel,
  offValue,
  start,
  label,
  unit,
  hint,
  min,
  max,
}: {
  path: string;
  legend: string;
  offLabel: string;
  offValue: "auto" | null;
  start: number;
  label: string;
  unit: string;
  hint: string;
  min?: number;
  max?: number;
}) {
  const form = useSettingsForm();
  const id = `${fieldId(path)}-off`;
  const stored = getAt(form.draft, path);
  // Whether the number is shown, decided in events: the checkbox; the number's focus and blur (an emptied
  // number, the stale period's `null`, which is its off value too, stays shown while it has the focus, and
  // ticks the box again when it loses it); and a value from elsewhere (a rebase, a reapply): a number shows the
  // field, the off value ticks the box.
  const [explicit, setExplicit] = useState(stored !== offValue);
  const [focused, setFocused] = useState(false);
  const [seen, setSeen] = useState(stored);
  if (stored !== seen) {
    setSeen(stored);
    setExplicit(stored !== offValue || focused);
  }
  const off = !explicit;
  return (
    <fieldset className={cx(fields.group, styles.optional)} aria-describedby={`${id}-hint`}>
      <legend className={fields.legend}>{legend}</legend>
      <div className={fields.toggle}>
        <input id={id} type="checkbox" checked={off} onChange={(event) => {
          setExplicit(!event.target.checked);
          form.set(path, event.target.checked ? offValue : start);
        }} />
        <label htmlFor={id}>{offLabel}</label>
      </div>
      {!off && (
        <div onFocus={() => { setFocused(true); }} onBlur={() => {
          setFocused(false);
          if (stored === offValue) {
            setExplicit(false);
          }
        }}>
          <NumberField path={path} label={label} unit={unit} min={min} max={max} />
        </div>
      )}
      <p id={`${id}-hint`} className={fields.hint}>{hint}</p>
    </fieldset>
  );
}

function Sources() {
  const { t } = useTranslation();
  const serverText = useServerText();
  const form = useSettingsForm();
  const headingId = useId();
  const maxAge = form.draft.scope.max_ad_age_days;
  return (
    <div className={styles.group} role="group" aria-labelledby={headingId}>
      <h3 id={headingId} className={fields.heading}>{t("settings_queries.group.sources")}</h3>
      <div className={fields.grid}>
        {SOURCE_SWITCHES.map((source) => (
          <ToggleField key={source} path={`sources.enabled.${source}`}
            label={source === ATS_SWITCH ? t("settings_queries.source_ats") : serverText("source", source)} />
        ))}
      </div>
      <div className={fields.grid}>
        <OptionalNumber path="collection.stale_ttl_days" legend={t("settings_queries.stale_ttl_legend")}
          offLabel={t("settings_queries.stale_ttl_follow", { count: maxAge })} offValue={null}
          start={Math.min(Math.max(maxAge, STALE_TTL_DAYS[0]), STALE_TTL_DAYS[1])}
          label={t("settings_queries.stale_ttl")} unit={t("settings_queries.unit.days")}
          hint={t("settings_queries.stale_ttl_hint")} min={STALE_TTL_DAYS[0]} max={STALE_TTL_DAYS[1]} />
      </div>
    </div>
  );
}

function QuotaLimits() {
  const { t } = useTranslation();
  const headingId = useId();
  return (
    <div className={styles.group} role="group" aria-labelledby={headingId}>
      <h3 id={headingId} className={fields.heading}>{t("settings_queries.group.quota")}</h3>
      <div className={fields.grid}>
        {QUOTA_FIELDS.map((name) => (
          <NumberField key={name} path={`quota.${name}`} label={t(`settings_queries.quota.${name}`)} unit={t(QUOTA_UNITS[name])} />
        ))}
        <OptionalNumber path="quota.paid_max_requests_per_run" legend={t("settings_queries.quota.paid")}
          offLabel={t("settings_queries.quota.paid_auto")} offValue="auto" start={PAID_MAX_START}
          label={t("settings_queries.quota.paid_max")} unit={t("settings_queries.unit.requests")}
          hint={t("settings_queries.quota.paid_hint")} />
      </div>
    </div>
  );
}

export function QueriesSection() {
  return (
    <div className={styles.queries}>
      <QueryTable />
      <ProposalPanel />
      <Sources />
      <QuotaLimits />
    </div>
  );
}
