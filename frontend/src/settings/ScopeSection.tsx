// Search scope (spec §15.7, §8.12, §11.4, §13.6; plan M4 Task 28, resolution e): every scope field of the
// settings document, in §15.7's order. The fields' bounds are the model's; the dry-run's errors are the check.
//
// - **Locations**: at most 10, each a gazetteer place with a radius slider (1 to 200 km, 30 for a new place).
//   Each row shows the place's name, its postal codes (three, then "+n") and its population: for a place just
//   picked, as the picker answered it; for a saved one, from a search of its name in the gazetteer. At 10 places
//   Add place is disabled, and says why; the place that fills the list takes the focus to its row.
// - Only the brief's ranges are written out (the radius, 1 to 200 km); every other bound is the dry-run's.
// - **The rest**: remote work within Germany, work modes, employment types, the seniority range (relative to the
//   profile, or an explicit ladder range with open bounds), industries to include and exclude, the accepted ad
//   languages, the language requirements (a severity per language, or the default), the maximum ad age, the
//   excluded companies and keywords (and whether keywords are also looked for in the description), the minimum
//   salary, and the knockout severities.
// - With a scope change, "No LLM cost — n scores will be recomputed" (the dry-run's `det_recompute`).
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useId, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { apiFetch } from "../api/client";
import codes from "../i18n/codes.json";
import { type TranslationKey, useServerText } from "../i18n/index";
import { PlacePicker, type PlaceOut } from "../jobs/PlacePicker";
import { useFormat } from "../system/format";
import { CheckboxSetField, NumberField, RangeSlider, SelectField, TextListField, ToggleField, closedSet, describedBy, useFieldIssues } from "./fields";
import fields from "./fields.module.css";
import styles from "./ScopeSection.module.css";
import { type Settings, fieldId, useSettingsForm } from "./useSettingsForm";

type Scope = Settings["scope"];
type Location = Scope["locations"][number];
type WorkMode = Scope["work_modes"][number];
type EmploymentType = Scope["employment_types"][number];
type AdLanguage = Scope["accepted_ad_languages"][number];
type Severity = Scope["language_policy_default"];
type SeniorityLevel = NonNullable<Scope["seniority_range"]["min"]>;
type SeniorityMode = Scope["seniority_range"]["mode"];
type KnockoutCode = keyof Scope["knockout_severity"];

/** `scope.locations` holds at most 10 places (the model's `max_length`). */
export const MAX_LOCATIONS = 10;
export const DEFAULT_RADIUS_KM = 30;
const SHOWN_POSTAL_CODES = 3;
/** The gazetteer search's widest answer, and its longest `q`. */
const PLACE_SEARCH_LIMIT = 25;
const MAX_Q = 100;

// The closed sets in the model's order, each held to its type both ways.
const WORK_MODE_LABELS = {
  remote: "filters.work_mode.remote",
  hybrid: "filters.work_mode.hybrid",
  onsite: "filters.work_mode.onsite",
} as const satisfies Record<WorkMode, TranslationKey>;
const WORK_MODES = closedSet<WorkMode>(WORK_MODE_LABELS);
const EMPLOYMENT_TYPES = closedSet<EmploymentType>({
  full_time: true,
  part_time: true,
  contract: true,
  temporary: true,
  internship: true,
  working_student: true,
  apprenticeship: true,
  freelance: true,
});
const AD_LANGUAGES = closedSet<AdLanguage>({ de: true, en: true, other: true });
const SEVERITIES = closedSet<Severity>({ hard: true, soft: true });
const SENIORITY_MODES = closedSet<SeniorityMode>({ profile_relative: true, explicit: true });
const SENIORITY_LEVELS = new Set<string>(
  closedSet<SeniorityLevel>({
    intern: true,
    working_student: true,
    apprentice: true,
    junior: true,
    mid: true,
    senior: true,
    lead: true,
    principal: true,
    head: true,
  }),
);
/** The ladder, in its order (`codes.json`'s `order.seniority`, the backend's `SENIORITY_LADDER`). */
const LADDER = codes.order.seniority.filter((level): level is SeniorityLevel => SENIORITY_LEVELS.has(level));
const KNOCKOUTS = closedSet<KnockoutCode>({
  degree: true,
  certification: true,
  security_clearance: true,
  work_permit: true,
  onsite_required: true,
  contract_type: true,
});
/** The languages of `scope.language_policy` (the model's `LanguageCode`, §11.5 `lang_code`), in its order. The
 * schema types the map by string keys, so the list is written here; the tests hold it to the catalogue's
 * `language_code`, which `test_i18n_codes.py` holds to the model. */
