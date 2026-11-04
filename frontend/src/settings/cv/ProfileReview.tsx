// The profile review (spec §12.3–§12.6, §15.7; plan M4 Task 27, resolution g).
//
// - The form holds a copy of the draft's profile; every field of `ConfirmedProfile` is editable, in the mock's
//   order, and every closed set renders through its code namespace (its option list held to the schema's type
//   by `satisfies`, and to the catalogue by the tests). The user-entered block is the user's alone (§12.4).
// - The issues show inline at their field, by path: the issue form (`skills[0].level`) and a 422's
//   (`body.draft.skills.0.level`) are one path (`paths.ts`). An issue at a path no field shows goes in the summary
//   "⚠ n validation issues", which counts them all. Each is its `issue` text and the server's message; a
//   proposal reads "Suggested: <value>", as text, with [Apply].
// - [Save draft] puts the profile and shows the answer's issues; the draft is read again (the server stores it
//   repaired), and the form takes the server's copy whenever it has no unsaved edits. [Discard] asks first.
//   [Confirm profile →] saves unsaved edits first, then opens the confirm modal; its 422 shows inline.
// - A stored profile the current schema refuses (`draft: null`, `schema_mismatch`) offers only Discard and
//   Paste text instead.
import { createContext, type ReactNode, useContext, useId, useState } from "react";
import { useTranslation } from "react-i18next";
import { type FieldError, toProblem } from "../../api/client";
import type { components } from "../../api/schema";
import { ConfirmDialog } from "../../components/ConfirmDialog";
import { cx } from "../../components/cx";
import { ProblemBox } from "../../components/ProblemBox";
import { useServerText } from "../../i18n/index";
import { useFormat } from "../../system/format";
import {
  type ConfirmedProfile,
  type DraftOut,
  useDiscardDraft,
  useSaveDraft,
  type ValidationIssueOut,
} from "./api";
import { ConfirmModal } from "./ConfirmModal";
import { normalizePath, parsePath, renderedPaths, setAt } from "./paths";
import controls from "./controls.module.css";
import styles from "./ProfileReview.module.css";

type Role = components["schemas"]["Role"];
type Skill = components["schemas"]["Skill"];
type Language = components["schemas"]["Language"];
type Education = components["schemas"]["Education"];
type Certification = components["schemas"]["Certification"];
type Clearance = components["schemas"]["SecurityClearance"];
type TargetTitle = components["schemas"]["TargetTitle"];
type UserEntered = Required<components["schemas"]["UserEntered"]>;
type Profile = Omit<ConfirmedProfile, "user_entered"> & { user_entered: UserEntered };
type JsonValue = components["schemas"]["JsonValue"];

/** A schema type's every value, in the schema's order (an object literal `satisfies` the full record). */
function valuesOf<T extends string>(all: Record<T, true>): readonly T[] {
  return Object.keys(all) as T[];
}

export const PROFILE_SENIORITIES = valuesOf({
  intern: true, working_student: true, apprentice: true, junior: true, mid: true, senior: true, lead: true,
  principal: true, head: true,
} satisfies Record<Profile["seniority"], true>);
export const ROLE_SENIORITIES = valuesOf({
  intern: true, working_student: true, apprentice: true, junior: true, mid: true, senior: true, lead: true,
  principal: true, head: true, unspecified: true,
} satisfies Record<Role["seniority"], true>);
export const SKILL_CATEGORIES = valuesOf({
  programming_language: true, framework: true, tool: true, platform: true, method: true, domain_knowledge: true,
  soft_skill: true, other: true,
} satisfies Record<Skill["category"], true>);
export const SKILL_LEVELS = valuesOf({ basic: true, intermediate: true, advanced: true, expert: true } satisfies Record<
  Skill["level"],
  true
>);
export const CEFR_LEVELS = valuesOf({
  A1: true, A2: true, B1: true, B2: true, C1: true, C2: true, native: true, unspecified: true,
} satisfies Record<Language["cefr"], true>);
export const LANGUAGE_CODES = valuesOf({
  de: true, en: true, fr: true, es: true, it: true, nl: true, pl: true, pt: true, other: true,
} satisfies Record<Language["code"], true>);
export const DEGREE_LEVELS = valuesOf({ vocational: true, bachelor: true, master: true, phd: true, other: true } satisfies Record<
  Education["level"],
  true
>);
export const WORK_AUTHORIZATIONS = valuesOf({ authorized: true, requires_sponsorship: true, unspecified: true } satisfies Record<
  UserEntered["work_authorization_de"],
  true
>);
const TITLE_LANGS = valuesOf({ en: true, de: true } satisfies Record<TargetTitle["lang"], true>);
// A target title's members, whose issues its chip shows (`paths.renderedPaths` lists them).
const TITLE_MEMBERS = valuesOf({ title: true, lang: true } satisfies Record<keyof TargetTitle, true>);

