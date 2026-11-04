// The Jobs page's filters (spec §15.3, §15.9; plan M4 Task 23, Ruling e): every §15.3 filter, each written to the
// URL's search (`useJobsSearch`), in the order of the spec's panel. The page's `aside`, named "Filters"; at
// 768 px and below it folds away behind its "Filters" button (closed at first), above that it is always shown.
//
// - A discrete change (a radio, a checkbox, the select, a picked place, Reset, Label mode) pushes a history
//   entry, so Back undoes it; the search text and the radius (each 300 ms after the last keystroke), the
//   custom dates and a slider's committed value replace the entry.
// - Work mode, Ad language, Status, Availability and Scoring state are never empty (the api refuses an empty
//   set): the last ticked box of each is disabled, and says why.
// - The option labels are the app's own keys, one per value of the API's set (`satisfies Record<…>`, so a new
//   value fails the typecheck); Employment and Seniority list the code catalogue's values, Source the facets'
//   sources (with what is picked), each with its count.
import * as Slider from "@radix-ui/react-slider";
import { type ReactNode, useEffect, useId, useState } from "react";
import { useTranslation } from "react-i18next";
import { useDisplay } from "../app/display";
import { cx } from "../components/cx";
import { intlLocale, type TranslationKey, useServerText } from "../i18n/index";
import { useFormat } from "../system/format";
import { useFacets } from "./api";
import { CompanyPicker } from "./CompanyPicker";
import styles from "./FilterPanel.module.css";
import { PlacePicker } from "./PlacePicker";
import {
  AD_LANGUAGES,
  type AdLanguage,
  type Availability,
  AVAILABILITIES,
  type Confidence,
  CONFIDENCES,
  DEFAULT_SEARCH,
  EMPLOYMENT_TYPES,
  type JobsSearch,
  POSTED_WINDOWS,
  type PostedWindow,
  SCORING_STATES,
  type ScoringState,
  SENIORITY_LEVELS,
  toApiParams,
  TRI,
  type Tri,
  USER_STATUSES,
  type UserStatus,
  WORK_MODES,
  type WorkMode,
} from "./search";
import { useJobsSearch, type WriteSearch } from "./useJobsSearch";

const TYPING_DELAY_MS = 300;
/** The radius a newly picked place starts with (km). */
const DEFAULT_RADIUS_KM = 30;
const MAX_RADIUS_KM = 200;
/** The days a custom window covers when it is chosen, up to today. */
const CUSTOM_DAYS = 30;

const POSTED_LABELS = {
  "24h": "filters.posted.24h",
  "3d": "filters.posted.3d",
  "7d": "filters.posted.7d",
  "14d": "filters.posted.14d",
  "30d": "filters.posted.30d",
  any: "filters.posted.any",
  custom: "filters.posted.custom",
} as const satisfies Record<PostedWindow, TranslationKey>;
const CONFIDENCE_LABELS = {
  any: "filters.confidence.any",
  medium: "filters.confidence.medium",
  high: "filters.confidence.high",
} as const satisfies Record<Confidence, TranslationKey>;
const WORK_MODE_LABELS = {
  remote: "filters.work_mode.remote",
  hybrid: "filters.work_mode.hybrid",
  onsite: "filters.work_mode.onsite",
  unknown: "filters.work_mode.unknown",
} as const satisfies Record<WorkMode, TranslationKey>;
const LANG_LABELS = {
  de: "filters.lang.de",
  en: "filters.lang.en",
  other: "filters.lang.other",
  unknown: "filters.lang.unknown",
} as const satisfies Record<AdLanguage, TranslationKey>;
const STATUS_LABELS = {
  unseen: "filters.status.unseen",
  seen: "filters.status.seen",
  saved: "filters.status.saved",
  applied: "filters.status.applied",
  hidden: "filters.status.hidden",
} as const satisfies Record<UserStatus, TranslationKey>;
const AVAILABILITY_LABELS = {
  active: "filters.availability.active",
  unknown: "filters.availability.unknown",
  stale: "filters.availability.stale",
  closed: "filters.availability.closed",
} as const satisfies Record<Availability, TranslationKey>;
const TRI_LABELS = {
  any: "filters.tri.any",
  only: "filters.tri.only",
  exclude: "filters.tri.exclude",
} as const satisfies Record<Tri, TranslationKey>;
const SCORING_STATE_LABELS = {
  scored: "filters.scoring_state.scored",
  unscored: "filters.scoring_state.unscored",
  filtered: "filters.scoring_state.filtered",
  failed: "filters.scoring_state.failed",
  expired: "filters.scoring_state.expired",
  not_a_job_ad: "filters.scoring_state.not_a_job_ad",
} as const satisfies Record<ScoringState, TranslationKey>;

