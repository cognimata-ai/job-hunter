import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";
import { ProblemError } from "./client";
import { useKeyedMutation } from "./idempotency";

function problem(status: number, code: string): ProblemError {
  return new ProblemError({ status, code, title: code, requestId: "r", errors: [] });
}

function setup<V>(answers: (() => Promise<string>)[]) {
  const keys: string[] = [];
  const seen: V[] = [];
  const fn = vi.fn((vars: V, key: string) => {
    keys.push(key);
    seen.push(vars);
    const next = answers.shift();
    if (next === undefined) {
      throw new Error("an unexpected call");
    }
    return next();
  });
  const client = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  const { result } = renderHook(() => useKeyedMutation(fn), { wrapper });
  // one user action: the call settles either way; what it answered is the hook's state
  const act$ = async (vars: V) => {
    await act(async () => {
      await result.current.mutateAsync(vars).catch(() => undefined);
    });
  };
  return { keys, seen, fn, result, act: act$ };
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

describe("useKeyedMutation", () => {
  it("one key per action, reused after a 503, a new one after a 409", async () => {
    const m = setup<{ id: number }>([
      () => Promise.reject(problem(503, "internal")),
      () => Promise.reject(problem(409, "conflict_state")),
      () => Promise.resolve("done"),
      () => Promise.resolve("again"),
    ]);
    await m.act({ id: 1 }); // 503: the key is kept for a manual retry
    await m.act({ id: 1 }); // the retry: the same key; 409: dropped
    await m.act({ id: 1 }); // a new action: a new key; success: dropped
    await m.act({ id: 1 }); // and a new one again
    expect(m.fn).toHaveBeenCalledTimes(4);
    const [first, retry, afterConflict, afterSuccess] = m.keys;
    expect(first).toMatch(UUID);
    expect(retry).toBe(first);
    expect(afterConflict).not.toBe(first);
    expect(afterSuccess).not.toBe(afterConflict);
    await waitFor(() => {
      expect(m.result.current.data).toBe("again");
    });
  });

  it("keeps the key after a network failure", async () => {
    const m = setup<string>([
      () => Promise.reject(new TypeError("Failed to fetch")),
      () => Promise.resolve("done"),
    ]);
    await m.act("run");
    await m.act("run");
    expect(m.keys[1]).toBe(m.keys[0]);
  });

  it("drops the key after a 4xx other than a conflict", async () => {
    const m = setup<string>([
      () => Promise.reject(problem(422, "validation_failed")),
      () => Promise.resolve("done"),
    ]);
    await m.act("save");
    await m.act("save");
    expect(m.keys[1]).not.toBe(m.keys[0]);
  });

  it("gives another action a new key even while one is kept", async () => {
    const m = setup<{ id: number }>([
      () => Promise.reject(problem(502, "internal")),
      () => Promise.resolve("done"),
    ]);
    await m.act({ id: 1 });
    await m.act({ id: 2 });
    expect(m.keys[1]).not.toBe(m.keys[0]);
  });

  it("keeps each action's key: A, then B, then a retry of A reuses A's key after A's 503", async () => {
    const m = setup<{ id: number }>([
      () => Promise.reject(problem(503, "internal")), // A
      () => Promise.resolve("b"), // B
      () => Promise.resolve("a"), // A again: the manual retry
    ]);
    await m.act({ id: 1 });
    await m.act({ id: 2 });
    await m.act({ id: 1 });
    const [a, b, retryOfA] = m.keys;
    expect(retryOfA).toBe(a);
    expect(b).not.toBe(a);
  });

  it("gives two different files two keys, and the same file retried after a network error one", async () => {
    const cv = new File(["%PDF-1.7 first"], "cv.pdf", { type: "application/pdf" });
    const other = new File(["%PDF-1.7 first"], "cv.pdf", { type: "application/pdf" }); // equal, not the same
    const m = setup<{ file: File; sendOriginal: boolean }>([
      () => Promise.reject(new TypeError("Failed to fetch")),
      () => Promise.reject(new TypeError("Failed to fetch")),
      () => Promise.resolve("uploaded"),
    ]);
    await m.act({ file: cv, sendOriginal: false }); // no answer: the key is kept
    await m.act({ file: other, sendOriginal: false }); // another file while it is kept
    await m.act({ file: cv, sendOriginal: false }); // the retry: the same file object
    const [first, otherFile, retry] = m.keys;
    expect(otherFile).not.toBe(first);
    expect(retry).toBe(first);
  });

  it("reads FormData by its entries in order, a file by its object", async () => {
    const cv = new File(["a"], "cv.pdf");
    const form = (file: File, mode: string) => {
      const data = new FormData();
      data.append("file", file);
      data.append("mode", mode);
      return data;
    };
    const m = setup<FormData>([
      () => Promise.reject(problem(500, "internal")),
      () => Promise.reject(problem(500, "internal")),
      () => Promise.reject(problem(500, "internal")),
      () => Promise.resolve("ok"),
    ]);
    await m.act(form(cv, "text"));
    await m.act(form(cv, "text")); // another FormData, the same file and fields: the same action
    await m.act(form(cv, "native_pdf")); // another field value
    await m.act(form(new File(["a"], "cv.pdf"), "text")); // another file
    const [first, same, otherField, otherFile] = m.keys;
    expect(same).toBe(first);
    expect(otherField).not.toBe(first);
    expect(otherFile).not.toBe(first);
    expect(new Set(m.keys).size).toBe(3);
  });

  it("answers any failure as a ProblemError and never retries by itself", async () => {
    const m = setup<string>([() => Promise.reject(new TypeError("Failed to fetch"))]);
    await m.act("run");
    expect(m.fn).toHaveBeenCalledTimes(1);
    await waitFor(() => {
      expect(m.result.current.error).toBeInstanceOf(ProblemError);
    });
    expect(m.result.current.error).toMatchObject({ status: 0, code: "network" });
  });
});