/** `items` with item `i` patched. */
function replaced<T>(items: readonly T[], i: number, patch: Partial<T>): T[] {
  return items.map((item, j) => (j === i ? { ...item, ...patch } : item));
}

/** `items` without item `i`. */
function without<T>(items: readonly T[], i: number): T[] {
  return items.filter((_, j) => j !== i);
}

// What [Add] puts in each list: an empty entry, its closed sets at their neutral value.
const NEW_ROLE: Role = { title: "", employer: null, start: null, end: null, seniority: "unspecified", domain: null, highlights: [] };
const NEW_SKILL: Skill = { name: "", category: "other", level: "basic", years: null, evidence: "" };
const NEW_LANGUAGE: Language = { code: "en", code_other: null, cefr: "unspecified", evidence: "" };
const NEW_EDUCATION: Education = { level: "other", field: "", institution: null, year: null };
const NEW_CERTIFICATION: Certification = { name: "", issuer: null, year: null };

/** The form's copy of a profile: the user-entered block with its defaults. */
function formOf(profile: ConfirmedProfile): Profile {
  return {
    ...profile,
    user_entered: {
      work_authorization_de: profile.user_entered?.work_authorization_de ?? "unspecified",
      security_clearances: profile.user_entered?.security_clearances ?? [],
    },
  };
}

interface Problem {
  path: string;
  code: string;
  message: string;
  /** The value the validator suggests; none (`undefined` or `null`) for most issues. */
  proposal: JsonValue;
}

interface IssuesApi {
  at: (path: string) => Problem[];
  idOf: (path: string) => string;
  apply: (path: string, value: JsonValue) => void;
}

const IssuesContext = createContext<IssuesApi | null>(null);

function useIssues(): IssuesApi {
  const api = useContext(IssuesContext);
  if (api === null) {
    throw new Error("a profile field outside its form");
  }
  return api;
}

/** A proposal as text. */
function proposalText(value: JsonValue): string {
  return typeof value === "string" ? value : JSON.stringify(value);
}

/** One issue: its `issue` text and the server's message, and a proposal with [Apply]. */
function IssueItem({ problem, showPath = false }: { problem: Problem; showPath?: boolean }) {
  const { t } = useTranslation();
  const serverText = useServerText();
  const { apply } = useIssues();
  const title = serverText("issue", problem.code, "");
  const proposal = problem.proposal;
  return (
    <li className={styles.issue}>
      <span aria-hidden="true">⚠ </span>
      {showPath && problem.path !== "" && <><code className={styles.path}>{problem.path}</code>{" "}</>}
      {title === "" ? problem.message : `${title}: ${problem.message}`}
      {proposal !== undefined && proposal !== null && (
        <span className={styles.proposal}>
          {" "}{t("cv.profile.suggested", { value: proposalText(proposal) })}{" "}
          <button type="button" className={controls.link} onClick={() => { apply(problem.path, proposal); }}>
            {t("cv.profile.apply")}
          </button>
        </span>
      )}
    </li>
  );
}

/** The issues at `path` (or at each of `path`'s paths, in order), and what its control carries for them. */
function useFieldIssues(path: string | readonly string[]): { invalid: boolean; describedBy: string | undefined; node: ReactNode } {
  const issues = useIssues();
  const paths = typeof path === "string" ? [path] : path;
  const list = paths.flatMap((p) => issues.at(p));
  if (list.length === 0) {
    return { invalid: false, describedBy: undefined, node: null };
  }
  const id = issues.idOf(paths[0] ?? "");
  return {
    invalid: true,
    describedBy: id,
    node: (
      <ul id={id} role="list" className={styles.issues}>
        {list.map((problem, i) => <IssueItem key={i} problem={problem} />)}
      </ul>
    ),
  };
}

interface FieldProps<V> {
  path: string;
  label: string;
  value: V;
  onChange: (value: V) => void;
  className?: string;
}

function TextField({ path, label, value, onChange, multiline = false, nullable = false, type = "text", className }:
  FieldProps<string | null> & { multiline?: boolean; nullable?: boolean; type?: "text" | "date" }) {
  const id = useId();
  const issues = useFieldIssues(path);
  const change = (next: string) => { onChange(nullable && next === "" ? null : next); };
  const shared = {
    id,
    value: value ?? "",
    "aria-invalid": issues.invalid,
    "aria-describedby": issues.describedBy,
  };
  return (
    <div className={cx(styles.field, className)}>
      <label htmlFor={id} className={controls.label}>{label}</label>
      {multiline ? (
        <textarea {...shared} rows={3} className={controls.textarea} onChange={(e) => { change(e.target.value); }} />
      ) : (
        <input {...shared} type={type} className={controls.input} onChange={(e) => { change(e.target.value); }} />
      )}
      {issues.node}
    </div>
  );
}

