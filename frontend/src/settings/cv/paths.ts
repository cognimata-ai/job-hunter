// The profile form's field paths (plan M4 Task 27, resolution g). An issue names its field by the path syntax of
// `profile/validate.py`: names joined by `.`, a zero-based `[index]` after a list, from the root of the profile
// (`skills[0].level`, `user_entered.security_clearances[1].name`). A 422 names it as FastAPI does, under the
// body (`body.draft.skills[0].level` for a string no JSONB value holds, `body.draft.skills.0.level` for a
// refused member); both are normalised to the issue form, so one field shows both.
import type { ConfirmedProfile } from "./api";

export type PathPart = string | number;

const BODY_DRAFT = /^body\.draft(?:\.|$)/;
const DOTTED_INDEX = /\.(\d+)(?=\.|\[|$)/g;
const INDEX = /\[(\d+)\]/g;
const NUMBER = /^\d+$/;

/** A field path in the issue form: `body.draft.` dropped, a `.<n>` list index written `[n]`. */
export function normalizePath(path: string): string {
  return path.replace(BODY_DRAFT, "").replace(DOTTED_INDEX, "[$1]");
}

/** The parts of an issue-form path: names and list indexes. */
export function parsePath(path: string): PathPart[] {
  return path
    .replace(INDEX, ".$1")
    .split(".")
    .filter((part) => part !== "")
    .map((part) => (NUMBER.test(part) ? Number(part) : part));
}

/** `value` with the member at `path` replaced by `next`, every object and list on the way copied; `value` when
 * the path leads nowhere. */
export function setAt(value: unknown, path: readonly PathPart[], next: unknown): unknown {
  if (path.length === 0) {
    return next;
  }
  const [head, ...rest] = path;
  if (typeof head === "number" && Array.isArray(value) && head < value.length) {
    return value.map((item: unknown, i) => (i === head ? setAt(item, rest, next) : item));
  }
  if (typeof head === "string" && typeof value === "object" && value !== null && !Array.isArray(value) && head in value) {
    const record = value as Record<string, unknown>;
    return { ...record, [head]: setAt(record[head], rest, next) };
  }
  return value;
}

/** Every path the form shows a field (or a list) at, where its issues are shown inline; an issue at any other
 * path goes in the form's summary. A language's `code_other` is shown when the code is `other`, or when it holds
 * a value to clear. */
export function renderedPaths(profile: ConfirmedProfile): Set<string> {
  const paths = new Set<string>(["summary", "target_titles", "seniority", "total_years_experience", "roles", "skills"]);
  const each = (list: string, count: number, members: readonly string[]) => {
    paths.add(list);
    for (let i = 0; i < count; i++) {
      paths.add(`${list}[${String(i)}]`);
      for (const member of members) {
        paths.add(`${list}[${String(i)}].${member}`);
      }
    }
  };
  each("target_titles", profile.target_titles.length, ["title", "lang"]);
  each("roles", profile.roles.length, ["title", "employer", "start", "end", "seniority", "domain", "highlights"]);
  profile.roles.forEach((role, i) => {
    each(`roles[${String(i)}].highlights`, role.highlights.length, []);
  });
  each("skills", profile.skills.length, ["name", "category", "level", "years", "evidence"]);
  each("languages", profile.languages.length, ["code", "cefr", "evidence"]);
  profile.languages.forEach((language, i) => {
    if (language.code === "other" || language.code_other !== null) {
      paths.add(`languages[${String(i)}].code_other`);
    }
  });
  each("education", profile.education.length, ["level", "field", "institution", "year"]);
  each("certifications", profile.certifications.length, ["name", "issuer", "year"]);
  each("domains", profile.domains.length, []);
  paths.add("user_entered.work_authorization_de");
  each("user_entered.security_clearances", profile.user_entered?.security_clearances?.length ?? 0, ["name", "level"]);
  return paths;
}