/** Today in the display time zone, as `YYYY-MM-DD`. */
function today(timeZone: string): string {
  return new Intl.DateTimeFormat("en-CA", { year: "numeric", month: "2-digit", day: "2-digit", timeZone }).format(new Date());
}

function daysBefore(isoDate: string, days: number): string {
  const date = new Date(`${isoDate}T00:00:00Z`);
  date.setUTCDate(date.getUTCDate() - days);
  return date.toISOString().slice(0, 10);
}

/**
 * A field's own text while it is typed, handed to `write` `TYPING_DELAY_MS` after the last change unless it
 * already is `stored` (trimmed), and set to `stored` when that changes from elsewhere (Back, Reset) to
 * something else than the text: the stored form of what is being typed leaves the text alone (a trailing
 * space being typed stays).
 */
function useTypedText(stored: string, write: (text: string) => void): [string, (text: string) => void] {
  const [text, setText] = useState(stored);
  const [seen, setSeen] = useState(stored);
  if (stored !== seen) {
    setSeen(stored);
    if (text.trim() !== stored) {
      setText(stored);
    }
  }
  useEffect(() => {
    if (text.trim() === seen) {
      return;
    }
    const timer = setTimeout(() => { write(text); }, TYPING_DELAY_MS);
    return () => { clearTimeout(timer); };
    // the text alone starts the wait: `write` and `seen` are those of the render that changed it
  }, [text]);
  return [text, setText];
}

interface ChoiceProps<T extends string> {
  legend: string;
  values: readonly T[];
  label: (value: T) => string;
}

function CheckboxGroup<T extends string>({
  legend,
  values,
  label,
  selected,
  onChange,
  keepOne = false,
  empty,
}: ChoiceProps<T> & {
  selected: readonly T[];
  onChange: (next: T[]) => void;
  /** The set may not be empty: its last ticked box is disabled. */
  keepOne?: boolean;
  /** The text in place of the boxes when there is none. */
  empty?: string;
}) {
  const { t } = useTranslation();
  const hintId = useId();
  const only = keepOne && selected.length === 1 ? selected[0] : null;
  return (
    <fieldset className={styles.group}>
      <legend className={styles.legend}>{legend}</legend>
      <div className={styles.choices}>
        {values.map((value) => (
          <label key={value} className={styles.choice}>
            <input type="checkbox" checked={selected.includes(value)} disabled={value === only}
              aria-describedby={value === only ? hintId : undefined}
              onChange={(event) => {
                onChange(values.filter((other) => (other === value ? event.target.checked : selected.includes(other))));
              }} />
            <span>{label(value)}</span>
          </label>
        ))}
      </div>
      {values.length === 0 && empty !== undefined && <p className={styles.hint}>{empty}</p>}
      {only !== null && <p id={hintId} className={styles.hint}>{t("filters.last_one")}</p>}
    </fieldset>
  );
}

function RadioGroup<T extends string>({
  legend,
  values,
  label,
  value,
  onChange,
  children,
}: ChoiceProps<T> & { value: T; onChange: (next: T) => void; children?: ReactNode }) {
  const name = useId();
  return (
    <fieldset className={styles.group}>
      <legend className={styles.legend}>{legend}</legend>
      <div className={styles.choices}>
        {values.map((option) => (
          <label key={option} className={styles.choice}>
            <input type="radio" name={name} value={option} checked={value === option}
              onChange={() => { onChange(option); }} />
            <span>{label(option)}</span>
          </label>
        ))}
      </div>
      {children}
    </fieldset>
  );
}