/** A number, or none when empty: the typed text is kept while it is not a number yet (`6.`). */
function NumberField({ path, label, value, onChange, step, className, after }:
  FieldProps<number | null> & { step: number; after?: ReactNode }) {
  const id = useId();
  const issues = useFieldIssues(path);
  const [typed, setTyped] = useState(value === null ? "" : String(value));
  const parsed = typed.trim() === "" ? null : Number(typed);
  const shown = parsed === value || (parsed !== null && Number.isNaN(parsed)) ? typed : value === null ? "" : String(value);
  return (
    <div className={cx(styles.field, className)}>
      <label htmlFor={id} className={controls.label}>{label}</label>
      <div className={styles.inline}>
        <input id={id} type="number" step={step} inputMode="decimal" className={cx(controls.input, styles.number)}
          value={shown} aria-invalid={issues.invalid} aria-describedby={issues.describedBy}
          onChange={(e) => {
            const next = e.target.value;
            setTyped(next);
            const n = next.trim() === "" ? null : Number(next);
            if (n === null || !Number.isNaN(n)) {
              onChange(n);
            }
          }} />
        {after}
      </div>
      {issues.node}
    </div>
  );
}

function SelectField<V extends string>({ path, label, value, onChange, options, text, className }:
  FieldProps<V> & { options: readonly V[]; text: (value: V) => string }) {
  const id = useId();
  const issues = useFieldIssues(path);
  return (
    <div className={cx(styles.field, className)}>
      <label htmlFor={id} className={controls.label}>{label}</label>
      <select id={id} className={controls.select} value={value} aria-invalid={issues.invalid}
        aria-describedby={issues.describedBy}
        onChange={(e) => { onChange(e.target.value as V); }}>
        {options.map((option) => <option key={option} value={option}>{text(option)}</option>)}
      </select>
      {issues.node}
    </div>
  );
}

/** A section of the form: its heading, the issues at the list itself, and its items. */
function Section({ title, path, children, add }: { title: string; path: string; children: ReactNode; add?: ReactNode }) {
  const headingId = useId();
  const issues = useFieldIssues(path);
  return (
    <section aria-labelledby={headingId} className={styles.section}>
      <h4 id={headingId} className={styles.sectionTitle}>{title}</h4>
      {issues.node}
      {children}
      {add !== undefined && <div className={styles.add}>{add}</div>}
    </section>
  );
}

/** One item of a list: a labelled group with its remove button. */
function Item({ legend, onRemove, removeLabel, children }:
  { legend: string; onRemove: () => void; removeLabel: string; children: ReactNode }) {
  return (
    <fieldset className={styles.item}>
      <legend className={styles.legend}>{legend}</legend>
      <div className={styles.fields}>{children}</div>
      <button type="button" className={styles.remove} onClick={onRemove}>{removeLabel}</button>
    </fieldset>
  );
}

/** A list shown as chips with ×, and a field to add one (`extra`: a control beside it, such as a language). A
 * chip shows the issues at its item and at its `members` (a target title's `title` and `lang`). */
function Chips({ path, members = [], labels, onRemove, onAdd, addLabel, removeLabel, extra }: {
  path: string;
  members?: readonly string[];
  labels: readonly string[];
  onRemove: (index: number) => void;
  onAdd: (text: string) => void;
  addLabel: string;
  removeLabel: (index: number) => string;
  extra?: ReactNode;
}) {
  const { t } = useTranslation();
  const inputId = useId();
  const [typed, setTyped] = useState("");
  const add = () => {
    const value = typed.trim();
    if (value !== "") {
      onAdd(value);
      setTyped("");
    }
  };
  return (
    <>
      <ul role="list" className={styles.chips}>
        {labels.map((label, i) => (
          <ChipItem key={`${String(i)}-${label}`} path={`${path}[${String(i)}]`} members={members} text={label}
            removeLabel={removeLabel(i)} onRemove={() => { onRemove(i); }} />
        ))}
      </ul>
      <div className={styles.inline}>
        <label htmlFor={inputId} className="visually-hidden">{addLabel}</label>
        <input id={inputId} className={controls.input} value={typed} placeholder={addLabel}
          onChange={(e) => { setTyped(e.target.value); }}
          onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); add(); } }} />
        {extra}
        <button type="button" className={controls.secondary} onClick={add}>{t("cv.profile.add")}</button>
      </div>
    </>
  );
}

