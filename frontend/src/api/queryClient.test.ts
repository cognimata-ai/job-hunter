import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ProblemError } from "./client";
import { CancelledError } from "@tanstack/react-query";
import { isTransient, makeQueryClient } from "./queryClient";

function problem(status: number, code: string): ProblemError {
  return new ProblemError({ status, code, title: code, requestId: "r", errors: [] });
}

function client() {
  const onUnauthenticated = vi.fn();
  const onTransient = vi.fn();
  return { qc: makeQueryClient({ onUnauthenticated, onTransient }), onUnauthenticated, onTransient };
}

describe("makeQueryClient", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("a 401 calls onUnauthenticated once and is not retried", async () => {
    const { qc, onUnauthenticated, onTransient } = client();
    const expired = vi.fn(() => Promise.reject(problem(401, "unauthenticated")));
    const burst = Promise.allSettled([
      qc.query({ queryKey: ["jobs"], queryFn: expired }),
      qc.query({ queryKey: ["status"], queryFn: expired }),
      qc.getMutationCache().build(qc, { mutationFn: expired }).execute(undefined),
    ]);
    await vi.runAllTimersAsync();
    const settled = await burst;
    expect(settled.map((s) => s.status)).toEqual(["rejected", "rejected", "rejected"]);
    expect(expired).toHaveBeenCalledTimes(3); // each once: a 4xx is never retried
    expect(onUnauthenticated).toHaveBeenCalledTimes(1);
    expect(onTransient).not.toHaveBeenCalled();
  });

  it("the login and the session read never call onUnauthenticated, and a new session re-arms it", async () => {
    const { qc, onUnauthenticated } = client();
    const expired = () => Promise.reject(problem(401, "unauthenticated"));
    await qc.query({ queryKey: ["session"], queryFn: expired, meta: { authCall: true } }).catch(() => undefined);
    await qc.getMutationCache().build(qc, { mutationFn: expired, meta: { authCall: true } }).execute(undefined)
      .catch(() => undefined);
    expect(onUnauthenticated).not.toHaveBeenCalled();
    await qc.query({ queryKey: ["a"], queryFn: expired }).catch(() => undefined);
    await qc.query({ queryKey: ["b"], queryFn: expired }).catch(() => undefined);
    expect(onUnauthenticated).toHaveBeenCalledTimes(1);
    // a login answered: the next expiry is a new one
    await qc.getMutationCache().build(qc, { mutationFn: () => Promise.resolve(), meta: { authCall: true } })
      .execute(undefined);
    await qc.query({ queryKey: ["c"], queryFn: expired }).catch(() => undefined);
    expect(onUnauthenticated).toHaveBeenCalledTimes(2);
  });

  it("a 401 of another code is an ordinary error", async () => {
    const { qc, onUnauthenticated } = client();
    await qc.query({ queryKey: ["x"], queryFn: () => Promise.reject(problem(401, "totp_required")) })
      .catch(() => undefined);
    expect(onUnauthenticated).not.toHaveBeenCalled();
  });

  it("a 503 retries three times with backoff", async () => {
    const { qc, onTransient, onUnauthenticated } = client();
    const failing = vi.fn(() => Promise.reject(problem(503, "internal")));
    const result = qc.query({ queryKey: ["jobs"], queryFn: failing }).catch((e: unknown) => e);
    await vi.advanceTimersByTimeAsync(0);
    expect(failing).toHaveBeenCalledTimes(1);
    expect(onTransient).toHaveBeenCalledTimes(1); // the toast comes with the first failure, before the retries
    for (const [delay, calls] of [[1000, 2], [2000, 3], [4000, 4]] as const) {
      await vi.advanceTimersByTimeAsync(delay - 1);
      expect(failing).toHaveBeenCalledTimes(calls - 1);
      await vi.advanceTimersByTimeAsync(1);
      expect(failing).toHaveBeenCalledTimes(calls);
    }
    await vi.advanceTimersByTimeAsync(60_000);
    expect(failing).toHaveBeenCalledTimes(4);
    expect(await result).toMatchObject({ status: 503 });
    expect(onTransient).toHaveBeenCalledTimes(1);
    expect(onUnauthenticated).not.toHaveBeenCalled();
  });

  it("a network failure is retried like a 5xx; a 4xx is not", async () => {
    const { qc, onTransient } = client();
    const offline = vi.fn(() => Promise.reject(new TypeError("Failed to fetch")));
    const missing = vi.fn(() => Promise.reject(problem(404, "not_found")));
    const both = Promise.allSettled([
      qc.query({ queryKey: ["a"], queryFn: offline }),
      qc.query({ queryKey: ["b"], queryFn: missing }),
    ]);
    await vi.runAllTimersAsync();
    await both;
    expect(offline).toHaveBeenCalledTimes(4);
    expect(missing).toHaveBeenCalledTimes(1);
    expect(onTransient).toHaveBeenCalledTimes(1);
  });

  it("a query that does not retry raises the toast on its one failure", async () => {
    const { qc, onTransient } = client();
    await qc.query({ queryKey: ["once"], queryFn: () => Promise.reject(problem(502, "internal")), retry: false })
      .catch(() => undefined);
    expect(onTransient).toHaveBeenCalledTimes(1);
  });

  it("a mutation never retries", async () => {
    const { qc, onTransient } = client();
    const failing = vi.fn(() => Promise.reject(problem(503, "internal")));
    const run = qc.getMutationCache().build(qc, { mutationFn: failing }).execute(undefined).catch((e: unknown) => e);
    await vi.runAllTimersAsync();
    expect(await run).toMatchObject({ status: 503 });
    expect(failing).toHaveBeenCalledTimes(1);
    expect(onTransient).toHaveBeenCalledTimes(1);
  });
});

describe("isTransient", () => {
  it("is a network failure or a 5xx, never an answer and never a cancellation", () => {
    expect(isTransient(new TypeError("Failed to fetch"))).toBe(true);
    expect(isTransient(problem(502, "internal"))).toBe(true);
    expect(isTransient(problem(0, "network"))).toBe(true);
    expect(isTransient(problem(409, "conflict_state"))).toBe(false);
    expect(isTransient(problem(401, "unauthenticated"))).toBe(false);
    expect(isTransient(new CancelledError())).toBe(false);
  });
});
