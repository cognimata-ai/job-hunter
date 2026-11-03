// Query strings for the api's list filters (spec §14.3).

export type SearchValue = string | number | boolean | readonly string[] | null | undefined;

/** `params` as a query string: an array as one repeated key per item, `null` and `undefined` left out (an
 * empty string and a zero are values, and stay). */
export function toSearchParams(params: Record<string, SearchValue>): URLSearchParams {
  const search = new URLSearchParams();
  for (const [name, value] of Object.entries(params)) {
    if (value === null || value === undefined) {
      continue;
    }
    if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
      search.append(name, String(value));
      continue;
    }
    for (const item of value) {
      search.append(name, item);
    }
  }
  return search;
}