function ChipItem({ path, members, text, removeLabel, onRemove }:
  { path: string; members: readonly string[]; text: string; removeLabel: string; onRemove: () => void }) {
  const issues = useFieldIssues([path, ...members.map((member) => `${path}.${member}`)]);
  return (
    <li className={cx(styles.chip, issues.invalid && styles.chipInvalid)} aria-describedby={issues.describedBy}>
      <span>{text}</span>
      <button type="button" className={styles.chipRemove} aria-label={removeLabel} onClick={onRemove}>
        <span aria-hidden="true">×</span>
      </button>
      {issues.node}
    </li>
  );
}

function monthOf(date: string | null): string {
  return date === null ? "" : /^\d{4}-\d{2}/.exec(date)?.[0] ?? date;
}

function RoleRow({ role, index, onChange, onRemove, open, onToggle }: {
  role: Role;
  index: number;
  onChange: (role: Role) => void;
  onRemove: () => void;
  open: boolean;
  onToggle: (open: boolean) => void;
}) {
  const { t } = useTranslation();
  const serverText = useServerText();
  const presentId = useId();
  const path = `roles[${String(index)}]`;
  const set = (patch: Partial<Role>) => { onChange({ ...role, ...patch }); };
  const present = role.end === "present";
  const summary = [
    role.title === "" ? t("cv.profile.untitled") : role.title,
    t("cv.profile.role_dates", { start: monthOf(role.start), end: present ? t("cv.profile.present") : monthOf(role.end) }),
    ...(role.domain === null || role.domain === "" ? [] : [t("cv.profile.role_domain", { domain: role.domain })]),
  ].join(" · ");
  return (
    <details className={styles.role} open={open} onToggle={(e) => { onToggle(e.currentTarget.open); }}>
      <summary className={styles.roleSummary}>
        <span>{summary}</span> <span className={styles.edit}>{t("cv.profile.edit")}</span>
      </summary>
      <Item legend={t("cv.profile.role_n", { n: index + 1 })} removeLabel={t("cv.profile.remove_role")} onRemove={onRemove}>
        <TextField path={`${path}.title`} label={t("cv.profile.role_title")} value={role.title}
          onChange={(v) => { set({ title: v ?? "" }); }} />
        <TextField path={`${path}.employer`} label={t("cv.profile.employer")} value={role.employer} nullable
          onChange={(v) => { set({ employer: v }); }} />
        <TextField path={`${path}.start`} label={t("cv.profile.start")} type="date" value={role.start} nullable
          onChange={(v) => { set({ start: v }); }} />
        <div className={styles.field}>
          {present ? (
            <p className={controls.label}>{t("cv.profile.end")}</p>
          ) : (
            <TextField path={`${path}.end`} label={t("cv.profile.end")} type="date" value={role.end} nullable
              onChange={(v) => { set({ end: v }); }} className={styles.flush} />
          )}
          <div className={controls.check}>
            <input id={presentId} type="checkbox" checked={present}
              onChange={(e) => { set({ end: e.target.checked ? "present" : null }); }} />
            <label htmlFor={presentId}>{t("cv.profile.present_role")}</label>
          </div>
          {present && <EndIssues path={`${path}.end`} />}
        </div>
        <SelectField path={`${path}.seniority`} label={t("cv.profile.role_seniority")} value={role.seniority}
          options={ROLE_SENIORITIES} text={(v) => serverText("role_seniority", v)} onChange={(v) => { set({ seniority: v }); }} />
        <TextField path={`${path}.domain`} label={t("cv.profile.domain")} value={role.domain} nullable
          onChange={(v) => { set({ domain: v }); }} />
        <div className={cx(styles.field, styles.wide)}>
          <p className={controls.label}>{t("cv.profile.highlights")}</p>
          <ListIssues path={`${path}.highlights`} />
          {role.highlights.map((highlight, i) => (
            <div key={i} className={cx(styles.inline, styles.highlight)}>
              <TextField path={`${path}.highlights[${String(i)}]`} label={t("cv.profile.highlight_n", { n: i + 1 })}
                value={highlight} className={styles.grow}
                onChange={(v) => { set({ highlights: role.highlights.map((h, j) => (j === i ? v ?? "" : h)) }); }} />
              <button type="button" className={styles.remove}
                aria-label={t("cv.profile.remove_highlight", { n: i + 1 })}
                onClick={() => { set({ highlights: role.highlights.filter((_, j) => j !== i) }); }}>
                <span aria-hidden="true">×</span>
              </button>
            </div>
          ))}
          <button type="button" className={controls.secondary} onClick={() => { set({ highlights: [...role.highlights, ""] }); }}>
            {t("cv.profile.add_highlight")}
          </button>
        </div>
      </Item>
    </details>
  );
}

