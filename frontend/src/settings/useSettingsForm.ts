// The one form state of the settings document (spec §13.2, §13.5, §15.7; plan M4 Task 28, D32, Rulings T28-a to
// T28-e): `SettingsFormProvider`, mounted once by Profile & Settings, holds one draft of the whole document for
// every section, one dirty set, one dry-run and one set of ticks; `useSettingsForm()` gives them to a section.
//
// - **Paths** are the server's field paths without `payload.` (`scope.locations[0].radius_km`,
//   `scope.language_policy.fr`). `set(path, value)` writes immutably (`undefined` drops a map key), marks the
//   path dirty, and drops the mark again once the value equals the base's.
// - **Entries of `queries[]` and `watchlist[]`** are marked by their id (Ruling T28-d), `queries[id=12].enabled`
//   for a toggle (`setEntry`) and `watchlist[id=9]` for an entry the draft added (`addEntry`), because an internal
//   save of another route reorders and resizes those lists. A rebase or a reapply takes the new base's list,
//   writes each marked toggle onto the entry of the same id, appends an added entry the new base lacks, and drops
//   a mark whose entry the new base no longer has.
// - **The dry-run** (Ruling T28-c) runs 500 ms after the last change, on the whole draft, and never while the
//   draft is the base. The latest answer wins: a change aborts the request in flight, and an answer for an older
//   draft is never taken. Until the current draft's answer is in, the form is `checking` and Save is disabled.
//   The base document is dry-run once after each load and rebase: its warnings are the standing ones (Ruling
//   T28-a). A tick is kept while its code is in the current dry-run; a code that leaves and comes back needs a
//   new one.
// - **Save** posts the whole draft with the base's version id, the ticked codes of the current dry-run (sorted)
//   and `eval_override_ack` when the override is ticked, under one `Idempotency-Key` per action. A 201 reads the
//   settings again and rebases with nothing dirty; a 409 `stale_base_version` offers Reload and reapply; a 409
//   `eval_gate_failed` offers the override; a 422 maps its `payload.` fields like the dry-run's errors.
// - **Other reads of `SETTINGS_KEY`** (Ruling T28-e): the base is captured at load and at each rebase. A read of
//   the same version changes nothing; a newer version is taken silently while nothing is dirty, and otherwise
//   keeps the base and offers the reload, as a stale save does. While a scope is pending, the settings are
//   polled every 3 s in the background; the poll never touches the draft.
// - While anything is dirty, leaving the page asks first (`beforeunload`); in-app navigation is not blocked.
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  createContext,
  createElement,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useReducer,
  useState,
} from "react";
import { useTranslation } from "react-i18next";
import { apiFetch, type ProblemError, toProblem } from "../api/client";
import { useKeyedMutation } from "../api/idempotency";
import type { components } from "../api/schema";
import { readSettings, SETTINGS_KEY, type SettingsOut } from "../app/AppShell";
import { useToast } from "../components/Toaster";
import type { SettingsSection } from "../pages/ProfileSettingsPage";
import { STATUS_KEY } from "../system/api";

export type { SettingsOut };
export type Settings = components["schemas"]["Settings"];
export type DryRunOut = components["schemas"]["DryRunOut"];
export type WarningOut = components["schemas"]["WarningOut"];
export type ProjectionsOut = components["schemas"]["ProjectionsOut"];
export type EffectClass = DryRunOut["effects"][number];
export type SaveOut = components["schemas"]["SaveOut"];
/** The save's body: `eval_override_ack` only when the override is ticked. */
export type SaveBody = Omit<components["schemas"]["SaveIn"], "eval_override_ack"> & { eval_override_ack?: true };

/** The lists whose entries are marked by id (Ruling T28-d). */
export type EntryList = "queries" | "watchlist";

/** A refused field as the form shows it: its path (without `payload.`), its code and the server's message. */
export interface Issue {
  path: string;
  code: string;
  message: string;
}

export const DRY_RUN_DELAY_MS = 500;
export const SCOPE_POLL_MS = 3000;
const DRY_RUN_PATH = "/api/settings/dry-run";
const ROOT = "payload.";

// --- paths ---

type Segment = string | number;

/** `scope.locations[0].radius_km` as `["scope", "locations", 0, "radius_km"]` (an entry's mark, `[id=9]`, as the
 * key `id=9`). */
export function parsePath(path: string): Segment[] {
  const segments: Segment[] = [];
  for (const part of path.split(".")) {
    const [name, ...indexes] = part.split("[");
    if (name !== "") {
      segments.push(name);
    }
    for (const index of indexes) {
      const inner = index.replace(/\]$/, "");
      segments.push(/^\d+$/.test(inner) ? Number(inner) : inner);
    }
  }
  return segments;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function child(node: unknown, segment: Segment): unknown {
  if (typeof segment === "number") {
    return Array.isArray(node) ? (node as unknown[])[segment] : undefined;
  }
  return isRecord(node) ? node[segment] : undefined;
}

