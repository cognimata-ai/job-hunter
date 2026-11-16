// The global setup's readiness probe (plan M5 Task 30, M4 Ruling F4-d R2-M1): `ready()` waits on an error that
// means the stack is not up yet, and rethrows any other at once, so a renamed private Playwright API fails the
// run in seconds, not after the 300 s readiness timeout. The messages are Playwright 1.56.1's own, as its
// request context words them.
import { describe, expect, it } from "vitest";
import { isNotUpYet, READY_REQUEST_TIMEOUT_MS } from "../../e2e/support/ready";

describe("isNotUpYet", () => {
  it("treats a refused connection, a reset and a request timeout as not up yet", () => {
    expect(READY_REQUEST_TIMEOUT_MS).toBe(5_000);
    for (const message of [
      "apiRequestContext.get: connect ECONNREFUSED 127.0.0.1:8080",
      "apiRequestContext.get: connect ECONNRESET 127.0.0.1:8080",
      "apiRequestContext.get: read ECONNRESET",
      "apiRequestContext.get: socket hang up",
      "apiRequestContext.get: Timeout 5000ms exceeded.",
    ]) {
      expect(isNotUpYet(new Error(message)), message).toBe(true);
    }
    const timedOut = new Error("apiRequestContext.get: Timeout 5000ms exceeded.\nCall log:\n  - → GET http://localhost:8080/readyz");
    timedOut.name = "TimeoutError";
    expect(isNotUpYet(timedOut)).toBe(true);
  });

  it("rethrows any other error", () => {
    // `false` is what makes `ready()` rethrow
    for (const error of [
      new TypeError("internal._wrapApiCall is not a function"), // a renamed private API (`support/api.ts`)
      new Error("apiRequestContext.get: getaddrinfo ENOTFOUND localhost"),
      new Error("apiRequestContext.get: Timeout 30000ms exceeded."), // not the probe's own 5 s
      new Error("browserType.launch: Executable doesn't exist"),
    ]) {
      expect(isNotUpYet(error), error.message).toBe(false);
    }
    // only an Error: a thrown string or object that names a code is no transport failure of the request
    for (const [what, thrown] of [
      ["a string", "connect ECONNREFUSED 127.0.0.1:8080"],
      ["an object with a message", { message: "socket hang up" }],
      ["null", null],
      ["undefined", undefined],
    ] as const) {
      expect(isNotUpYet(thrown), what).toBe(false);
    }
  });
});