/** The issues of a field the form shows as a checkbox (a role's end while it is "present"). */
function EndIssues({ path }: { path: string }) {
  return useFieldIssues(path).node;
}

/** The issues at a list itself (`roles[0].highlights`: a highlight dropped for its contact data). */
function ListIssues({ path }: { path: string }) {
  return useFieldIssues(path).node;
}

export interface ProfileReviewProps {
  draftId: number;
  /** A ready draft. */
  draft: DraftOut;
  /** [Paste text instead], offered with a stored profile the schema refuses. */
  onPaste: () => void;
}

export function ProfileReview({ draftId, draft, onPaste }: ProfileReviewProps) {
  if (draft.draft === null) {
    return <SchemaMismatch draftId={draftId} draft={draft} onPaste={onPaste} />;
  }
  return <ProfileForm draftId={draftId} draft={draft} profile={draft.draft} />;
}

function DiscardButton({ draftId }: { draftId: number }) {
  const { t } = useTranslation();
  const discard = useDiscardDraft(draftId);
  const [asking, setAsking] = useState(false);
  return (
    <>
      <button type="button" className={controls.secondary} onClick={() => { setAsking(true); }}>{t("cv.profile.discard")}</button>
      <ConfirmDialog open={asking} title={t("cv.discard.title")} confirmLabel={t("cv.discard.confirm")} busy={discard.isPending}
        onCancel={() => { setAsking(false); }}
        onConfirm={() => { discard.mutate(undefined, { onSettled: () => { setAsking(false); } }); }}>
        <p>{t("cv.discard.body")}</p>
      </ConfirmDialog>
      {discard.error !== null && <ProblemBox error={discard.error} />}
    </>
  );
}

function SchemaMismatch({ draftId, draft, onPaste }: ProfileReviewProps) {
  const { t } = useTranslation();
  const serverText = useServerText();
  const mismatch = draft.validation_issues.find((issue) => issue.code === "schema_mismatch");
  return (
    <div className={styles.mismatch}>
      <p role="alert" className={styles.issue}>{serverText("issue", "schema_mismatch", mismatch?.message)}</p>
      <div className={styles.actions}>
        <DiscardButton draftId={draftId} />
        <button type="button" className={controls.primary} onClick={onPaste}>{t("draft_repair.paste_text")}</button>
      </div>
    </div>
  );
}

