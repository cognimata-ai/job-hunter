import { describe, expect, it } from "vitest";
import { ProblemError } from "./client";
import { throttleWait } from "./throttle";

function throttled(extra: Record<string, unknown>): ProblemError {
  return new ProblemError({ status: 429, code: "throttled", title: "Too many", requestId: null, errors: [], extra });
}

describe("throttleWait", () => {
  it("reads a busy gate, a lockout in whole minutes, and an answer without a wait", () => {
    // the gate's busy answer (1 s), and a lockout with a second left, are "in a moment"
    expect(throttleWait(throttled({ retry_after_s: 1 }))).toEqual({ kind: "busy" });
    // a lockout: its minutes, rounded up
    expect(throttleWait(throttled({ retry_after_s: 2 }))).toEqual({ kind: "minutes", minutes: 1 });
    expect(throttleWait(throttled({ retry_after_s: 541 }))).toEqual({ kind: "minutes", minutes: 10 });
    // no number: no wait to state
    expect(throttleWait(throttled({}))).toEqual({ kind: "unknown" });
    expect(throttleWait(throttled({ retry_after_s: "soon" }))).toEqual({ kind: "unknown" });
  });
});