export const POLICY_LANGUAGES = ["de", "en", "fr", "es", "it", "nl", "pl", "pt", "other"] as const;

const SENIORITY_MODE_LABELS = {
  profile_relative: "settings_scope.seniority_mode.profile_relative",
  explicit: "settings_scope.seniority_mode.explicit",
} as const satisfies Record<SeniorityMode, TranslationKey>;

const placeKey = (id: string) => ["geo", "place", id] as const;

/** A saved place's postal codes and population: its entry in a search of its name (none when the search does
 * not list it). The gazetteer ships with the release, so an answer is kept. */
function usePlaceDetail(location: Location): PlaceOut | null {
  const query = useQuery({
    queryKey: placeKey(location.gazetteer_id),
    queryFn: async ({ signal }) => {
      const search = new URLSearchParams({ q: location.label.slice(0, MAX_Q), limit: String(PLACE_SEARCH_LIMIT) });
      const found = await apiFetch<PlaceOut[]>(`/api/geo/places?${search.toString()}`, { signal });
      return found.find((place) => place.gazetteer_id === location.gazetteer_id) ?? null;
    },
    staleTime: Infinity,
  });
  return query.data ?? null;
}

function LocationRow({ location, index, onRemove }: { location: Location; index: number; onRemove: () => void }) {
  const { t } = useTranslation();
  const format = useFormat();
  const detail = usePlaceDetail(location);
  const path = `scope.locations[${String(index)}]`;
  // the row holds the issues of its place (its label and id); the slider those of its radius
  const label = useFieldIssues(`${path}.label`);
  const place = useFieldIssues(`${path}.gazetteer_id`);
  const issues = describedBy(label.issueIds, place.issueIds);
  let meta: string | null = null;
  if (detail !== null) {
    const shown = detail.postal_codes.slice(0, SHOWN_POSTAL_CODES).join(", ");
    const more = detail.postal_codes.length - SHOWN_POSTAL_CODES;
    const postal = more > 0 ? `${shown} ${t("filters.place_more", { count: more })}` : shown;
    meta = [postal, format.number(detail.population)].filter((part) => part !== "").join(" · ");
  }
  return (
    <li id={fieldId(path)} tabIndex={-1} className={styles.place} aria-describedby={issues}
      data-invalid={issues !== undefined || undefined}>
      <div className={styles.placeHead}>
        <div className={styles.placeName}>
          <span className={styles.placeLabel}>{location.label}</span>
          {meta !== null && <span className={styles.placeMeta}>{meta}</span>}
        </div>
        <button type="button" className={fields.iconButton} aria-label={t("settings_scope.remove_place", { place: location.label })}
          onClick={onRemove}>
          <span aria-hidden="true">×</span>
        </button>
      </div>
      {/* the brief's radius slider, 1–200 km (`settings/model.py` `ScopeLocation.radius_km`, ge=1, le=200) */}
      <RangeSlider path={`${path}.radius_km`} label={t("settings_scope.radius")} min={1} max={200}
        valueText={(km) => t("settings_scope.km", { value: format.number(km) })} />
    </li>
  );
}