function ProfileForm({ draftId, draft, profile }: { draftId: number; draft: DraftOut; profile: ConfirmedProfile }) {
  const { t } = useTranslation();
  const serverText = useServerText();
  const format = useFormat();
  const prefix = useId();
  const save = useSaveDraft(draftId);
  const server = JSON.stringify(profile);
  // The server's copy last seen, the copy the form has no unsaved edits against, and the form itself.
  const [state, setState] = useState(() => ({ seen: server, base: JSON.stringify(formOf(profile)), form: formOf(profile) }));
  const [savedIssues, setSavedIssues] = useState<ValidationIssueOut[] | null>(null);
  const [fieldErrors, setFieldErrors] = useState<FieldError[]>([]);
  const [confirming, setConfirming] = useState(false);
  const [openRoles, setOpenRoles] = useState<Record<number, boolean>>({});
  const dirty = JSON.stringify(state.form) !== state.base;
  if (state.seen !== server) {
    // the server's copy changed (after a save, repaired): the form takes it unless it has unsaved edits
    setState(dirty ? { ...state, seen: server } : { seen: server, base: JSON.stringify(formOf(profile)), form: formOf(profile) });
    setSavedIssues(null);
  }
  const form = state.form;
  const update = (patch: Partial<Profile>) => { setState((s) => ({ ...s, form: { ...s.form, ...patch } })); };
  const setSkill = (i: number, patch: Partial<Skill>) => { update({ skills: replaced(form.skills, i, patch) }); };
  const setLanguage = (i: number, patch: Partial<Language>) => { update({ languages: replaced(form.languages, i, patch) }); };
  const setEducation = (i: number, patch: Partial<Education>) => { update({ education: replaced(form.education, i, patch) }); };
  const setCertification = (i: number, patch: Partial<Certification>) => {
    update({ certifications: replaced(form.certifications, i, patch) });
  };

  const problems: Problem[] = [
    ...(savedIssues ?? draft.validation_issues).map((i) => ({
      path: normalizePath(i.path),
      code: i.code,
      message: i.message,
      proposal: i.proposal,
    })),
    ...fieldErrors.map((e) => ({ path: normalizePath(e.field), code: e.code, message: e.message, proposal: null })),
  ];
  const rendered = renderedPaths(form);
  const unplaced = problems.filter((p) => !rendered.has(p.path));
  const issuesApi: IssuesApi = {
    at: (path) => problems.filter((p) => p.path === path),
    idOf: (path) => `${prefix}-issues-${path}`,
    apply: (path, value) => {
      setState((s) => ({ ...s, form: setAt(s.form, parsePath(path), value) as Profile }));
    },
  };
  const roleOpen = (i: number) =>
    openRoles[i] ?? problems.some((p) => p.path === `roles[${String(i)}]` || p.path.startsWith(`roles[${String(i)}].`));

  const saveNow = async (): Promise<boolean> => {
    setFieldErrors([]);
    const sent = state.form;
    try {
      const answer = await save.mutateAsync(sent);
      setSavedIssues(answer.validation_issues);
      setState((s) => ({ ...s, base: JSON.stringify(sent) }));
      return true;
    } catch (error) {
      const problem = toProblem(error);
      if (problem.status === 422) {
        setFieldErrors(problem.errors);
      }
      return false;
    }
  };
  const confirm = async () => {
    if (save.isPending) {
      return;
    }
    if (!dirty || (await saveNow())) {
      setConfirming(true);
    }
  };

  const [newLang, setNewLang] = useState<TargetTitle["lang"]>("en");
  const titleLangId = useId();
  const clearances = form.user_entered.security_clearances;
  const setUser = (patch: Partial<UserEntered>) => { update({ user_entered: { ...form.user_entered, ...patch } }); };
  const issueCount = problems.length;

  return (
    <IssuesContext.Provider value={issuesApi}>
      <form aria-label={t("cv.profile.title")} className={styles.form} noValidate
        onSubmit={(e) => { e.preventDefault(); void saveNow(); }}>
        <TextField path="summary" label={t("cv.profile.summary")} value={form.summary} multiline className={styles.wide}
          onChange={(v) => { update({ summary: v ?? "" }); }} />
        <Section title={t("cv.profile.target_titles")} path="target_titles">
          <Chips path="target_titles" members={TITLE_MEMBERS} addLabel={t("cv.profile.new_title")}
            labels={form.target_titles.map((title) =>
              t("cv.profile.title_chip", { title: title.title, lang: serverText("language_code", title.lang) }),
            )}
            removeLabel={(i) => t("cv.profile.remove_title", { title: form.target_titles[i]?.title ?? "" })}
            onRemove={(i) => { update({ target_titles: without(form.target_titles, i) }); }}
            onAdd={(title) => { update({ target_titles: [...form.target_titles, { title, lang: newLang }] }); }}
            extra={
              <>
                <label htmlFor={titleLangId} className="visually-hidden">{t("cv.profile.title_lang")}</label>
                <select id={titleLangId} className={controls.select} value={newLang}
                  onChange={(e) => { setNewLang(e.target.value as TargetTitle["lang"]); }}>
                  {TITLE_LANGS.map((lang) => <option key={lang} value={lang}>{serverText("language_code", lang)}</option>)}
                </select>
              </>
            } />
        </Section>
        <div className={styles.pair}>
          <SelectField path="seniority" label={t("cv.profile.seniority")} value={form.seniority} options={PROFILE_SENIORITIES}
            text={(v) => serverText("seniority", v)} onChange={(v) => { update({ seniority: v }); }} />
          <NumberField path="total_years_experience" label={t("cv.profile.total_years")} step={0.1}
            value={form.total_years_experience} onChange={(v) => { update({ total_years_experience: v }); }}
            after={draft.recomputed_total_years !== null && (
              <span className={styles.muted}>{t("cv.profile.recomputed", { years: format.number(draft.recomputed_total_years) })}</span>
            )} />
        </div>
        <Section title={t("cv.profile.roles")} path="roles"
          add={<button type="button" className={controls.secondary}
            onClick={() => {
              update({ roles: [...form.roles, NEW_ROLE] });
              setOpenRoles({ ...openRoles, [form.roles.length]: true });
            }}>{t("cv.profile.add_role")}</button>}>
          {form.roles.map((role, i) => (
            <RoleRow key={i} role={role} index={i} open={roleOpen(i)}
              onToggle={(open) => { if (open !== roleOpen(i)) { setOpenRoles({ ...openRoles, [i]: open }); } }}
              onChange={(next) => { update({ roles: form.roles.map((r, j) => (j === i ? next : r)) }); }}
              onRemove={() => { update({ roles: form.roles.filter((_, j) => j !== i) }); setOpenRoles({}); }} />
          ))}
        </Section>
        <Section title={t("cv.profile.skills")} path="skills"
          add={<button type="button" className={controls.secondary}
            onClick={() => { update({ skills: [...form.skills, NEW_SKILL] }); }}>
            {t("cv.profile.add_skill")}</button>}>
          {form.skills.map((skill, i) => {
            const path = `skills[${String(i)}]`;
            return (
              <Item key={i} legend={t("cv.profile.skill_n", { n: i + 1 })} removeLabel={t("cv.profile.remove_skill")}
                onRemove={() => { update({ skills: without(form.skills, i) }); }}>
                <TextField path={`${path}.name`} label={t("cv.profile.skill_name")} value={skill.name}
                  onChange={(v) => { setSkill(i, { name: v ?? "" }); }} />
                <SelectField path={`${path}.category`} label={t("cv.profile.skill_category")} value={skill.category} options={SKILL_CATEGORIES}
                  text={(v) => serverText("skill_category", v)} onChange={(v) => { setSkill(i, { category: v }); }} />
                <SelectField path={`${path}.level`} label={t("cv.profile.skill_level")} value={skill.level} options={SKILL_LEVELS}
                  text={(v) => serverText("skill_level", v)} onChange={(v) => { setSkill(i, { level: v }); }} />
                <NumberField path={`${path}.years`} label={t("cv.profile.years")} step={0.5} value={skill.years}
                  onChange={(v) => { setSkill(i, { years: v }); }} />
                <TextField path={`${path}.evidence`} label={t("cv.profile.evidence")} value={skill.evidence} className={styles.wide}
                  onChange={(v) => { setSkill(i, { evidence: v ?? "" }); }} />
              </Item>
            );
          })}
        </Section>
        <Section title={t("cv.profile.languages")} path="languages"
          add={<button type="button" className={controls.secondary}
            onClick={() => { update({ languages: [...form.languages, NEW_LANGUAGE] }); }}>
            {t("cv.profile.add_language")}</button>}>
          {form.languages.map((language, i) => {
            const path = `languages[${String(i)}]`;
            return (
              <Item key={i} legend={t("cv.profile.language_n", { n: i + 1 })} removeLabel={t("cv.profile.remove_language")}
                onRemove={() => { update({ languages: without(form.languages, i) }); }}>
                <SelectField path={`${path}.code`} label={t("cv.profile.language_code")} value={language.code} options={LANGUAGE_CODES}
                  text={(v) => serverText("language_code", v)}
                  onChange={(v) => { setLanguage(i, { code: v, code_other: v === "other" ? language.code_other : null }); }} />
                {(language.code === "other" || language.code_other !== null) && (
                  <TextField path={`${path}.code_other`} label={t("cv.profile.code_other")} value={language.code_other} nullable
                    onChange={(v) => { setLanguage(i, { code_other: v }); }} />
                )}
                <SelectField path={`${path}.cefr`} label={t("cv.profile.cefr")} value={language.cefr} options={CEFR_LEVELS}
                  text={(v) => serverText("cefr", v)} onChange={(v) => { setLanguage(i, { cefr: v }); }} />
                <TextField path={`${path}.evidence`} label={t("cv.profile.evidence")} value={language.evidence} className={styles.wide}
                  onChange={(v) => { setLanguage(i, { evidence: v ?? "" }); }} />
              </Item>
            );
          })}
        </Section>
        <Section title={t("cv.profile.education")} path="education"
          add={<button type="button" className={controls.secondary}
            onClick={() => { update({ education: [...form.education, NEW_EDUCATION] }); }}>
            {t("cv.profile.add_education")}</button>}>
          {form.education.map((entry, i) => {
            const path = `education[${String(i)}]`;
            return (
              <Item key={i} legend={t("cv.profile.education_n", { n: i + 1 })} removeLabel={t("cv.profile.remove_education")}
                onRemove={() => { update({ education: without(form.education, i) }); }}>
                <SelectField path={`${path}.level`} label={t("cv.profile.degree")} value={entry.level} options={DEGREE_LEVELS}
                  text={(v) => serverText("degree_level", v)} onChange={(v) => { setEducation(i, { level: v }); }} />
                <TextField path={`${path}.field`} label={t("cv.profile.field")} value={entry.field}
                  onChange={(v) => { setEducation(i, { field: v ?? "" }); }} />
                <TextField path={`${path}.institution`} label={t("cv.profile.institution")} value={entry.institution} nullable
                  onChange={(v) => { setEducation(i, { institution: v }); }} />
                <NumberField path={`${path}.year`} label={t("cv.profile.year")} step={1} value={entry.year}
                  onChange={(v) => { setEducation(i, { year: v }); }} />
              </Item>
            );
          })}
        </Section>
        <Section title={t("cv.profile.certifications")} path="certifications"
          add={<button type="button" className={controls.secondary}
            onClick={() => { update({ certifications: [...form.certifications, NEW_CERTIFICATION] }); }}>
            {t("cv.profile.add_certification")}</button>}>
          {form.certifications.map((cert: Certification, i) => {
            const path = `certifications[${String(i)}]`;
            return (
              <Item key={i} legend={t("cv.profile.certification_n", { n: i + 1 })} removeLabel={t("cv.profile.remove_certification")}
                onRemove={() => { update({ certifications: without(form.certifications, i) }); }}>
                <TextField path={`${path}.name`} label={t("cv.profile.cert_name")} value={cert.name}
                  onChange={(v) => { setCertification(i, { name: v ?? "" }); }} />
                <TextField path={`${path}.issuer`} label={t("cv.profile.issuer")} value={cert.issuer} nullable
                  onChange={(v) => { setCertification(i, { issuer: v }); }} />
                <NumberField path={`${path}.year`} label={t("cv.profile.year")} step={1} value={cert.year}
                  onChange={(v) => { setCertification(i, { year: v }); }} />
              </Item>
            );
          })}
        </Section>
        <Section title={t("cv.profile.domains")} path="domains">
          <Chips path="domains" labels={form.domains} addLabel={t("cv.profile.new_domain")}
            removeLabel={(i) => t("cv.profile.remove_domain", { domain: form.domains[i] ?? "" })}
            onRemove={(i) => { update({ domains: without(form.domains, i) }); }}
            onAdd={(domain) => { update({ domains: [...form.domains, domain] }); }} />
        </Section>
        <Section title={t("cv.profile.you_only")} path="user_entered.security_clearances"
          add={<button type="button" className={controls.secondary}
            onClick={() => { setUser({ security_clearances: [...clearances, { name: "", level: null }] }); }}>
            {t("cv.profile.add_clearance")}</button>}>
          <SelectField path="user_entered.work_authorization_de" label={t("cv.profile.work_authorization")} className={styles.narrow}
            value={form.user_entered.work_authorization_de} options={WORK_AUTHORIZATIONS}
            text={(v) => serverText("work_authorization", v)} onChange={(v) => { setUser({ work_authorization_de: v }); }} />
          {clearances.map((clearance: Clearance, i) => {
            const path = `user_entered.security_clearances[${String(i)}]`;
            const set = (patch: Partial<Clearance>) => {
              setUser({ security_clearances: clearances.map((c, j) => (j === i ? { ...c, ...patch } : c)) });
            };
            return (
              <Item key={i} legend={t("cv.profile.clearance_n", { n: i + 1 })} removeLabel={t("cv.profile.remove_clearance")}
                onRemove={() => { setUser({ security_clearances: clearances.filter((_, j) => j !== i) }); }}>
                <TextField path={`${path}.name`} label={t("cv.profile.clearance_name")} value={clearance.name}
                  onChange={(v) => { set({ name: v ?? "" }); }} />
                <TextField path={`${path}.level`} label={t("cv.profile.clearance_level")} value={clearance.level} nullable
                  onChange={(v) => { set({ level: v }); }} />
              </Item>
            );
          })}
        </Section>
        <IssueSummary count={issueCount} unplaced={unplaced} />
        {save.error !== null && save.error.status !== 422 && <ProblemBox error={save.error} />}
        {save.error !== null && save.error.status === 422 && <p role="alert" className={controls.error}>{t("cv.profile.save_refused")}</p>}
        <div className={styles.actions}>
          <button type="submit" className={controls.secondary} disabled={save.isPending} aria-busy={save.isPending}>
            {t("cv.profile.save")}
          </button>
          <DiscardButton draftId={draftId} />
          {dirty && <span className={styles.muted}>{t("cv.profile.unsaved")}</span>}
          <button type="button" className={cx(controls.primary, styles.end)} disabled={save.isPending}
            onClick={() => { void confirm(); }}>
            {t("cv.profile.confirm")}
          </button>
        </div>
      </form>
      {confirming && (
        <ConfirmModal draftId={draftId} onCancel={() => { setConfirming(false); }}
          onInvalid={(errors) => { setConfirming(false); setFieldErrors(errors); }} />
      )}
    </IssuesContext.Provider>
  );
}

function IssueSummary({ count, unplaced }: { count: number; unplaced: Problem[] }) {
  const { t } = useTranslation();
  const headingId = useId();
  if (count === 0) {
    return <p className={styles.muted}>{t("cv.profile.no_issues")}</p>;
  }
  return (
    <section aria-labelledby={headingId} className={styles.summary}>
      <p id={headingId} className={styles.summaryTitle}>{t("cv.profile.issues", { count })}</p>
      {unplaced.length > 0 && (
        <ul role="list" className={styles.issues}>
          {unplaced.map((problem, i) => <IssueItem key={i} problem={problem} showPath />)}
        </ul>
      )}
    </section>
  );
}
