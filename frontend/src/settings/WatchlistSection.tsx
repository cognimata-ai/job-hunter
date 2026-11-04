// The watchlist (spec §5.4, §15.7; plan M4 Task 29, resolution d): adding a company's board by its careers URL
// or by ATS and slug, and the list of the companies. A company's enable writes the draft (its `watchlist[]`
// entry by `watchlist_company_id`); its add and its removal are writes of their own routes, each with an
// internal settings save, after which the form rebases.
//
// - **By careers URL**: `POST /api/watchlist/parse-url {url}` reads the URL by its pattern alone (no request to
//   the ATS), 300 ms after the last keystroke or at once on [Detect]: the ATS (through `source`), the slug and
//   Lever's region, or why it names no board (`watchlist_unsupported`).
// - **By ATS and slug**: the ATS, the slug and, for Lever only and required there, its region (`global`, `eu`).
// - **Both** take a display name of 1 to 120 characters (code points, trimmed), the slug until the user writes
//   one, and send `POST /api/watchlist {display_name, ats_type, board_slug, lever_region?, input_url?}`
//   (`input_url` on the URL's path only). A 201 rebases and reads the list again, where the company is
//   `unvalidated` until its validation ends; a 409 says the board is on the list; a refusal shows its errors.
// - **The list** (`GET /api/watchlist`): each company's name and board, validation, last complete snapshot (in
//   the display time zone, or "Never"), its board's health and its postings; [Remove] asks first, then
//   `DELETE /api/watchlist/{id}` (204) rebases. Neither write takes an `Idempotency-Key`; each write's follow-up
//   (the rebase, the list read again) is its mutation's own, so it runs also once its form or dialog is gone.
import { useMutation, useQuery, useQueryClient, type UseQueryResult } from "@tanstack/react-query";
import { type SyntheticEvent, type ReactNode, useId, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { apiSend, type ProblemError, toProblem } from "../api/client";
import type { components } from "../api/schema";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { cx } from "../components/cx";
import { ProblemBox } from "../components/ProblemBox";
import { Skeleton } from "../components/Skeleton";
import { useDebounced } from "../components/useDebounced";
import { useServerText } from "../i18n/index";
import { useFormat } from "../system/format";
import { closedSet } from "./fields";
import fields from "./fields.module.css";
import { EntryToggle } from "./EntryToggle";
import { conflictId, useLatestForm } from "./queriesApi";
import tables from "./tables.module.css";
import { useSettingsForm } from "./useSettingsForm";
import styles from "./WatchlistSection.module.css";

export type WatchlistCompanyOut = components["schemas"]["WatchlistCompanyOut"];
type WatchlistIn = components["schemas"]["WatchlistIn"];
type WatchlistCreatedOut = components["schemas"]["WatchlistCreatedOut"];
type ParsedUrlOut = components["schemas"]["ParsedUrlOut"];
type UnsupportedUrlOut = components["schemas"]["UnsupportedUrlOut"];
type AtsType = WatchlistIn["ats_type"];
type LeverRegion = NonNullable<WatchlistIn["lever_region"]>;

export const WATCHLIST_KEY = ["watchlist"] as const;
const parseKey = (url: string) => ["watchlist-url", url] as const;
/** How long after the last keystroke a careers URL is read. */
export const PARSE_DELAY_MS = 300;
/** A careers URL's longest form (`watchlist.INPUT_URL_MAX_CHARS`). */
const URL_MAX_CHARS = 2048;
/** A display name: 1 to 120 characters once trimmed (`watchlist.DISPLAY_NAME_CHARS`, the column's CHECK). */
const NAME_CHARS = [1, 120] as const;
const ATS_TYPES = closedSet<AtsType>({
  greenhouse: true,
  lever: true,
  ashby: true,
  personio: true,
  recruitee: true,
  smartrecruiters: true,
});
const LEVER_REGIONS = closedSet<LeverRegion>({ global: true, eu: true });

function nameValid(name: string): boolean {
  const length = Array.from(name.trim()).length;
  return length >= NAME_CHARS[0] && length <= NAME_CHARS[1];
}

/** Every watchlist company, by display name. */
export function useWatchlist(): UseQueryResult<WatchlistCompanyOut[], ProblemError> {
  return useQuery<WatchlistCompanyOut[], ProblemError>({
    queryKey: WATCHLIST_KEY,
    queryFn: ({ signal }) => apiSend<WatchlistCompanyOut[]>("/api/watchlist", { signal }),
  });
}

type AddOutcome = { kind: "added"; name: string } | { kind: "exists" };

/** `POST /api/watchlist`, then, in the mutation's own `onSuccess` (it runs also once the form is gone): the
 * rebase (the route saved the settings with the new entry) and the list read again. A 409 with the company's
 * id is an outcome, not a failure. `onAdded` clears the form while it is there. */
function useAddCompany() {
  const form = useSettingsForm();
  const latest = useLatestForm();
  const queryClient = useQueryClient();
  const mutation = useMutation<AddOutcome, ProblemError, WatchlistIn>({
    mutationFn: async (body) => {
      try {
        await apiSend<WatchlistCreatedOut>("/api/watchlist", { method: "POST", body: JSON.stringify(body) });
        return { kind: "added", name: body.display_name };
      } catch (error) {
        const problem = toProblem(error);
        if (conflictId(problem, "watchlist_company_id") === null) {
          throw problem;
        }
        return { kind: "exists" };
      }
    },
    retry: false,
    onSuccess: async (outcome) => {
      if (outcome.kind === "added") {
        await (latest.current ?? form).rebase();
        await queryClient.invalidateQueries({ queryKey: WATCHLIST_KEY, exact: true });
      }
    },
  });
  return {
    add: (body: WatchlistIn, onAdded: () => void) => {
      mutation.mutate(body, {
        onSuccess: (outcome) => {
          if (outcome.kind === "added") {
            onAdded();
          }
        },
      });
    },
    pending: mutation.isPending,
    outcome: mutation.data ?? null,
    error: mutation.error,
  };
}

/** What an add came to: a notice, or the refusal with each of its errors. */
function AddResult({ add }: { add: ReturnType<typeof useAddCompany> }) {
  const { t } = useTranslation();
  const serverText = useServerText();
  const { outcome, error } = add;
  return (
    <>
      <p role="status" className={styles.notice}>
        {outcome?.kind === "added" && t("settings_watchlist.added", { name: outcome.name })}
        {outcome?.kind === "exists" && t("settings_watchlist.exists")}
      </p>
      {error !== null && (
        <div className={styles.refusal}>
          <ProblemBox error={error} />
          {error.errors.length > 0 && (
            <ul className={styles.errors}>
              {error.errors.map((issue) => (
                <li key={`${issue.field}-${issue.code}`}>{serverText("issue", issue.code, issue.message)}</li>
              ))}
            </ul>
          )}
        </div>
      )}
    </>
  );
}

function NameField({ id, value, onChange }: { id: string; value: string; onChange: (name: string) => void }) {
  const { t } = useTranslation();
  return (
    <div className={cx(fields.field, styles.name)}>
      <label htmlFor={id} className={fields.label}>{t("settings_watchlist.name")}</label>
      <input id={id} type="text" className={fields.input} value={value} aria-describedby={`${id}-hint`}
        onChange={(event) => { onChange(event.target.value); }} />
      <p id={`${id}-hint`} className={fields.hint}>{t("settings_watchlist.name_hint")}</p>
    </div>
  );
}

function isParsed(answer: ParsedUrlOut | UnsupportedUrlOut | undefined): answer is ParsedUrlOut {
  return answer !== undefined && "ats" in answer;
}

/** The board a URL names, its display name and [Add to the watchlist]. */
function Detected({
  board,
  url,
  add,
  onAdded,
}: {
  board: ParsedUrlOut;
  url: string;
  add: ReturnType<typeof useAddCompany>;
  onAdded: () => void;
}) {
  const { t } = useTranslation();
  const serverText = useServerText();
  const [name, setName] = useState(board.slug);
  const id = useId();
  const submit = (event: SyntheticEvent) => {
    event.preventDefault();
    if (!nameValid(name) || add.pending) {
      return;
    }
    add.add(
      {
        display_name: name.trim(),
        ats_type: board.ats,
        board_slug: board.slug,
        ...(board.region === null ? {} : { lever_region: board.region }),
        input_url: url,
      },
      onAdded,
    );
  };
  return (
    <div role="group" aria-labelledby={`${id}-heading`} className={styles.detected}>
      <p id={`${id}-heading`} className={fields.subheading}>{t("settings_watchlist.detected")}</p>
      <dl className={styles.board}>
        <div>
          <dt>{t("settings_watchlist.ats")}</dt>
          <dd>{serverText("source", board.ats)}</dd>
        </div>
        <div>
          <dt>{t("settings_watchlist.slug")}</dt>
          <dd>{board.slug}</dd>
        </div>
        {board.region !== null && (
          <div>
            <dt>{t("settings_watchlist.region")}</dt>
            <dd>{serverText("lever_region", board.region)}</dd>
          </div>
        )}
      </dl>
      <form onSubmit={submit} className={styles.detectedForm} noValidate>
        <NameField id={`${id}-name`} value={name} onChange={setName} />
        <button type="submit" className={fields.primary} disabled={!nameValid(name) || add.pending} aria-busy={add.pending || undefined}>
          {t("settings_watchlist.add")}
        </button>
      </form>
    </div>
  );
}

function AddByUrl() {
  const { t } = useTranslation();
  const serverText = useServerText();
  const add = useAddCompany();
  const [typed, setTyped] = useState("");
  const [submitted, setSubmitted] = useState<string | null>(null);
  const debounced = useDebounced(typed, PARSE_DELAY_MS);
  const target = (submitted === typed ? typed : debounced).trim();
  const id = useId();
  const parse = useQuery<ParsedUrlOut | UnsupportedUrlOut, ProblemError>({
    queryKey: parseKey(target),
    queryFn: ({ signal }) =>
      apiSend<ParsedUrlOut | UnsupportedUrlOut>("/api/watchlist/parse-url", { method: "POST", body: JSON.stringify({ url: target }), signal }),
    enabled: target !== "",
    staleTime: Infinity,
  });
  // the answer for the URL as it is typed now: none while a newer one waits for its read
  const current = target !== "" && target === typed.trim() ? parse.data : undefined;
  let result: ReactNode = null;
  if (isParsed(current)) {
    result = (
      <Detected key={`${current.ats}/${current.slug}/${String(current.region)}`} board={current} url={target} add={add}
        onAdded={() => {
          setTyped("");
          setSubmitted(null);
        }} />
    );
  } else if (current !== undefined) {
    result = <p className={styles.unsupported}>{serverText("watchlist_unsupported", current.unsupported)}</p>;
  } else if (target !== "" && target === typed.trim() && parse.isError) {
    result = <ProblemBox error={parse.error} onRetry={() => { void parse.refetch(); }} />;
  }
  return (
    <div role="group" aria-labelledby={`${id}-heading`} className={styles.path}>
      <h4 id={`${id}-heading`} className={fields.subheading}>{t("settings_watchlist.by_url")}</h4>
      <form className={styles.addRow} noValidate onSubmit={(event) => {
        event.preventDefault();
        setSubmitted(typed);
      }}>
        <div className={cx(fields.field, styles.url)}>
          <label htmlFor={`${id}-url`} className={fields.label}>{t("settings_watchlist.url")}</label>
          <input id={`${id}-url`} type="url" inputMode="url" className={fields.input} value={typed} maxLength={URL_MAX_CHARS}
            aria-describedby={`${id}-url-hint`}
            onChange={(event) => { setTyped(event.target.value); }} />
        </div>
        <button type="submit" className={fields.button} disabled={typed.trim() === ""}>{t("settings_watchlist.detect")}</button>
      </form>
      <p id={`${id}-url-hint`} className={fields.hint}>{t("settings_watchlist.url_hint")}</p>
      <div aria-live="polite" className={styles.result}>{result}</div>
      <AddResult add={add} />
    </div>
  );
}

function AddByHand() {
  const { t } = useTranslation();
  const serverText = useServerText();
  const add = useAddCompany();
  const [ats, setAts] = useState<AtsType | null>(null);
  const [slug, setSlug] = useState("");
  const [region, setRegion] = useState<LeverRegion | null>(null);
  // the display name follows the slug until the user writes one
  const [name, setName] = useState<string | null>(null);
  const id = useId();
  const shownName = name ?? slug.trim();
  const lever = ats === "lever";
  const regionMissing = lever && region === null;
  const ready = ats !== null && slug.trim() !== "" && nameValid(shownName) && !regionMissing;
  const reset = () => {
    setAts(null);
    setSlug("");
    setRegion(null);
    setName(null);
  };
  const submit = (event: SyntheticEvent) => {
    event.preventDefault();
    if (!ready || add.pending) {
      return;
    }
    add.add(
      {
        display_name: shownName.trim(),
        ats_type: ats,
        board_slug: slug.trim(),
        ...(lever && region !== null ? { lever_region: region } : {}),
      },
      reset,
    );
  };
  return (
    <div role="group" aria-labelledby={`${id}-heading`} className={styles.path}>
      <h4 id={`${id}-heading`} className={fields.subheading}>{t("settings_watchlist.by_hand")}</h4>
      <form onSubmit={submit} className={styles.hand} noValidate>
        <div className={fields.field}>
          <label htmlFor={`${id}-ats`} className={fields.label}>{t("settings_watchlist.ats")}</label>
          <select id={`${id}-ats`} className={fields.select} value={ats ?? ""}
            onChange={(event) => { setAts(ATS_TYPES.find((type) => type === event.target.value) ?? null); }}>
            <option value="">{t("settings_watchlist.choose_ats")}</option>
            {ATS_TYPES.map((type) => <option key={type} value={type}>{serverText("source", type)}</option>)}
          </select>
        </div>
        <div className={fields.field}>
          <label htmlFor={`${id}-slug`} className={fields.label}>{t("settings_watchlist.slug")}</label>
          <input id={`${id}-slug`} type="text" className={fields.input} value={slug} autoCapitalize="off" spellCheck={false}
            aria-describedby={`${id}-slug-hint`} onChange={(event) => { setSlug(event.target.value); }} />
          <p id={`${id}-slug-hint`} className={fields.hint}>{t("settings_watchlist.slug_hint")}</p>
        </div>
        {lever && (
          <div className={fields.field}>
            <label htmlFor={`${id}-region`} className={fields.label}>{t("settings_watchlist.region")}</label>
            <select id={`${id}-region`} className={fields.select} value={region ?? ""} aria-describedby={regionMissing ? `${id}-region-required` : undefined}
              onChange={(event) => { setRegion(LEVER_REGIONS.find((code) => code === event.target.value) ?? null); }}>
              <option value="">{t("settings_watchlist.choose_region")}</option>
              {LEVER_REGIONS.map((code) => <option key={code} value={code}>{serverText("lever_region", code)}</option>)}
            </select>
            {regionMissing && <p id={`${id}-region-required`} className={fields.hint}>{t("settings_watchlist.region_required")}</p>}
          </div>
        )}
        <NameField id={`${id}-name`} value={shownName} onChange={setName} />
        <div className={styles.submit}>
          <button type="submit" className={fields.primary} disabled={!ready || add.pending} aria-busy={add.pending || undefined}
            aria-describedby={regionMissing ? `${id}-region-required` : undefined}>
            {t("settings_watchlist.add")}
          </button>
        </div>
      </form>
      <AddResult add={add} />
    </div>
  );
}

function CompanyRow({ company, onRemove }: { company: WatchlistCompanyOut; onRemove: () => void }) {
  const { t } = useTranslation();
  const serverText = useServerText();
  const format = useFormat();
  const id = useId();
  const ats = serverText("source", company.ats_type);
  const board = company.lever_region === null
    ? t("settings_watchlist.board", { ats, slug: company.board_slug })
    : t("settings_watchlist.board_region", { ats, region: serverText("lever_region", company.lever_region), slug: company.board_slug });
  const snapshot = company.instance?.last_complete_snapshot_at ?? null;
  const { postings } = company;
  return (
    <tr role="row">
      <td role="cell" data-label={t("settings_watchlist.col.enabled")}>
        <EntryToggle list="watchlist" id={company.id} label={t("settings_watchlist.enable", { name: company.display_name })} />
      </td>
      <td role="cell" data-label={t("settings_watchlist.col.company")} className={cx(tables.text, tables.wide)}>
        <span id={`${id}-name`}>{company.display_name}</span>
        <span className={tables.meta}>{board}</span>
      </td>
      <td role="cell" data-label={t("settings_watchlist.col.validation")}>{serverText("watchlist_validation", company.validation_status)}</td>
      <td role="cell" data-label={t("settings_watchlist.col.snapshot")} className={tables.nowrap}>
        {snapshot === null ? t("settings_watchlist.never") : format.dayMonthTime(snapshot)}
      </td>
      <td role="cell" data-label={t("settings_watchlist.col.health")}>
        {company.instance === null ? t("value.none") : serverText("instance_health", company.instance.health)}
      </td>
      <td role="cell" data-label={t("settings_watchlist.col.postings")} className={tables.wide}>
        {t("settings_watchlist.postings", {
          active: format.number(postings.active),
          stale: format.number(postings.stale),
          unknown: format.number(postings.unknown),
          closed: format.number(postings.closed),
        })}
      </td>
      <td role="cell" className={tables.wide}>
        <div className={tables.actions}>
          <button type="button" className={tables.action} aria-label={t("settings_watchlist.remove_label", { name: company.display_name })}
            onClick={onRemove}>
            {t("settings_watchlist.remove")}
          </button>
        </div>
      </td>
    </tr>
  );
}

/** `DELETE /api/watchlist/{id}` (204), then, in the mutation's own `onSuccess` (it runs also once the list is
 * gone): the rebase (the route saved the settings without the entry), an entry the draft had added taken back
 * (no settings version held it, so the route saved none), and the list read again. The take-back holds against
 * either base, so it also follows a failed rebase: `removeEntry` acts only on an entry the base lacks, and a
 * deleted company's id must not stay in the draft (the dry-run refuses it). */
function useRemoveCompany() {
  const form = useSettingsForm();
  const latest = useLatestForm();
  const queryClient = useQueryClient();
  return useMutation<undefined, ProblemError, WatchlistCompanyOut>({
    mutationFn: (company) => apiSend<undefined>(`/api/watchlist/${String(company.id)}`, { method: "DELETE" }),
    retry: false,
    onSuccess: async (_out, company) => {
      const target = latest.current ?? form;
      await target.rebase();
      target.removeEntry("watchlist", company.id);
      await queryClient.invalidateQueries({ queryKey: WATCHLIST_KEY, exact: true });
    },
  });
}

function CompanyList() {
  const { t } = useTranslation();
  const companies = useWatchlist();
  const [removing, setRemoving] = useState<WatchlistCompanyOut | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const heading = useRef<HTMLHeadingElement>(null);
  const headingId = useId();
  const removal = useRemoveCompany();
  const confirm = () => {
    if (removing === null || removal.isPending) {
      return;
    }
    removal.mutate(removing, {
      onSuccess: (_out, company) => {
        setRemoving(null);
        setNotice(t("settings_watchlist.removed", { name: company.display_name }));
        heading.current?.focus(); // its row, and the Remove the dialog gave the focus back to, are gone
      },
    });
  };
  let body: ReactNode;
  if (companies.isPending) {
    body = <Skeleton variant="line" count={3} />;
  } else if (companies.isError) {
    body = <ProblemBox error={companies.error} onRetry={() => { void companies.refetch(); }} />;
  } else if (companies.data.length === 0) {
    body = <p className={tables.empty}>{t("settings_watchlist.empty")}</p>;
  } else {
    body = (
      <div className={tables.scroll}>
        <table role="table" className={tables.table} aria-labelledby={headingId}>
          <thead role="rowgroup">
            <tr role="row">
              <th role="columnheader" scope="col">{t("settings_watchlist.col.enabled")}</th>
              <th role="columnheader" scope="col">{t("settings_watchlist.col.company")}</th>
              <th role="columnheader" scope="col">{t("settings_watchlist.col.validation")}</th>
              <th role="columnheader" scope="col">{t("settings_watchlist.col.snapshot")}</th>
              <th role="columnheader" scope="col">{t("settings_watchlist.col.health")}</th>
              <th role="columnheader" scope="col">{t("settings_watchlist.col.postings")}</th>
              <th role="columnheader" scope="col"><span className="visually-hidden">{t("settings_watchlist.col.actions")}</span></th>
            </tr>
          </thead>
          <tbody role="rowgroup">
            {companies.data.map((company) => (
              <CompanyRow key={company.id} company={company} onRemove={() => {
                removal.reset();
                setRemoving(company);
              }} />
            ))}
          </tbody>
        </table>
      </div>
    );
  }
  return (
    <div className={styles.group}>
      <h3 ref={heading} id={headingId} tabIndex={-1} className={cx(fields.heading, styles.listHeading)}>
        {t("settings_watchlist.list_heading")}
      </h3>
      <p role="status" className={styles.notice}>{notice}</p>
      {body}
      <ConfirmDialog open={removing !== null} title={t("settings_watchlist.remove_title", { name: removing?.display_name ?? "" })}
        confirmLabel={t("settings_watchlist.remove")} busy={removal.isPending} onConfirm={confirm}
        onCancel={() => { setRemoving(null); }}>
        <p>{t("settings_watchlist.remove_body")}</p>
        {removal.error !== null && <ProblemBox error={removal.error} />}
      </ConfirmDialog>
    </div>
  );
}

export function WatchlistSection() {
  const { t } = useTranslation();
  const headingId = useId();
  return (
    <div className={styles.watchlist}>
      <div className={styles.group} role="group" aria-labelledby={headingId}>
        <h3 id={headingId} className={fields.heading}>{t("settings_watchlist.add_heading")}</h3>
        <div className={styles.paths}>
          <AddByUrl />
          <AddByHand />
        </div>
      </div>
      <CompanyList />
    </div>
  );
}