/** The value at `path` of `doc`; `undefined` where it is absent. */
export function getAt(doc: unknown, path: string): unknown {
  return parsePath(path).reduce<unknown>((node, segment) => child(node, segment), doc);
}

function setIn(node: unknown, segments: readonly Segment[], value: unknown): unknown {
  if (segments.length === 0) {
    return value;
  }
  const [head, ...rest] = segments as readonly [Segment, ...Segment[]];
  if (typeof head === "number") {
    const list = Array.isArray(node) ? [...(node as unknown[])] : [];
    list[head] = setIn(list[head], rest, value);
    return list;
  }
  const record = isRecord(node) ? node : {};
  const next = setIn(record[head], rest, value);
  if (next === undefined) {
    return Object.fromEntries(Object.entries(record).filter(([key]) => key !== head));
  }
  return { ...record, [head]: next };
}

/** `doc` with `value` at `path`, every object on the way copied; `undefined` drops the key. */
export function setAt<T>(doc: T, path: string, value: unknown): T {
  return setIn(doc, parsePath(path), value) as T;
}

/** Whether every container above `path` exists in `doc` (a list index within its list). */
function reachable(doc: unknown, path: string): boolean {
  const segments = parsePath(path);
  let node: unknown = doc;
  for (const segment of segments.slice(0, -1)) {
    node = child(node, segment);
    if (node === undefined || node === null) {
      return false;
    }
  }
  const last = segments.at(-1);
  return typeof last === "number" ? Array.isArray(node) && last < (node as unknown[]).length : isRecord(node);
}

/** Two JSON values alike: an absent key and an `undefined` one are the same. */
export function deepEqual(a: unknown, b: unknown): boolean {
  if (a === b) {
    return true;
  }
  if (Array.isArray(a) || Array.isArray(b)) {
    return (
      Array.isArray(a) && Array.isArray(b) && a.length === b.length && a.every((value, i) => deepEqual(value, b[i]))
    );
  }
  if (isRecord(a) && isRecord(b)) {
    const keys = new Set([...Object.keys(a), ...Object.keys(b)]);
    return [...keys].every((key) => deepEqual(a[key], b[key]));
  }
  return false;
}

/** The `id` of the field at `path` (its input, its group). */
export function fieldId(path: string): string {
  return `field-${path.replace(/[^A-Za-z0-9_]+/g, "-").replace(/-+$/, "")}`;
}

/** The id of the `index`-th issue in the panel of `section`. */
export function issueId(section: SettingsSection, index: number): string {
  return `${section}-issue-${String(index)}`;
}

/** Whether the field at `field` holds the issue at `issue`: its own path, or (unless `exact`) one inside it. */
export function claims(field: string, issue: string, exact = false): boolean {
  return issue === field || (!exact && (issue.startsWith(`${field}.`) || issue.startsWith(`${field}[`)));
}

// The section of each top-level group (spec §15.7's table): the scoring group's budget fields are Budget & quota's.
const SECTION_OF = {
  schedule: "schedule",
  display: "system",
  scope: "scope",
  evaluation: "scoring",
  sources: "queries",
  queries: "queries",
  watchlist: "watchlist",
  collection: "queries",
  quota: "queries",
  scoring: "scoring",
  llm: "budget",
  retention: "privacy",
  notifications: "system",
} as const satisfies Record<keyof Settings, SettingsSection>;
const BUDGET_SCORING: ReadonlySet<string> = new Set([
  "max_targets_per_cycle",
  "min_cycle_targets",
  "batch_bulk_rescore",
  "batch_min_targets",
  "batch_max_items",
  "bulk_rescore_max_budget_share",
] satisfies (keyof Settings["scoring"])[]);

/** The section whose fields hold `path`; `null` for a path outside the document. */
export function sectionOf(path: string): SettingsSection | null {
  const [top, second] = parsePath(path);
  if (top === "scoring" && typeof second === "string" && BUDGET_SCORING.has(second)) {
    return "budget";
  }
  return typeof top === "string" && top in SECTION_OF ? SECTION_OF[top as keyof Settings] : null;
}

// --- entries by id ---

const ID_KEY = { queries: "query_id", watchlist: "watchlist_company_id" } as const satisfies Record<EntryList, string>;
const ENTRY_PATH = /^(queries|watchlist)\[id=(\d+)\](\.enabled)?$/;

interface Entry {
  enabled: boolean;
  [id: string]: number | boolean;
}

/** The mark of the entry `id` of `list` (`watchlist[id=9]`); a toggle's mark adds `.enabled`. */
export function entryPath(list: EntryList, id: number): string {
  return `${list}[id=${String(id)}]`;
}

