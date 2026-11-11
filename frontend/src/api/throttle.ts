// A `throttled` answer (429, spec §16.4) and how long it asks to wait, which the Login page and the privacy
// dialogs read alike (their re-authentication shares the login's gate and lockouts, plan M5 Task 18).
//
// The login gate's busy answer carries `retry_after_s: 1`. A lockout's is `max(1, ceil(seconds left))`, so a
// lockout with one second left reads as busy too; either way the right advice is "in a moment".
import type { ProblemError } from "./client";

const BUSY_RETRY_AFTER_S = 1;

/** The wait a `throttled` answer asks for: a moment (the server is busy), whole minutes (a lockout), or none it
 * states (no `retry_after_s`). */
export type ThrottleWait = { kind: "busy" } | { kind: "minutes"; minutes: number } | { kind: "unknown" };

export function throttleWait(error: ProblemError): ThrottleWait {
  const seconds = error.extra.retry_after_s;
  if (typeof seconds !== "number") {
    return { kind: "unknown" };
  }
  if (seconds <= BUSY_RETRY_AFTER_S) {
    return { kind: "busy" };
  }
  return { kind: "minutes", minutes: Math.ceil(seconds / 60) };
}
