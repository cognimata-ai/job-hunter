// frontend/src/api/client.test.ts
import { afterEach, describe, expect, it, vi } from "vitest";
import { apiFetch, ProblemError } from "./client";
afterEach(() => vi.restoreAllMocks());

const PROBLEM_JSON = { "content-type": "application/problem+json" };

// A Response body can be read once, so a fetch that answers several calls builds one per call.
function mockFetch(make: () => Response) {
  return vi.spyOn(globalThis, "fetch").mockImplementation(() => Promise.resolve(make()));
}

function problem(status: number, body: unknown, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), { status, headers: { ...PROBLEM_JSON, ...headers } });
}

// The init of fetch call `call`. Each test asserts the number of calls first, so a missing call fails there
// instead of reading as an empty init.
function sentInit(f: ReturnType<typeof mockFetch>, call = 0): RequestInit {
  const init = f.mock.calls[call]?.[1];
  if (init === undefined) {
    throw new Error(`fetch call ${String(call)} was made without an init`);
  }
  return init;
}

describe("apiFetch", () => {
  it("adds X-JH-Request only on state-changing methods and keeps same-origin credentials", async () => {
    const f = mockFetch(() => new Response("{}", { status: 200 }));
    await apiFetch("/api/auth/session");
    await apiFetch("/api/runs", { method: "POST", body: "{}" });
    const [getInit, postInit] = f.mock.calls.map((c) => c[1] as RequestInit);
    expect(new Headers(getInit.headers).has("X-JH-Request")).toBe(false);
    expect(new Headers(postInit.headers).get("X-JH-Request")).toBe("1");
    expect(postInit.credentials).toBe("same-origin");
  });
  it("throws ProblemError from problem details", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify(
      { code: "throttled", title: "Too many attempts", status: 429, request_id: "r-1", errors: [] }),
      { status: 429, headers: { "content-type": "application/problem+json" } }));
    await expect(apiFetch("/api/auth/login", { method: "POST" })).rejects.toMatchObject(
      { status: 429, code: "throttled", requestId: "r-1" } satisfies Partial<ProblemError>);
  });

  describe("request", () => {
    it.each(["POST", "PUT", "PATCH", "DELETE", "post", "patch"])("marks %s as state-changing", async (method) => {
      const f = mockFetch(() => new Response("{}"));
      await apiFetch("/api/x", { method });
      expect(f).toHaveBeenCalledTimes(1);
      expect(new Headers(sentInit(f).headers).get("X-JH-Request")).toBe("1");
    });

    it.each([undefined, "GET", "HEAD", "get"])("does not mark %s", async (method) => {
      const f = mockFetch(() => new Response("{}"));
      await apiFetch("/api/x", method === undefined ? {} : { method });
      expect(f).toHaveBeenCalledTimes(1);
      expect(new Headers(sentInit(f).headers).has("X-JH-Request")).toBe(false);
    });

    it("keeps same-origin credentials whatever the caller passes", async () => {
      const f = mockFetch(() => new Response("{}"));
      await apiFetch("/api/x", { credentials: "include" });
      expect(f).toHaveBeenCalledTimes(1);
      expect(sentInit(f).credentials).toBe("same-origin");
    });

    it("keeps the caller's headers, in any form, and the rest of its init", async () => {
      const f = mockFetch(() => new Response("{}"));
      await apiFetch("/api/runs", {
        method: "POST",
        headers: [["Idempotency-Key", "k-1"]],
        body: "{}",
        cache: "no-store",
      });
      expect(f).toHaveBeenCalledTimes(1);
      const init = sentInit(f);
      expect(new Headers(init.headers).get("Idempotency-Key")).toBe("k-1");
      expect(new Headers(init.headers).get("X-JH-Request")).toBe("1");
      expect(init.cache).toBe("no-store");
      expect(f.mock.calls[0]?.[0]).toBe("/api/runs");
    });

    it("sends a string body as JSON unless the caller chose a content type", async () => {
      const f = mockFetch(() => new Response("{}"));
      await apiFetch("/api/x", { method: "POST", body: "{}" });
      await apiFetch("/api/x", { method: "POST", body: "a", headers: { "Content-Type": "text/plain" } });
      await apiFetch("/api/x");
      expect(f).toHaveBeenCalledTimes(3);
      expect(new Headers(sentInit(f, 0).headers).get("content-type")).toBe("application/json");
      expect(new Headers(sentInit(f, 1).headers).get("content-type")).toBe("text/plain");
      expect(new Headers(sentInit(f, 2).headers).has("content-type")).toBe(false);
    });
  });

  describe("success", () => {
    it("returns the parsed JSON body", async () => {
      mockFetch(() => new Response(JSON.stringify({ username: "u" }), { status: 200 }));
      await expect(apiFetch<{ username: string }>("/api/auth/session")).resolves.toEqual({ username: "u" });
    });

    it("returns undefined for a 204", async () => {
      mockFetch(() => new Response(null, { status: 204 }));
      await expect(apiFetch<undefined>("/api/auth/logout", { method: "POST" })).resolves.toBeUndefined();
    });

    it.each([200, 201, 202])("returns undefined for a %i with an empty body", async (status) => {
      mockFetch(() => new Response(null, { status, headers: { "content-type": "application/json" } }));
      await expect(apiFetch<undefined>("/api/x", { method: "POST" })).resolves.toBeUndefined();
    });

    it("answers a 2xx whose body is not JSON with code internal and the header's request id", async () => {
      mockFetch(() => new Response("<html>ok</html>",
        { status: 200, headers: { "content-type": "text/html", "x-request-id": "r-6" } }));
      const error = await apiFetch("/api/x").catch((e: unknown) => e);
      expect(error).toBeInstanceOf(ProblemError);
      expect(error).toMatchObject({ status: 200, code: "internal", requestId: "r-6", errors: [] });
    });
  });

  describe("failure", () => {
    it("fills code, request id and field errors from a problem document", async () => {
      const errors = [
        { field: "body.username", code: "missing", message: "Field required" },
        { field: "body.totp", code: "string_pattern_mismatch", message: "bad" },
      ];
      mockFetch(() => problem(422, {
        code: "validation_failed", title: "Request validation failed", status: 422, request_id: "r-2", errors,
      }, { "x-request-id": "ignored-for-a-problem-body" }));
      const error = await apiFetch("/api/auth/login", { method: "POST" }).catch((e: unknown) => e);
      expect(error).toBeInstanceOf(ProblemError);
      expect(error).toBeInstanceOf(Error);
      expect(error).toMatchObject({
        name: "ProblemError",
        message: "Request validation failed",
        status: 422,
        code: "validation_failed",
        requestId: "r-2",
        errors,
      });
    });

    it("reads a problem content type with parameters and any case", async () => {
      mockFetch(() => problem(404, { code: "not_found", title: "Not Found" },
        { "content-type": "Application/Problem+JSON; charset=utf-8" }));
      await expect(apiFetch("/api/x")).rejects.toMatchObject({ status: 404, code: "not_found" });
    });

    it("defaults errors to [] and takes the request id from the header when the body has none", async () => {
      mockFetch(() => problem(401, { code: "unauthenticated", title: "Authentication required" },
        { "x-request-id": "r-3" }));
      await expect(apiFetch("/api/x")).rejects.toMatchObject(
        { status: 401, code: "unauthenticated", requestId: "r-3", errors: [] });
    });

    it("drops malformed field errors", async () => {
      const good = { field: "a", code: "c", message: "m" };
      mockFetch(() => problem(422, { code: "validation_failed", title: "t", errors: [good, { field: "b" }, 7, null] }));
      await expect(apiFetch("/api/x")).rejects.toMatchObject({ errors: [good] });
    });

    it("answers a response that is not a problem document with code internal and the header's request id", async () => {
      mockFetch(() => new Response("<html>Bad Gateway</html>",
        { status: 502, headers: { "content-type": "text/html", "x-request-id": "r-4" } }));
      const error = await apiFetch("/api/x").catch((e: unknown) => e);
      expect(error).toBeInstanceOf(ProblemError);
      expect(error).toMatchObject({ status: 502, code: "internal", requestId: "r-4", errors: [] });
    });

    it("has a null request id when a non-problem response carries no header", async () => {
      mockFetch(() => new Response("", { status: 500 }));
      await expect(apiFetch("/api/x")).rejects.toMatchObject(
        { status: 500, code: "internal", requestId: null, errors: [] });
    });

    it("does not take a plain JSON body for a problem, even one with a code (the readiness report)", async () => {
      mockFetch(() => new Response(JSON.stringify({ code: "x", failing: ["db"] }),
        { status: 503, headers: { "content-type": "application/json" } }));
      await expect(apiFetch("/readyz")).rejects.toMatchObject({ status: 503, code: "internal" });
    });

    it("answers a problem content type with an unparsable body with code internal", async () => {
      mockFetch(() => new Response("{not json",
        { status: 500, headers: { ...PROBLEM_JSON, "x-request-id": "r-5" } }));
      await expect(apiFetch("/api/x")).rejects.toMatchObject(
        { status: 500, code: "internal", requestId: "r-5", errors: [] });
    });

    it("answers a problem content type whose body has no string code with code internal", async () => {
      mockFetch(() => problem(500, [1]));
      await expect(apiFetch("/api/x")).rejects.toMatchObject({ status: 500, code: "internal" });
    });

    it("lets a network failure through as it is", async () => {
      const failure = new TypeError("Failed to fetch");
      vi.spyOn(globalThis, "fetch").mockRejectedValue(failure);
      const error = await apiFetch("/api/x").catch((e: unknown) => e);
      expect(error).toBe(failure);
      expect(error).not.toBeInstanceOf(ProblemError);
    });
  });
});
