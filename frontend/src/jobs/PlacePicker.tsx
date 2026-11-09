// The place picker (spec §8.12, §15.3; plan M4 Task 23, Ruling f, M2 Carry-forward 2; shared with Task 28's
// scope): a combobox over the gazetteer (`GET /api/geo/places?q=…&limit=10`), read 250 ms after the last
// keystroke; an empty field lists the most populous places. Each option reads "<label> · <postal codes> ·
// <population>": at most three postal codes, then "+n" for the rest (none: the part is left out), and the
// population in the UI language's numbers.
//
// While the places are read the list says "Searching…", and a failed read says so. The field shows the
// picked place's name; text typed and left without a pick goes back to it. The clear button picks no place.
// `onChange` gets the place's id, name and coordinates, or `null`, and with a pick the whole place as the
// gazetteer answered it (its postal codes and population, which the scope's location rows show, Task 28).
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { apiFetch } from "../api/client";
import type { components } from "../api/schema";
import { Combobox } from "../components/Combobox";
import { useDebounced } from "../components/useDebounced";
import { useFormat } from "../system/format";
import styles from "./PlacePicker.module.css";

export type PlaceOut = components["schemas"]["PlaceOut"];

/** A picked place: what the Jobs list's location filter and the scope's location keep of it. */
export interface PickedPlace {
  gazetteer_id: string;
  label: string;
  lat: number;
  lon: number;
}

export const PLACES_KEY = ["geo", "places"] as const;
const SEARCH_DELAY_MS = 250;
const LIMIT = 10;
/** `GET /api/geo/places` refuses a longer `q`. */
const MAX_Q = 100;
const SHOWN_POSTAL_CODES = 3;
/** The gazetteer ships with the release: a search's answer does not change. */
const PLACES_STALE_MS = 60 * 60_000;

export interface PlacePickerProps {
  /** The field's label. */
  label: string;
  value: PickedPlace | null;
  onChange: (place: PickedPlace | null, found?: PlaceOut) => void;
}

export function PlacePicker({ label, value, onChange }: PlacePickerProps) {
  const { t } = useTranslation();
  const format = useFormat();
  const input = useRef<HTMLInputElement>(null);
  const [text, setText] = useState(value?.label ?? "");
  const [shown, setShown] = useState(value);
  if (value?.gazetteer_id !== shown?.gazetteer_id || value?.label !== shown?.label) {
    setShown(value); // a place set from elsewhere (the URL, Reset): its name in the field
    setText(value?.label ?? "");
  }
  const [open, setOpen] = useState(false);
  const typed = text.trim().slice(0, MAX_Q);
  const q = useDebounced(typed, SEARCH_DELAY_MS);
  const places = useQuery({
    queryKey: [...PLACES_KEY, q],
    queryFn: ({ signal }) =>
      apiFetch<PlaceOut[]>(`/api/geo/places?${new URLSearchParams({ q, limit: String(LIMIT) }).toString()}`, { signal }),
    enabled: open && q === typed, // not while the text is still being typed
    placeholderData: keepPreviousData,
    staleTime: PLACES_STALE_MS,
  });
  const optionText = (place: PlaceOut) => {
    const codes = place.postal_codes.slice(0, SHOWN_POSTAL_CODES).join(", ");
    const more = place.postal_codes.length - SHOWN_POSTAL_CODES;
    const postal = more > 0 ? `${codes} ${t("filters.place_more", { count: more })}` : codes;
    return [place.label, postal, format.number(place.population)].filter((part) => part !== "").join(" · ");
  };
  return (
    <Combobox<PlaceOut>
      label={label}
      value={text}
      onValueChange={setText}
      onOpenChange={setOpen}
      options={open ? (places.data ?? []) : []}
      optionKey={(place) => place.gazetteer_id}
      optionText={optionText}
      onPick={(place) => {
        setText(place.label);
        onChange({ gazetteer_id: place.gazetteer_id, label: place.label, lat: place.lat, lon: place.lon }, place);
      }}
      emptyText={t("filters.place_none")}
      loading={places.isFetching || q !== typed}
      failed={places.isError && q === typed ? t("filters.place_failed") : undefined}
      onBlur={() => { setText(value?.label ?? ""); }}
      inputRef={input}
      after={
        value !== null && (
          <button type="button" className={styles.clear} aria-label={t("filters.location_clear")}
            onClick={() => {
              setText("");
              onChange(null);
              input.current?.focus(); // the button leaves with the place
            }}>
            <span aria-hidden="true">×</span>
          </button>
        )
      }
    />
  );
}