function entriesOf(doc: Settings, list: EntryList): Entry[] {
  return doc[list];
}

function findEntry(doc: Settings, list: EntryList, id: number): Entry | undefined {
  return entriesOf(doc, list).find((entry) => entry[ID_KEY[list]] === id);
}

function withEntries(doc: Settings, list: EntryList, entries: Entry[]): Settings {
  return { ...doc, [list]: entries };
}

function setEnabled(doc: Settings, list: EntryList, id: number, enabled: boolean): Settings {
  const key = ID_KEY[list];
  return withEntries(doc, list, entriesOf(doc, list).map((entry) => (entry[key] === id ? { ...entry, enabled } : entry)));
}

/** Whether `path` of `draft` differs from `base`. */
function isDirty(base: Settings, draft: Settings, path: string): boolean {
  const entry = ENTRY_PATH.exec(path);
  if (entry === null) {
    return !deepEqual(getAt(draft, path), getAt(base, path));
  }
  const [, list, id, toggle] = entry as unknown as [string, EntryList, string, string | undefined];
  const mine = findEntry(draft, list, Number(id));
  const theirs = findEntry(base, list, Number(id));
  if (mine === undefined) {
    return false;
  }
  return toggle === undefined ? !deepEqual(mine, theirs) : theirs?.enabled !== mine.enabled;
}

function depth(path: string): number {
  return parsePath(path).length;
}

const LOCATION_PATH = /^scope\.locations\[(\d+)\](.+)$/;

/** Where the mark `path` of `draft` lands in `next`: a mark inside a location (`scope.locations[i].…`) at the
 * index of the same place (`gazetteer_id`, Ruling T28-g), `null` when `next` has no such place; any other mark at
 * its own path. */
function remapped(path: string, draft: Settings, next: Settings): string | null {
  const inside = LOCATION_PATH.exec(path);
  if (inside === null) {
    return path;
  }
  const [, index, rest] = inside as unknown as [string, string, string];
  const place = draft.scope.locations.at(Number(index));
  const at = place === undefined ? -1 : next.scope.locations.findIndex((other) => other.gazetteer_id === place.gazetteer_id);
  return at < 0 ? null : `scope.locations[${String(at)}]${rest}`;
}

/** The dirty paths' values of `draft` written onto `base` (module docstring): the plain paths shallowest first
 * (a location's by its place, Ruling T28-g), then the entries by id; a path whose place `base` lacks is dropped
 * with its mark. */
export function overlay(base: Settings, draft: Settings, dirty: ReadonlySet<string>): { draft: Settings; dirty: Set<string> } {
  let next = base;
  const kept: string[] = [];
  const plain = [...dirty].filter((path) => !ENTRY_PATH.test(path)).sort((a, b) => depth(a) - depth(b));
  for (const path of plain) {
    const target = remapped(path, draft, next);
    if (target !== null && reachable(next, target)) {
      next = setAt(next, target, getAt(draft, path));
      kept.push(target);
    }
  }
  for (const path of [...dirty].filter((p) => ENTRY_PATH.test(p))) {
    const [, list, idText, toggle] = ENTRY_PATH.exec(path) as unknown as [string, EntryList, string, string | undefined];
    const id = Number(idText);
    const mine = findEntry(draft, list, id);
    if (mine === undefined) {
      continue;
    }
    if (findEntry(next, list, id) !== undefined) {
      next = setEnabled(next, list, id, mine.enabled);
    } else if (toggle === undefined) {
      next = withEntries(next, list, [...entriesOf(next, list), mine]);
    } else {
      continue; // the new base no longer has the entry
    }
    kept.push(path);
  }
  return { draft: next, dirty: new Set(kept.filter((path) => isDirty(base, next, path))) };
}

// --- state ---

type Adopting = { mode: "clean"; version: number } | { mode: "reapply"; clearTicks: boolean };

interface Answer<T> {
  draft: Settings;
  nonce: number;
  value: T;
}

interface State {
  base: SettingsOut;
  draft: Settings;
  dirty: ReadonlySet<string>;
  /** Counts the bases: the base document is dry-run once for each. */
  baseEpoch: number;
  baseDryRun: DryRunOut | null;
  dryRun: Answer<DryRunOut> | null;
  dryRunError: Answer<ProblemError> | null;
  /** A new dry-run of the same draft (Retry, a refused save). */
  nonce: number;
  ticks: ReadonlySet<string>;
  evalOverride: boolean;
  /** Settings changed elsewhere: the offer to reload and reapply. */
  stale: boolean;
  /** A read of the settings the form is waiting for: the saved version, or a rebase's. */
  adopting: Adopting | null;
  /** The save in flight or waiting for its version (Ruling T28-f): the document it posted, and the marks of the
   * edits made since, which the adopt keeps. */
  posted: { payload: Settings; edited: ReadonlySet<string> } | null;
  /** The saved version's re-read failed past its retries: its version and the problem (Retry reads again). */
  reread: { version: number; error: ProblemError } | null;
  saveError: ProblemError | null;
  saveIssues: readonly Issue[];
  /** The section of the last change or Save: where the panel raises its alerts. */
  active: SettingsSection | null;
}

