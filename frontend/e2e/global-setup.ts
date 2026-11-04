// Before every E2E run (plan M4 Task 32): the stack ready (`/readyz` 200 within 300 s), the user `smoke` with
// `smoke_m0.PASSWORD` (`smoke_m0.USER_CREATE` in a one-off api container; a "user already exists" answer is
// accepted when the login then works), and its session saved to `AUTH_STATE` through an API login. The E2E
// and the visual checks share that user. The password is read in process and never printed.
import { request } from "@playwright/test";
import { ORIGIN, saveAuthState } from "./support/api";
import { compose, smokeUser, waitFor } from "./support/stack";

const READY_WITHIN_MS = 300_000;

async function ready(): Promise<true | null> {
  const context = await request.newContext({ baseURL: ORIGIN });
  try {
    return (await context.get("/readyz", { timeout: 5_000 })).status() === 200 ? true : null;
  } catch {
    return null; // devproxy or the api not up yet
  } finally {
    await context.dispose();
  }
}

export default async function globalSetup(): Promise<void> {
  await waitFor(ready, READY_WITHIN_MS, "GET /readyz 200");
  const user = await smokeUser();
  try {
    await compose(["run", "--rm", "-T", "api", ...user.create], { input: `${user.password}\n` });
  } catch (error) {
    if (!(error instanceof Error) || !error.message.includes(user.exists)) {
      throw error;
    }
    // the user exists already: the login below proves it is the smoke user
  }
  await saveAuthState(user);
}