function Locations() {
  const { t } = useTranslation();
  const form = useSettingsForm();
  const queryClient = useQueryClient();
  const locations = form.draft.scope.locations;
  const [adding, setAdding] = useState(false);
  const picker = useRef<HTMLDivElement>(null);
  const addButton = useRef<HTMLButtonElement>(null);
  const placesList = useRef<HTMLUListElement>(null);
  const returnFocus = useRef(false);
  const fullId = useId();
  const full = locations.length >= MAX_LOCATIONS;
  const list = useFieldIssues("scope.locations", { exact: true });
  useEffect(() => {
    if (adding) {
      picker.current?.querySelector("input")?.focus();
    } else if (returnFocus.current) {
      returnFocus.current = false;
      // back to Add; when the new place filled the list (Add disabled), to the new row's first control
      const add = addButton.current;
      if (add !== null && !add.disabled) {
        add.focus();
      } else {
        placesList.current?.lastElementChild?.querySelector<HTMLElement>("button, input, select, [role='slider']")?.focus();
      }
    }
  }, [adding]);
  const close = () => {
    returnFocus.current = true;
    setAdding(false);
  };
  return (
    <div id={fieldId("scope.locations")} className={styles.locations} aria-describedby={describedBy(list.issueIds)}>
      <h4 id={`${fieldId("scope.locations")}-heading`} className={fields.subheading}>{t("settings_scope.locations")}</h4>
      <ul ref={placesList} role="list" className={styles.places} aria-labelledby={`${fieldId("scope.locations")}-heading`}>
        {locations.map((location, index) => (
          <LocationRow key={`${location.gazetteer_id}-${String(index)}`} location={location} index={index}
            onRemove={() => { form.set("scope.locations", locations.filter((_, other) => other !== index)); }} />
        ))}
      </ul>
      {locations.length === 0 && <p className={fields.hint}>{t("settings_scope.no_places")}</p>}
      {adding ? (
        <div ref={picker} className={styles.adding}>
          <div className={styles.picker}>
            <PlacePicker label={t("settings_scope.place_to_add")} value={null}
              onChange={(picked, found) => {
                if (picked === null) {
                  return;
                }
                if (found !== undefined) {
                  queryClient.setQueryData(placeKey(found.gazetteer_id), found);
                }
                form.set("scope.locations", [...locations, { ...picked, radius_km: DEFAULT_RADIUS_KM }]);
                close();
              }} />
          </div>
          <button type="button" className={fields.button} onClick={close}>{t("action.cancel")}</button>
        </div>
      ) : (
        <div className={styles.add}>
          <button ref={addButton} type="button" className={fields.button} disabled={full}
            aria-describedby={full ? fullId : undefined} onClick={() => { setAdding(true); }}>
            {t("settings_scope.add_place")}
          </button>
          {full && <p id={fullId} className={fields.hint}>{t("settings_scope.places_full", { count: MAX_LOCATIONS })}</p>}
        </div>
      )}
    </div>
  );
}

function Seniority() {
  const { t } = useTranslation();
  const serverText = useServerText();
  const form = useSettingsForm();
  const mode = form.draft.scope.seniority_range.mode;
  const level = (value: SeniorityLevel) => serverText("seniority", value);
  return (
    <fieldset id={fieldId("scope.seniority_range")} className={fields.group}>
      <legend className={fields.legend}>{t("settings_scope.seniority")}</legend>
      <div className={fields.grid}>
        <SelectField path="scope.seniority_range.mode" label={t("settings_scope.seniority_range")} options={SENIORITY_MODES}
          optionLabel={(value) => t(SENIORITY_MODE_LABELS[value])} />
        {mode === "profile_relative" ? (
          <>
            <NumberField path="scope.seniority_range.below" label={t("settings_scope.seniority_below")} />
            <NumberField path="scope.seniority_range.above" label={t("settings_scope.seniority_above")} />
          </>
        ) : (
          <>
            <SelectField path="scope.seniority_range.min" label={t("settings_scope.seniority_min")} options={LADDER}
              optionLabel={level} empty={{ label: t("settings_scope.seniority_open"), value: null }} />
            <SelectField path="scope.seniority_range.max" label={t("settings_scope.seniority_max")} options={LADDER}
              optionLabel={level} empty={{ label: t("settings_scope.seniority_open"), value: null }} />
          </>
        )}
      </div>
    </fieldset>
  );
}

function LanguagePolicy() {
  const { t } = useTranslation();
  const serverText = useServerText();
  const severity = (value: Severity) => serverText("severity", value);
  return (
    <fieldset id={fieldId("scope.language_policy")} className={fields.group}>
      <legend className={fields.legend}>{t("settings_scope.language_policy")}</legend>
      <p className={fields.hint}>{t("settings_scope.language_policy_hint")}</p>
      <div className={fields.grid}>
        <SelectField path="scope.language_policy_default" label={t("settings_scope.language_policy_default")}
          options={SEVERITIES} optionLabel={severity} />
      </div>
      <div className={styles.policy}>
        {POLICY_LANGUAGES.map((code) => (
          <SelectField key={code} path={`scope.language_policy.${code}`} label={serverText("language_code", code)}
            options={SEVERITIES} optionLabel={severity}
            empty={{ label: t("settings_scope.language_policy_inherit"), value: undefined }} />
        ))}
      </div>
    </fieldset>
  );
}

