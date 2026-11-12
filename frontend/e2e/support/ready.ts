// The global setup's readiness probe (plan M5 Task 30, M4 Ruling F4-d R2-M1): which failure of `GET /readyz`
// means the stack is not up yet. While it starts, devproxy refuses or resets the connection, or the api accepts it
// and does not answer within the probe's own timeout; those are waited out. Any other error, such as a renamed
// private Playwright API (`support/api.ts`'s `_wrapApiCall`), is rethrown at once instead of showing as a 300 s
// readiness timeout. Pure, and it imports nothing: the vitest test (`src/test/e2eReady.test.ts`) compiles it
// under the root tsconfig.json, which has no Node or Playwright types.

/** The probe's request timeout; Playwright words it `Timeout 5000ms exceeded.` (1.56.1). */
export const READY_REQUEST_TIMEOUT_MS = 5_000;

const NOT_UP_YET: readonly string[] = [
  "ECONNREFUSED", // nothing listens on 8080 yet
  "ECONNRESET", // devproxy is up, the api behind it is not
  "socket hang up",
  `Timeout ${String(READY_REQUEST_TIMEOUT_MS)}ms exceeded`,
];

/** Whether `error`, a failure of the readiness probe's request, means the stack is not up yet: an `Error`
 * whose message names a refused or reset connection, a hang-up, or the probe's own timeout. */
export function isNotUpYet(error: unknown): boolean {
  return error instanceof Error && NOT_UP_YET.some((sign) => error.message.includes(sign));
}
