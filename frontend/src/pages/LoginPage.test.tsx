import { createMemoryHistory } from "@tanstack/react-router";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App, createApp } from "../app/App";
import en from "../i18n/en.json";
import { fakeApi, problem } from "../test/api";

afterEach(() => {
  vi.restoreAllMocks();
});

function at(path: string) {
  const app = createApp({ history: createMemoryHistory({ initialEntries: [path] }) });
  render(<App app={app} />);
  return app;
}

async function signIn(user: ReturnType<typeof userEvent.setup>, password = "correct horse") {
  await user.type(await screen.findByLabelText(en["login.username"]), "smoke");
  await user.type(screen.getByLabelText(en["login.password"]), password);
  await user.click(screen.getByRole("button", { name: en["login.submit"] }));
}

function field(name: string): HTMLInputElement | null {
  return document.querySelector<HTMLInputElement>(`input[name=${name}]`);
}

describe("LoginPage", () => {
  it("wrong password shows the generic error", async () => {
    const api = fakeApi({ loginAnswers: [() => problem(401, "unauthenticated")] });
    const app = at("/login");
    const user = userEvent.setup();
    await signIn(user, "wrong");
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toBe(en["login.invalid"]);
    expect(api.logins).toEqual([{ username: "smoke", password: "wrong" }]);
    // the contract of the visual checks and the E2E suite
    expect(field("username")).not.toBeNull();
    expect(field("password")?.type).toBe("password");
    expect(field("totp")).toBeNull();
    expect(document.querySelector("button[type=submit]")).not.toBeNull();
    expect(field("password")?.getAttribute("aria-describedby")).toBe(alert.id);
    expect(app.router.state.location.pathname).toBe("/login");
    expect(screen.getByText(en["login.reset_note"])).toBeTruthy();
  });

  it("totp_required reveals the TOTP field and resends", async () => {
    const api = fakeApi({ loginAnswers: [() => problem(401, "totp_required"), () => new Response(null, { status: 204 })] });
    const app = at("/login");
    const user = userEvent.setup();
    await signIn(user);
    await waitFor(() => {
      expect(field("totp")).not.toBeNull();
    });
    const totp = field("totp");
    expect(document.activeElement).toBe(totp);
    expect(screen.queryByText(en["login.invalid"])).toBeNull();
    await user.type(screen.getByLabelText(en["login.totp"]), "123456");
    await user.click(screen.getByRole("button", { name: en["login.submit"] }));
    await waitFor(() => {
      expect(app.router.state.location.pathname).toBe("/");
    });
    expect(api.logins).toEqual([
      { username: "smoke", password: "correct horse" },
      { username: "smoke", password: "correct horse", totp: "123456" },
    ]);
  });

  it("a TOTP code that is not six digits is refused before it is sent", async () => {
    const api = fakeApi({ loginAnswers: [() => problem(401, "totp_required")] });
    at("/login");
    const user = userEvent.setup();
    await signIn(user);
    await user.type(await screen.findByLabelText(en["login.totp"]), "12345");
    await user.click(screen.getByRole("button", { name: en["login.submit"] }));
    expect((await screen.findByRole("alert")).textContent).toBe(en["login.totp_format"]);
    expect(api.logins).toHaveLength(1);
  });

  it("throttled shows minutes", async () => {
    fakeApi({ loginAnswers: [() => problem(429, "throttled", { retry_after_s: 541 })] });
    at("/login");
    await signIn(userEvent.setup());
    expect((await screen.findByRole("alert")).textContent).toBe(en["login.throttled"].replace("{{minutes}}", "10"));
  });

  it("busy shows try again in a moment", async () => {
    fakeApi({ loginAnswers: [() => problem(429, "throttled", { retry_after_s: 1 })] });
    at("/login");
    await signIn(userEvent.setup());
    expect((await screen.findByRole("alert")).textContent).toBe(en["login.busy"]);
  });

  it("success navigates to a safe redirect only", async () => {
    fakeApi({ loginAnswers: [() => new Response(null, { status: 204 })] });
    const app = at(`/login?redirect=${encodeURIComponent("/settings#budget")}`);
    await signIn(userEvent.setup());
    await waitFor(() => {
      expect(app.router.state.location.href).toBe("/settings#budget");
    });
    expect(await screen.findByRole("heading", { level: 1, name: en["settings.title"] })).toBeTruthy();
  });

  it("success ignores a redirect to another origin", async () => {
    fakeApi({ loginAnswers: [() => new Response(null, { status: 204 })] });
    const app = at(`/login?redirect=${encodeURIComponent("//evil.example/settings")}`);
    await signIn(userEvent.setup());
    await waitFor(() => {
      expect(app.router.state.location.href).toBe("/");
    });
    expect(await screen.findByRole("heading", { level: 1, name: en["jobs.title"] })).toBeTruthy();
  });

  it("shows an unexpected answer as a problem", async () => {
    fakeApi({ loginAnswers: [() => problem(503, "internal")] });
    at("/login");
    await signIn(userEvent.setup());
    expect((await screen.findByRole("alert")).textContent).toContain(en["problem.internal"]);
  });
});
