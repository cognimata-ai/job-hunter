// The Company filter (spec §15.3 "Multi-select on company key"; plan M4 Task 23, Ruling e): a combobox over the
// listed cards' companies (`GET /api/jobs/facets?<the current filters>&company_q=…`, read 300 ms after the last
// keystroke), each option "<display name> (<count>)", and the picked companies as removable chips.
//
// The URL holds company keys only, so a chip names its company by the option it was picked from, else by the
// panel's own facets, else by its key (a company picked before a reload, beyond the facets' first ones).
import { useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Combobox } from "../components/Combobox";
import { useDebounced } from "../components/useDebounced";
import { type FacetsOut, useFacets } from "./api";
import styles from "./CompanyPicker.module.css";

type CompanyFacetOut = FacetsOut["companies"][number];

const SEARCH_DELAY_MS = 300;
const LIMIT = 10;
/** `GET /api/jobs/facets` refuses a longer `company_q`. */
const MAX_Q = 200;

export interface CompanyPickerProps {
  /** The list's API params (`toApiParams`), which the facets are counted under. */
  params: string;
  /** The picked company keys. */
  value: readonly string[];
  onChange: (keys: string[]) => void;
  /** Companies the panel's facets have named. */
  known: readonly CompanyFacetOut[];
}

export function CompanyPicker({ params, value, onChange, known }: CompanyPickerProps) {
  const { t } = useTranslation();
  const input = useRef<HTMLInputElement>(null);
  const [pickedNames, setPickedNames] = useState<ReadonlyMap<string, string>>(new Map());
  const [text, setText] = useState("");
  const [open, setOpen] = useState(false);
  const typed = text.trim().slice(0, MAX_Q);
  const companyQ = useDebounced(typed, SEARCH_DELAY_MS);
  const found = useFacets(params, { companyQ, limit: LIMIT, enabled: open && companyQ === typed });
  const picked = new Set(value);
  const options = open ? (found.data?.companies ?? []).filter((company) => !picked.has(company.company_key)) : [];
  const nameOf = (key: string) =>
    pickedNames.get(key) ?? known.find((company) => company.company_key === key)?.display_name ?? key;
  return (
    <div className={styles.picker}>
      <Combobox<CompanyFacetOut>
        label={t("filters.company_search")}
        value={text}
        onValueChange={setText}
        onOpenChange={setOpen}
        options={options}
        optionKey={(company) => company.company_key}
        optionText={(company) => t("filters.company_option", { name: company.display_name, count: company.count })}
        onPick={(company) => {
          setText("");
          setPickedNames((names) => new Map(names).set(company.company_key, company.display_name));
          onChange([...value, company.company_key]);
        }}
        emptyText={t("filters.company_none")}
        loading={found.isFetching || companyQ !== typed}
        inputRef={input}
      />
      {value.length > 0 && (
        <ul role="list" className={styles.chips} aria-label={t("filters.company_picked")}>
          {value.map((key) => (
            <li key={key} className={styles.chip}>
              <span className={styles.name}>{nameOf(key)}</span>
              <button type="button" className={styles.remove} aria-label={t("filters.company_remove", { name: nameOf(key) })}
                onClick={() => {
                  onChange(value.filter((other) => other !== key));
                  input.current?.focus(); // the chip leaves with its button
                }}>
                <span aria-hidden="true">×</span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