type Action =
  | { type: "settings"; data: SettingsOut; settled: boolean }
  | { type: "set"; path: string; value: unknown }
  | { type: "setEntry"; list: EntryList; id: number; enabled: boolean }
  | { type: "addEntry"; list: EntryList; id: number }
  | { type: "baseDryRun"; epoch: number; result: DryRunOut }
  | { type: "dryRun"; draft: Settings; nonce: number; result: DryRunOut }
  | { type: "dryRunFailed"; draft: Settings; nonce: number; error: ProblemError }
  | { type: "retryDryRun" }
  | { type: "tick"; code: string; ticked: boolean }
  | { type: "evalOverride"; on: boolean }
  | { type: "adopting"; adopting: Adopting }
  | { type: "adoptFailed" }
  | { type: "saveFailed"; error: ProblemError }
  | { type: "saving"; from: SettingsSection | null; posted: Settings }
  | { type: "rereadFailed"; version: number; error: ProblemError };

const NOTHING: ReadonlySet<string> = new Set();

function codesOf(result: DryRunOut | null): ReadonlySet<string> {
  return new Set(result?.warnings.map((warning) => warning.code) ?? []);
}

function keepOnly(ticks: ReadonlySet<string>, codes: ReadonlySet<string>): ReadonlySet<string> {
  return new Set([...ticks].filter((code) => codes.has(code)));
}

function initial(data: SettingsOut): State {
  return {
    base: data,
    draft: data.payload,
    dirty: NOTHING,
    baseEpoch: 1,
    baseDryRun: null,
    dryRun: null,
    dryRunError: null,
    nonce: 0,
    ticks: NOTHING,
    evalOverride: false,
    stale: false,
    adopting: null,
    posted: null,
    reread: null,
    saveError: null,
    saveIssues: [],
    active: null,
  };
}

function adoptClean(state: State, data: SettingsOut): State {
  return { ...initial(data), baseEpoch: state.baseEpoch + 1, nonce: state.nonce, active: state.active };
}

/** The saved version taken as the base (Ruling T28-f): every path where the draft differs from what the save
 * posted (an edit made while the save was in flight or read) is written onto it, and stays dirty. */
function adoptSaved(state: State, data: SettingsOut): State {
  const clean = adoptClean(state, data);
  const posted = state.posted;
  if (posted === null) {
    return clean;
  }
  const edited = new Set([...state.dirty, ...posted.edited].filter((path) => isDirty(posted.payload, state.draft, path)));
  if (edited.size === 0) {
    return clean;
  }
  const next = overlay(data.payload, state.draft, edited);
  return { ...clean, draft: next.draft, dirty: next.dirty };
}

function adoptReapply(state: State, data: SettingsOut, clearTicks: boolean): State {
  const next = overlay(data.payload, state.draft, state.dirty);
  return {
    ...state,
    base: data,
    draft: next.draft,
    dirty: next.dirty,
    baseEpoch: state.baseEpoch + 1,
    baseDryRun: null,
    dryRun: next.dirty.size === 0 ? null : state.dryRun,
    dryRunError: null,
    ticks: clearTicks ? NOTHING : state.ticks,
    stale: false,
    adopting: null,
    posted: null,
    reread: null,
    saveError: null,
    saveIssues: [],
  };
}

/** The state after the draft became `draft` by a change at `path`, `candidates` the paths that may be dirty now. */
function changed(state: State, draft: Settings, path: string, candidates: readonly string[]): State {
  const dirty = new Set(candidates.filter((path) => isDirty(state.base.payload, draft, path)));
  const clean = dirty.size === 0;
  return {
    ...state,
    draft,
    dirty,
    dryRun: clean ? null : state.dryRun,
    dryRunError: clean ? null : state.dryRunError,
    ticks: clean ? keepOnly(state.ticks, codesOf(state.baseDryRun)) : state.ticks,
    evalOverride: false,
    saveError: null,
    saveIssues: [],
    active: sectionOf(path) ?? state.active,
    posted: state.posted === null ? null : { ...state.posted, edited: new Set([...state.posted.edited, path]) },
  };
}

function isEvalGate(error: ProblemError): boolean {
  return error.code === "eval_gate_failed" || (error.code === "conflict_state" && error.detail === "eval_gate_failed");
}

