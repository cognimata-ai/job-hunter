// The enabled checkbox of a `queries[]` or `watchlist[]` entry in a table row (plan M4 Task 29, resolutions b
// and d): bound to the draft's entry by its id (Ruling T28-d). An id the draft does not list reads "Not in the
// settings", unticked; ticking it adds the entry, enabled (`addEntry`), and unticking an entry the draft added
// takes it back (`removeEntry`); an entry of the base is toggled (`setEntry`).
import { useId } from "react";
import { useTranslation } from "react-i18next";
import tables from "./tables.module.css";
import { type EntryList, type SettingsForm, useSettingsForm } from "./useSettingsForm";

/** The draft's or the base's entry `id` of `list`. */
function entryOf(doc: SettingsForm["draft"], list: EntryList, id: number): { enabled: boolean } | undefined {
  return list === "queries"
    ? doc.queries.find((entry) => entry.query_id === id)
    : doc.watchlist.find((entry) => entry.watchlist_company_id === id);
}

export function EntryToggle({ list, id, label }: { list: EntryList; id: number; label: string }) {
  const { t } = useTranslation();
  const form = useSettingsForm();
  const noteId = useId();
  const entry = entryOf(form.draft, list, id);
  const saved = entryOf(form.base.payload, list, id) !== undefined;
  const toggle = (checked: boolean) => {
    if (entry === undefined) {
      if (checked) {
        form.addEntry(list, id);
      }
    } else if (!checked && !saved) {
      form.removeEntry(list, id);
    } else {
      form.setEntry(list, id, checked);
    }
  };
  return (
    <span className={tables.check}>
      <input type="checkbox" checked={entry?.enabled === true} aria-label={label}
        aria-describedby={entry === undefined ? noteId : undefined}
        onChange={(event) => { toggle(event.target.checked); }} />
      {entry === undefined && <span id={noteId} className={tables.note}>{t("settings_entries.not_in_settings")}</span>}
    </span>
  );
}