function SearchText({ search, write }: { search: JobsSearch; write: WriteSearch }) {
  const { t } = useTranslation();
  const id = useId();
  const [text, setText] = useTypedText(search.q ?? "", (typed) => {
    const q = typed.trim();
    write({ q: q === "" ? undefined : q }, { replace: true });
  });
  return (
    <div className={styles.field}>
      <label htmlFor={id} className={styles.legend}>{t("filters.search")}</label>
      <input id={id} type="search" className={styles.input} value={text} maxLength={200} spellCheck={false}
        onChange={(event) => { setText(event.target.value); }} />
    </div>
  );
}

function PostedWithin({ search, write }: { search: JobsSearch; write: WriteSearch }) {
  const { t } = useTranslation();
  const { timezone } = useDisplay();
  const id = useId();
  const stored = { from: search.posted_from ?? "", to: search.posted_to ?? "" };
  const [dates, setDates] = useState(stored);
  const [seen, setSeen] = useState(stored);
  if (stored.from !== seen.from || stored.to !== seen.to) {
    setSeen(stored);
    setDates(stored);
  }
  const invalid = dates.from === "" || dates.to === "" || dates.from > dates.to;
  const errorId = `${id}-error`;
  const changeDate = (field: "from" | "to", value: string) => {
    const next = { ...dates, [field]: value };
    setDates(next);
    if (next.from !== "" && next.to !== "" && next.from <= next.to) {
      write({ posted_from: next.from, posted_to: next.to }, { replace: true });
    }
  };
  return (
    <RadioGroup legend={t("filters.posted")} values={POSTED_WINDOWS} label={(value) => t(POSTED_LABELS[value])}
      value={search.posted}
      onChange={(posted) => {
        if (posted === "custom") {
          const to = today(timezone);
          write({ posted, posted_from: daysBefore(to, CUSTOM_DAYS), posted_to: to });
        } else {
          write({ posted, posted_from: undefined, posted_to: undefined });
        }
      }}>
      {search.posted === "custom" && (
        <div className={styles.dates}>
          <label className={styles.date}>
            <span>{t("filters.posted_from")}</span>
            <input type="date" className={styles.input} value={dates.from} max={dates.to || undefined}
              aria-invalid={invalid} aria-describedby={invalid ? errorId : undefined}
              onChange={(event) => { changeDate("from", event.target.value); }} />
          </label>
          <label className={styles.date}>
            <span>{t("filters.posted_to")}</span>
            <input type="date" className={styles.input} value={dates.to} min={dates.from || undefined}
              aria-invalid={invalid} aria-describedby={invalid ? errorId : undefined}
              onChange={(event) => { changeDate("to", event.target.value); }} />
          </label>
          {invalid && <p id={errorId} className={styles.error}>{t("filters.posted_invalid")}</p>}
        </div>
      )}
    </RadioGroup>
  );
}

/** A slider's values while it is dragged, and the stored ones whenever those change from elsewhere. */
function useSliding(stored: number[]): [number[], (values: number[]) => void] {
  const [values, setValues] = useState(stored);
  const [seen, setSeen] = useState(stored);
  if (stored.join() !== seen.join()) {
    setSeen(stored);
    setValues(stored);
  }
  return [values, setValues];
}

function ScoreRange({ search, write }: { search: JobsSearch; write: WriteSearch }) {
  const { t } = useTranslation();
  const [range, setRange] = useSliding([search.score_min, search.score_max]);
  const [low = 0, high = 100] = range;
  return (
    <fieldset className={styles.group}>
      <legend className={styles.legend}>{t("filters.score")}</legend>
      <p className={styles.value}>{t("filters.score_range", { min: low, max: high })}</p>
      <Slider.Root className={styles.slider} min={0} max={100} step={1} value={range} onValueChange={setRange}
        onValueCommit={([min = 0, max = 100]) => { write({ score_min: min, score_max: max }, { replace: true }); }}>
        <Slider.Track className={styles.track}>
          <Slider.Range className={styles.range} />
        </Slider.Track>
        <Slider.Thumb className={styles.thumb} aria-label={t("filters.score_min")} />
        <Slider.Thumb className={styles.thumb} aria-label={t("filters.score_max")} />
      </Slider.Root>
    </fieldset>
  );
}

