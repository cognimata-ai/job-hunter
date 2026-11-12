// Before every E2E run (plan M4 Task 32): the stack ready (`/readyz` 200 within 300 s), the user `smoke` with
// `smoke_m0.PASSWORD` (`smoke_m0.USER_CREATE` in a one-off api container; a "user already exists" answer is
// accepted when the login then works), and its session saved to `AUTH_STATE` through an API login. The E2E
// and the visual checks share that user. The password is read in process and never printed; every request
// context is `newApiContext`'s, whose failures are redacted (Ruling F4-c). The readiness probe waits out a
// status other than 200 (502 or 503 while the stack starts) and an error of a stack not up yet (`isNotUpYet`),
// and rethrows any other error at once (plan M5 Task 30, M4 Ruling F4-d R2-M1).
import { newApiContext, ORIGIN, saveAuthState } from "./support/api";
import { isNotUpYet, READY_REQUEST_TIMEOUT_MS } from "./support/ready";
import { DC, run, smokeUser, waitFor } from "./support/stack";

const READY_WITHIN_MS = 300_000;

async function ready(): Promise<true | null> {
  const context = await newApiContext({ baseURL: ORIGIN });
  try {
    return (await context.get("/readyz", { timeout: READY_REQUEST_TIMEOUT_MS })).status() === 200 ? true : null;
  } catch (error) {
    if (isNotUpYet(error)) {
      return null; // devproxy or the api not up yet
    }
    throw error;
  } finally {
    await context.dispose();
  }
}

export default async function globalSetup(): Promise<void> {
  await waitFor(ready, READY_WITHIN_MS, "GET /readyz 200");
  const user = await smokeUser();
  const created = await run([...DC, "run", "--rm", "-T", "api", ...user.create], { input: `${user.password}\n` });
  // A user that exists already is the smoke user when the login below works (`smoke_m0` reads the whole stderr
  // for the message, as here).
  if (created.code !== 0 && !created.stderr.includes(user.exists)) {
    const tail = created.stderr.trim().split("\n").slice(-3).join(" | ").slice(0, 400);
    throw new Error(`jobhunter user create exited ${String(created.code)}: ${tail}`);
  }
  await saveAuthState(user);
}