function reducer(state: State, action: Action): State {
  switch (action.type) {
    case "settings": {
      const { data } = action;
      const adopting = state.adopting;
      const saved = adopting?.mode === "clean" ? adopting.version : state.reread?.version;
      if (saved !== undefined) {
        return data.version.id >= saved ? adoptSaved(state, data) : state;
      }
      if (adopting?.mode === "reapply") {
        return action.settled ? adoptReapply(state, data, adopting.clearTicks) : state;
      }
      // a save in flight: its answer decides what a read means (the saved version, or a stale base)
      if (state.posted !== null) {
        return state;
      }
      // a newer version has a greater id (ids are serial, M1): the same or a late older answer changes nothing
      if (data.version.id <= state.base.version.id) {
        return state;
      }
      if (state.dirty.size === 0) {
        return adoptClean(state, data);
      }
      return state.stale ? state : { ...state, stale: true };
    }
    case "set":
      if (deepEqual(getAt(state.draft, action.path), action.value)) {
        return state;
      }
      return changed(state, setAt(state.draft, action.path, action.value), action.path, [...state.dirty, action.path]);
    case "setEntry": {
      const entry = findEntry(state.draft, action.list, action.id);
      if (entry === undefined || entry.enabled === action.enabled) {
        return state;
      }
      const whole = entryPath(action.list, action.id);
      const mark = state.dirty.has(whole) ? whole : `${whole}.enabled`;
      return changed(state, setEnabled(state.draft, action.list, action.id, action.enabled), mark, [...state.dirty, mark]);
    }
    case "addEntry": {
      if (findEntry(state.draft, action.list, action.id) !== undefined) {
        return state;
      }
      const added: Entry = { [ID_KEY[action.list]]: action.id, enabled: true };
      const draft = withEntries(state.draft, action.list, [...entriesOf(state.draft, action.list), added]);
      const mark = entryPath(action.list, action.id);
      return changed(state, draft, mark, [...state.dirty, mark]);
    }
    case "baseDryRun":
      if (action.epoch !== state.baseEpoch) {
        return state;
      }
      return {
        ...state,
        baseDryRun: action.result,
        ticks: state.dirty.size === 0 ? keepOnly(state.ticks, codesOf(action.result)) : state.ticks,
      };
    case "dryRun":
      if (action.draft !== state.draft || action.nonce !== state.nonce) {
        return state; // an answer for an older draft never replaces a newer one
      }
      return {
        ...state,
        dryRun: { draft: action.draft, nonce: action.nonce, value: action.result },
        dryRunError: null,
        ticks: keepOnly(state.ticks, codesOf(action.result)),
        saveIssues: [],
      };
    case "dryRunFailed":
      if (action.draft !== state.draft || action.nonce !== state.nonce) {
        return state;
      }
      return { ...state, dryRunError: { draft: action.draft, nonce: action.nonce, value: action.error } };
    case "retryDryRun":
      return { ...state, nonce: state.nonce + 1, dryRunError: null };
    case "tick": {
      const ticks = new Set(state.ticks);
      if (action.ticked) {
        ticks.add(action.code);
      } else {
        ticks.delete(action.code);
      }
      return { ...state, ticks };
    }
    case "evalOverride":
      return { ...state, evalOverride: action.on };
    case "adopting":
      return { ...state, adopting: action.adopting, reread: null };
    case "rereadFailed":
      return { ...state, adopting: null, reread: { version: action.version, error: action.error } };
    case "adoptFailed":
      return { ...state, adopting: null };
    case "saving":
      return { ...state, active: action.from ?? state.active, saveError: null, posted: { payload: action.posted, edited: NOTHING } };
    case "saveFailed": {
      const { error } = action;
      const unsent = { ...state, posted: null };
      if (error.status === 409 && error.code === "stale_base_version") {
        return { ...unsent, stale: true, saveError: null };
      }
      if (error.status === 422) {
        const issues = error.errors
          .filter((issue) => issue.field.startsWith(ROOT))
          .map((issue) => ({ path: issue.field.slice(ROOT.length), code: issue.code, message: issue.message }));
        // a refusal of the acknowledgements: the dry-run is read again, and the problem shown beside it
        const other = issues.length < error.errors.length || issues.length === 0;
        return { ...unsent, saveIssues: issues, saveError: other ? error : null, nonce: state.nonce + 1, dryRunError: null };
      }
      return { ...unsent, saveError: error };
    }
  }
}

// --- the form ---