function MinCoverage({ search, write }: { search: JobsSearch; write: WriteSearch }) {
  const { t, i18n } = useTranslation();
  const [values, setValues] = useSliding([search.coverage_min]);
  const [value = 0] = values;
  const percent = new Intl.NumberFormat(intlLocale(i18n.language), { style: "percent" }).format(value / 100);
  return (
    <fieldset className={styles.group}>
      <legend className={styles.legend}>{t("filters.coverage")}</legend>
      <p className={styles.value}>{percent}</p>
      <Slider.Root className={styles.slider} min={0} max={100} step={1} value={values} onValueChange={setValues}
        onValueCommit={([min = 0]) => { write({ coverage_min: min }, { replace: true }); }}>
        <Slider.Track className={styles.track}>
          <Slider.Range className={styles.range} />
        </Slider.Track>
        <Slider.Thumb className={styles.thumb} aria-label={t("filters.coverage")} aria-valuetext={percent} />
      </Slider.Root>
    </fieldset>
  );
}

function Location({ search, write }: { search: JobsSearch; write: WriteSearch }) {
  const { t } = useTranslation();
  const id = useId();
  const place =
    search.loc_id === undefined || search.loc_lat === undefined || search.loc_lon === undefined
      ? null
      : { gazetteer_id: search.loc_id, label: search.loc_label ?? search.loc_id, lat: search.loc_lat, lon: search.loc_lon };
  const stored = search.radius_km === undefined ? "" : String(search.radius_km);
  const [radius, setRadius] = useTypedText(stored, (typed) => {
    const km = Number(typed);
    if (/^\d+$/.test(typed) && km >= 1 && km <= MAX_RADIUS_KM) {
      write({ radius_km: km }, { replace: true });
    }
  });
  const radiusInvalid = place !== null && !(/^\d+$/.test(radius) && Number(radius) >= 1 && Number(radius) <= MAX_RADIUS_KM);
  const errorId = `${id}-radius-error`;
  return (
    <fieldset className={styles.group}>
      <legend className={styles.legend}>{t("filters.location")}</legend>
      <PlacePicker label={t("filters.place")} value={place}
        onChange={(picked) => {
          write(
            picked === null
              ? { loc_id: undefined, loc_lat: undefined, loc_lon: undefined, radius_km: undefined, loc_label: undefined }
              : {
                  loc_id: picked.gazetteer_id,
                  loc_lat: picked.lat,
                  loc_lon: picked.lon,
                  radius_km: search.radius_km ?? DEFAULT_RADIUS_KM,
                  loc_label: picked.label,
                },
          );
        }} />
      {place !== null && (
        <>
          <div className={styles.field}>
            <label htmlFor={`${id}-radius`} className={styles.label}>{t("filters.radius")}</label>
            <input id={`${id}-radius`} type="number" inputMode="numeric" className={cx(styles.input, styles.radius)}
              min={1} max={MAX_RADIUS_KM} step={1} value={radius} aria-invalid={radiusInvalid}
              aria-describedby={radiusInvalid ? errorId : undefined}
              onChange={(event) => { setRadius(event.target.value); }} />
            {radiusInvalid && <p id={errorId} className={styles.error}>{t("filters.radius_invalid")}</p>}
          </div>
          <label className={styles.choice}>
            <input type="checkbox" checked={search.include_remote}
              onChange={(event) => { write({ include_remote: event.target.checked }); }} />
            <span>{t("filters.include_remote")}</span>
          </label>
        </>
      )}
    </fieldset>
  );
}