export function ScopeSection() {
  const { t } = useTranslation();
  const serverText = useServerText();
  const format = useFormat();
  const form = useSettingsForm();
  const recompute = form.dirty.size > 0 ? form.dryRun?.projections?.det_recompute : undefined;
  const severity = (value: Severity) => serverText("severity", value);
  return (
    <div className={styles.scope}>
      <div className={styles.group} role="group" aria-labelledby="scope-where">
        <h3 id="scope-where" className={fields.heading}>{t("settings_scope.group.where")}</h3>
        <Locations />
        <ToggleField path="scope.remote_within_germany" label={t("settings_scope.remote_within_germany")} />
      </div>
      <div className={styles.group} role="group" aria-labelledby="scope-work">
        <h3 id="scope-work" className={fields.heading}>{t("settings_scope.group.work")}</h3>
        <CheckboxSetField path="scope.work_modes" label={t("settings_scope.work_modes")} options={WORK_MODES}
          optionLabel={(mode) => t(WORK_MODE_LABELS[mode])} keepOne />
        <CheckboxSetField path="scope.employment_types" label={t("settings_scope.employment_types")}
          options={EMPLOYMENT_TYPES} optionLabel={(type) => serverText("employment", type)} keepOne />
        <Seniority />
      </div>
      <div className={styles.group} role="group" aria-labelledby="scope-industries">
        <h3 id="scope-industries" className={fields.heading}>{t("settings_scope.group.industries")}</h3>
        <div className={fields.grid}>
          <TextListField path="scope.industries_include" label={t("settings_scope.industries_include")}
            hint={t("settings_scope.industries_include_hint")} maxLength={60} />
          <TextListField path="scope.industries_exclude" label={t("settings_scope.industries_exclude")} maxLength={60} />
        </div>
      </div>
      <div className={styles.group} role="group" aria-labelledby="scope-languages">
        <h3 id="scope-languages" className={fields.heading}>{t("settings_scope.group.languages")}</h3>
        <CheckboxSetField path="scope.accepted_ad_languages" label={t("settings_scope.accepted_ad_languages")}
          options={AD_LANGUAGES} optionLabel={(code) => serverText("language_code", code)} keepOne />
        <LanguagePolicy />
      </div>
      <div className={styles.group} role="group" aria-labelledby="scope-exclusions">
        <h3 id="scope-exclusions" className={fields.heading}>{t("settings_scope.group.exclusions")}</h3>
        <div className={fields.grid}>
          <NumberField path="scope.max_ad_age_days" label={t("settings_scope.max_ad_age")} unit={t("settings_scope.days")} />
          <NumberField path="scope.min_salary_eur_year" label={t("settings_scope.min_salary")}
            unit={t("settings_scope.eur_year")} hint={t("settings_scope.min_salary_hint")} />
        </div>
        <div className={fields.grid}>
          <TextListField path="scope.excluded_companies" label={t("settings_scope.excluded_companies")} />
          <div className={styles.keywords}>
            <TextListField path="scope.excluded_keywords" label={t("settings_scope.excluded_keywords")} maxLength={60} />
            <ToggleField path="scope.exclude_keywords_in_description" label={t("settings_scope.keywords_in_description")} />
          </div>
        </div>
      </div>
      <div className={styles.group} role="group" aria-labelledby="scope-knockouts">
        <h3 id="scope-knockouts" className={fields.heading}>{t("settings_scope.group.knockouts")}</h3>
        <p className={fields.hint}>{t("settings_scope.knockouts_hint")}</p>
        <div className={styles.policy}>
          {KNOCKOUTS.map((code) => (
            <SelectField key={code} path={`scope.knockout_severity.${code}`} label={serverText("knockout", code)}
              options={SEVERITIES} optionLabel={severity} />
          ))}
        </div>
      </div>
      {typeof recompute === "number" && (
        <p className={styles.recompute}>
          {t("settings_scope.recompute", { count: recompute, value: format.number(recompute) })}
        </p>
      )}
    </div>
  );
}