export interface SettingsForm {
  /** The settings version the draft started from (Ruling T28-e: moved only by a rebase). */
  base: SettingsOut;
  draft: Settings;
  /** The changed paths (module docstring). */
  dirty: ReadonlySet<string>;
  /** The latest dry-run of the draft while anything is dirty (an older draft's while `checking`), else `null`. */
  dryRun: DryRunOut | null;
  /** The ticked warning codes. */
  ticks: ReadonlySet<string>;
  evalOverride: boolean;
  /** The current draft's dry-run is not in yet: "Checking…", and Save disabled. */
  checking: boolean;
  /** The current draft's dry-run failed (a network failure, a 5xx). */
  dryRunError: ProblemError | null;
  /** The base document's own dry-run (its warnings are the standing ones). */
  baseDryRun: DryRunOut | null;
  /** The codes of the standing warnings: those of the base's own dry-run (Ruling T28-a). */
  standing: ReadonlySet<string>;
  /** The refused fields shown: the dry-run's errors, then a refused save's. */
  issues: readonly Issue[];
  /** Settings changed elsewhere: Reload and reapply is offered. */
  stale: boolean;
  /** A save is sent, or its new version is being read. */
  saving: boolean;
  /** Why the last save was refused (none for `stale_base_version`, which is `stale`). */
  saveError: ProblemError | null;
  /** The last save failed the eval gate: the override is offered. */
  evalGateFailed: boolean;
  /** The saved version could not be read again (Ruling T28-f): the problem, with Retry (`retryReread`); Save
   * stays disabled until a read brings the saved version. */
  rereadError: ProblemError | null;
  /** The section of the last change or Save: the one panel that raises alerts (the others show them quietly). */
  activeSection: SettingsSection | null;
  canSave: boolean;
  /** The scope being applied, as the latest read of the settings has it. */
  pendingScope: SettingsOut["pending_scope"];
  set: (path: string, value: unknown) => void;
  /** Toggles the entry `id` of `list` (marked by its id). */
  setEntry: (list: EntryList, id: number, enabled: boolean) => void;
  /** Adds the entry `id` to `list`, enabled (marked by its id). */
  addEntry: (list: EntryList, id: number) => void;
  tick: (code: string, ticked: boolean) => void;
  setEvalOverride: (on: boolean) => void;
  /** Saves the draft; `from` is the section whose Save was pressed. */
  save: (from?: SettingsSection) => void;
  /** After an internal save of another route: reads the settings, then writes the dirty paths onto them. */
  rebase: () => Promise<void>;
  /** The offer's Reload and reapply: as `rebase`, and the ticks are cleared. */
  reapply: () => Promise<void>;
  retryDryRun: () => void;
  /** Reads the saved version again after a failed re-read. */
  retryReread: () => void;
}

/** What a section shows before the form is there: the settings read's state. A failed read is one alert for
 * the page: the first framed section's (`alertFor`); each frame registers itself while the read has failed. */
export interface SettingsLoad {
  error: unknown;
  retry: () => void;
  alertFor: SettingsSection | null;
  /** Registers a frame; the function it returns unregisters it. */
  frame: (section: SettingsSection) => () => void;
}

const FormContext = createContext<SettingsForm | null>(null);
const LoadContext = createContext<SettingsLoad | null>(null);
const SectionContext = createContext<SettingsSection | null>(null);

/** The form; inside a loaded `SettingsFormProvider` only (a `SectionFrame`'s body). */
export function useSettingsForm(): SettingsForm {
  const form = useContext(FormContext);
  if (form === null) {
    throw new Error("useSettingsForm is used outside a loaded SettingsFormProvider");
  }
  return form;
}

/** The form, or `null` while the settings are not read yet. */
export function useSettingsFormOrNull(): SettingsForm | null {
  return useContext(FormContext);
}

/** The settings read's error and retry, while the form is not there. */
export function useSettingsLoad(): SettingsLoad | null {
  return useContext(LoadContext);
}

/** The section a field or panel is in (its `SectionFrame`). */
export function useSection(): SettingsSection | null {
  return useContext(SectionContext);
}

export function SectionProvider({ section, children }: { section: SettingsSection; children: ReactNode }) {
  return createElement(SectionContext.Provider, { value: section }, children);
}

function dryRun(payload: Settings, signal: AbortSignal): Promise<DryRunOut> {
  return apiFetch<DryRunOut>(DRY_RUN_PATH, { method: "POST", body: JSON.stringify({ payload }), signal });
}