export function FilterPanel() {
  const { t } = useTranslation();
  const serverText = useServerText();
  const format = useFormat();
  const { search, write } = useJobsSearch();
  const [open, setOpen] = useState(false);
  const bodyId = useId();
  const params = toApiParams(search).toString();
  const facets = useFacets(params);
  const sourceCounts = new Map((facets.data?.sources ?? []).map((source) => [source.source_id, source.count]));
  const sources = [...sourceCounts.keys(), ...search.source.filter((id) => !sourceCounts.has(id))];
  return (
    <aside className={styles.panel} aria-label={t("filters.title")}>
      <h2 className={styles.heading}>{t("filters.title")}</h2>
      <button type="button" className={styles.toggle} aria-expanded={open} aria-controls={bodyId}
        onClick={() => { setOpen(!open); }}>
        <span>{t("filters.title")}</span>
        <span aria-hidden="true" className={styles.chevron}>{open ? "▴" : "▾"}</span>
      </button>
      <div id={bodyId} className={cx(styles.body, open && styles.open)}>
        <SearchText search={search} write={write} />
        <PostedWithin search={search} write={write} />
        <ScoreRange search={search} write={write} />
        <MinCoverage search={search} write={write} />
        <div className={cx(styles.field, styles.separated)}>
          <label htmlFor={`${bodyId}-confidence`} className={styles.legend}>{t("filters.confidence")}</label>
          <select id={`${bodyId}-confidence`} className={styles.input} value={search.confidence}
            onChange={(event) => {
              const confidence = CONFIDENCES.find((value) => value === event.target.value);
              if (confidence !== undefined) {
                write({ confidence });
              }
            }}>
            {CONFIDENCES.map((value) => <option key={value} value={value}>{t(CONFIDENCE_LABELS[value])}</option>)}
          </select>
        </div>
        <Location search={search} write={write} />
        <CheckboxGroup legend={t("filters.work_mode")} values={WORK_MODES} label={(value) => t(WORK_MODE_LABELS[value])}
          selected={search.work_mode} keepOne onChange={(work_mode) => { write({ work_mode }); }} />
        <CheckboxGroup legend={t("filters.employment")} values={EMPLOYMENT_TYPES} label={(value) => serverText("employment", value)}
          selected={search.employment} onChange={(employment) => { write({ employment }); }} />
        <CheckboxGroup legend={t("filters.seniority")} values={SENIORITY_LEVELS} label={(value) => serverText("seniority", value)}
          selected={search.seniority} onChange={(seniority) => { write({ seniority }); }} />
        <fieldset className={styles.group}>
          <legend className={styles.legend}>{t("filters.company")}</legend>
          <CompanyPicker params={params} value={search.company} known={facets.data?.companies ?? []}
            onChange={(company) => { write({ company }); }} />
        </fieldset>
        <CheckboxGroup legend={t("filters.source")} values={sources} selected={search.source}
          label={(id) => t("filters.source_option", { name: serverText("source", id), count: format.number(sourceCounts.get(id) ?? 0) })}
          empty={facets.data === undefined ? undefined : t("filters.source_none")}
          onChange={(source) => { write({ source }); }} />
        <CheckboxGroup legend={t("filters.lang")} values={AD_LANGUAGES} label={(value) => t(LANG_LABELS[value])}
          selected={search.lang} keepOne onChange={(lang) => { write({ lang }); }} />
        <CheckboxGroup legend={t("filters.status")} values={USER_STATUSES} label={(value) => t(STATUS_LABELS[value])}
          selected={search.status} keepOne onChange={(status) => { write({ status }); }} />
        <CheckboxGroup legend={t("filters.availability")} values={AVAILABILITIES} label={(value) => t(AVAILABILITY_LABELS[value])}
          selected={search.availability} keepOne onChange={(availability) => { write({ availability }); }} />
        <RadioGroup legend={t("filters.contested")} values={TRI} label={(value) => t(TRI_LABELS[value])}
          value={search.contested} onChange={(contested) => { write({ contested }); }} />
        <RadioGroup legend={t("filters.partial")} values={TRI} label={(value) => t(TRI_LABELS[value])}
          value={search.partial} onChange={(partial) => { write({ partial }); }} />
        <CheckboxGroup legend={t("filters.scoring_state")} values={SCORING_STATES} label={(value) => t(SCORING_STATE_LABELS[value])}
          selected={search.scoring_state} keepOne onChange={(scoring_state) => { write({ scoring_state }); }} />
        <div className={styles.buttons}>
          <button type="button" className={styles.button}
            onClick={() => { write((current) => ({ ...DEFAULT_SEARCH, ...(current.label === 1 ? { label: 1 } : {}) })); }}>
            {t("filters.reset")}
          </button>
          {/* Label mode is the `label=1` param alone until Task 26 builds the mode itself. */}
          <button type="button" className={styles.button} aria-pressed={search.label === 1}
            onClick={() => { write((current) => ({ ...current, label: current.label === 1 ? undefined : 1 })); }}>
            {t("filters.label_mode")}
          </button>
        </div>
      </div>
    </aside>
  );
}
