// No credential in an error's text (spec §16 L5486, Rulings F4-a, F4-c). When a request of an API context fails
// in transport (refused, reset, timed out), Playwright appends its call log to the error, and the call log lists
// every request header, the session cookie the context supplies included; the list and HTML reporters keep the
// message, and e2e.yml uploads the report. `redacted` rewrites such an error: the value of every `Cookie` and
// `Set-Cookie` header, every `password` field, and each value `rememberSecret` was given (the smoke password,
// the session cookies of a context's storage state) become `[redacted]`.
const secrets = new Set<string>();
const REDACTED = "[redacted]";
// a header line of the call log (`  - cookie: a=b; c=d`), or the header in a JSON or HAR form, to the line's end
const COOKIE_HEADER = /\b((?:set-)?cookie["']?\s*:\s*)[^\r\n]*/gi;
// a `password` member or parameter: JSON, a query string or a form
const PASSWORD_FIELD = /\b(password["']?\s*[:=]\s*)("(?:[^"\\\r\n]|\\.)*"|'(?:[^'\\\r\n]|\\.)*'|[^\s,&;}]+)/gi;

/** Remembers `value` (a password, a session cookie's value) as one `redact` removes wherever it appears. */
export function rememberSecret(value: string): void {
  if (value !== "") {
    secrets.add(value);
  }
}

/** `text` with every credential `redacted` knows of replaced by `[redacted]`. */
export function redact(text: string): string {
  let clean = text.replace(COOKIE_HEADER, `$1${REDACTED}`).replace(PASSWORD_FIELD, `$1${REDACTED}`);
  for (const secret of secrets) {
    clean = clean.split(secret).join(REDACTED);
  }
  return clean;
}

/** A new `Error` of `error`'s name with its message and stack redacted (`redact`). */
export function redacted(error: unknown): Error {
  if (!(error instanceof Error)) {
    return new Error(redact(String(error)));
  }
  const clean = new Error(redact(error.message));
  clean.name = error.name;
  clean.stack = error.stack === undefined ? undefined : redact(error.stack);
  return clean;
}

/** `work`'s result, or its failure rethrown `redacted`. */
export async function scrubbed<T>(work: () => Promise<T>): Promise<T> {
  try {
    return await work();
  } catch (error) {
    throw redacted(error);
  }
}