export function SettingsFormProvider({ children }: { children: ReactNode }) {
  const { t } = useTranslation();
  const toast = useToast();
  const queryClient = useQueryClient();
  const [frames, setFrames] = useState<readonly SettingsSection[]>([]);
  // the shell's own query (the same key and read); a second observer reads nothing more on mount
  const settings = useQuery({ queryKey: SETTINGS_KEY, queryFn: ({ signal }) => readSettings({ signal }), refetchOnMount: false });
  const data = settings.data;
  const [state, dispatch] = useReducer(
    (current: State | null, action: Action | { type: "load"; data: SettingsOut }) => {
      if (action.type === "load") {
        return current ?? initial(action.data);
      }
      return current === null ? current : reducer(current, action);
    },
    data,
    (loaded) => (loaded === undefined ? null : initial(loaded)),
  );

  // every read of the settings (a refetch, a poll, a save's or a rebase's), Ruling T28-e; and once nothing is dirty
  // any more, the newer version a read brought meanwhile
  const clean = state === null || state.dirty.size === 0;
  useEffect(() => {
    if (data !== undefined) {
      dispatch({ type: "load", data });
      dispatch({ type: "settings", data, settled: false });
    }
  }, [data, clean]);

  // the base document's own dry-run, once per base
  const baseEpoch = state?.baseEpoch;
  const basePayload = state?.base.payload;
  useEffect(() => {
    if (baseEpoch === undefined || basePayload === undefined) {
      return;
    }
    const controller = new AbortController();
    dryRun(basePayload, controller.signal).then(
      (result) => { dispatch({ type: "baseDryRun", epoch: baseEpoch, result }); },
      () => undefined, // no standing warning is known: each warning is shown in full
    );
    return () => { controller.abort(); };
  }, [baseEpoch, basePayload]);

  // the draft's dry-run, 500 ms after the last change
  const dirtyNow = state !== null && state.dirty.size > 0;
  const draft = state?.draft;
  const nonce = state?.nonce ?? 0;
  useEffect(() => {
    if (!dirtyNow || draft === undefined) {
      return;
    }
    const controller = new AbortController();
    const timer = setTimeout(() => {
      dryRun(draft, controller.signal).then(
        (result) => { dispatch({ type: "dryRun", draft, nonce, result }); },
        (error: unknown) => {
          if (!controller.signal.aborted) {
            dispatch({ type: "dryRunFailed", draft, nonce, error: toProblem(error) });
          }
        },
      );
    }, DRY_RUN_DELAY_MS);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [dirtyNow, draft, nonce]);

  // the scope-apply poll: every 3 s in the background while a scope is pending, its answer written into the one
  // query (not through it, so the query keeps its own read)
  const pending = data?.pending_scope != null;
  useEffect(() => {
    if (!pending) {
      return;
    }
    const controller = new AbortController();
    const timer = setInterval(() => {
      readSettings({ signal: controller.signal, background: true }).then(
        (read) => {
          // a late answer older than the version read meanwhile (a save's re-read) is never written (M1)
          const cached = queryClient.getQueryData<SettingsOut>(SETTINGS_KEY);
          if (!controller.signal.aborted && (cached === undefined || read.version.id >= cached.version.id)) {
            queryClient.setQueryData(SETTINGS_KEY, read);
          }
        },
        () => undefined, // the next poll tries again
      );
    }, SCOPE_POLL_MS);
    return () => {
      clearInterval(timer);
      controller.abort();
    };
  }, [pending, queryClient]);

  // leaving the page with unsaved changes asks first
  useEffect(() => {
    if (!dirtyNow) {
      return;
    }
    const guard = (event: BeforeUnloadEvent) => {
      event.preventDefault();
    };
    window.addEventListener("beforeunload", guard);
    return () => { window.removeEventListener("beforeunload", guard); };
  }, [dirtyNow]);

  // What a committed save must do even when the page is left before its answer (M2): the notice, and the status
  // and the settings read again (the hook's own `onSuccess`, which runs without an observer). The form's own
  // adopt is the `mutate` call's.
  const mutation = useKeyedMutation(
    (body: SaveBody, key: string) =>
      apiFetch<SaveOut>("/api/settings", { method: "POST", body: JSON.stringify(body), idempotencyKey: key }),
    {
      onSuccess: (out) => {
        toast({ message: t("settings_form.saved", { version: out.version.version_no }) });
        void queryClient.invalidateQueries({ queryKey: STATUS_KEY });
        void queryClient.invalidateQueries({ queryKey: SETTINGS_KEY, exact: true });
      },
    },
  );

  // Takes the saved version `version` (Ruling T28-f): from the cache when a read already brought it, else from the
  // read in flight (the hook's invalidation) or a new one. A read failing past its retries is the panel's problem.
  const reread = useCallback(async (version: number) => {
    dispatch({ type: "adopting", adopting: { mode: "clean", version } });
    const cached = queryClient.getQueryData<SettingsOut>(SETTINGS_KEY);
    if (cached === undefined || cached.version.id < version) {
      try {
        await queryClient.refetchQueries({ queryKey: SETTINGS_KEY, exact: true }, { throwOnError: true, cancelRefetch: false });
      } catch (error) {
        dispatch({ type: "rereadFailed", version, error: toProblem(error) });
        return;
      }
    }
    const read = queryClient.getQueryData<SettingsOut>(SETTINGS_KEY);
    if (read !== undefined) {
      dispatch({ type: "settings", data: read, settled: true });
    }
  }, [queryClient]);

  // Reads the settings for a rebase, then writes the dirty paths onto them.
  const rebase = useCallback(async (clearTicks: boolean) => {
    dispatch({ type: "adopting", adopting: { mode: "reapply", clearTicks } });
    try {
      await queryClient.refetchQueries({ queryKey: SETTINGS_KEY, exact: true }, { throwOnError: true });
    } catch {
      dispatch({ type: "adoptFailed" });
      return;
    }
    const read = queryClient.getQueryData<SettingsOut>(SETTINGS_KEY);
    if (read !== undefined) {
      dispatch({ type: "settings", data: read, settled: true });
    }
  }, [queryClient]);

  const form = useMemo((): SettingsForm | null => {
    if (state === null) {
      return null;
    }
    const dirty = state.dirty.size > 0;
    const answered = (answer: Answer<unknown> | null) =>
      answer !== null && answer.draft === state.draft && answer.nonce === state.nonce;
    const current = answered(state.dryRun) ? state.dryRun?.value ?? null : null;
    const failed = answered(state.dryRunError) ? state.dryRunError?.value ?? null : null;
    const shown = dirty ? (state.dryRun?.value ?? null) : null;
    const saving = mutation.isPending || state.adopting?.mode === "clean";
    const canSave =
      dirty &&
      !state.stale &&
      !saving &&
      state.adopting === null &&
      state.reread === null &&
      current !== null &&
      current.errors.length === 0 &&
      state.saveIssues.length === 0 &&
      current.warnings.every((warning) => state.ticks.has(warning.code));
    const issues: Issue[] = [
      ...(shown?.errors ?? []).map((issue) => ({
        path: issue.field.startsWith(ROOT) ? issue.field.slice(ROOT.length) : issue.field,
        code: issue.code,
        message: issue.message,
      })),
      ...state.saveIssues,
    ];
    const value: SettingsForm = {
      base: state.base,
      draft: state.draft,
      dirty: state.dirty,
      dryRun: shown,
      ticks: state.ticks,
      evalOverride: state.evalOverride,
      checking: dirty && current === null && failed === null,
      dryRunError: failed,
      baseDryRun: state.baseDryRun,
      standing: codesOf(state.baseDryRun),
      issues,
      stale: state.stale,
      saving,
      saveError: state.saveError,
      evalGateFailed: state.saveError !== null && isEvalGate(state.saveError),
      rereadError: state.reread?.error ?? null,
      activeSection: state.active,
      canSave,
      pendingScope: data === undefined ? state.base.pending_scope : data.pending_scope,
      set: (path, next) => { dispatch({ type: "set", path, value: next }); },
      setEntry: (list, id, enabled) => { dispatch({ type: "setEntry", list, id, enabled }); },
      addEntry: (list, id) => { dispatch({ type: "addEntry", list, id }); },
      tick: (code, ticked) => { dispatch({ type: "tick", code, ticked }); },
      setEvalOverride: (on) => { dispatch({ type: "evalOverride", on }); },
      save: (from) => {
        if (!canSave) {
          return;
        }
        dispatch({ type: "saving", from: from ?? null, posted: state.draft });
        const codes = codesOf(current);
        const body: SaveBody = {
          payload: state.draft,
          base_version_id: state.base.version.id,
          acknowledged_warnings: [...state.ticks].filter((code) => codes.has(code)).sort(),
          ...(state.evalOverride ? { eval_override_ack: true as const } : {}),
        };
        mutation.mutate(body, {
          onSuccess: (out) => { void reread(out.version.id); },
          onError: (error) => { dispatch({ type: "saveFailed", error }); },
        });
      },
      rebase: () => rebase(false),
      reapply: () => rebase(true),
      retryDryRun: () => { dispatch({ type: "retryDryRun" }); },
      retryReread: () => {
        if (state.reread !== null) {
          void reread(state.reread.version);
        }
      },
    };
    return value;
  }, [state, data, mutation, rebase, reread]);

  const frame = useCallback((section: SettingsSection) => {
    setFrames((current) => (current.includes(section) ? current : [...current, section]));
    return () => { setFrames((current) => current.filter((other) => other !== section)); };
  }, []);
  const load = useMemo(
    (): SettingsLoad => ({
      error: settings.error,
      retry: () => { void settings.refetch(); },
      alertFor: frames.at(0) ?? null,
      frame,
    }),
    [settings, frames, frame],
  );

  return createElement(
    LoadContext.Provider,
    { value: load },
    createElement(FormContext.Provider, { value: form }, children),
  );
}
