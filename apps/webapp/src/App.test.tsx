import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "./App";
import type { QueueItem, Review } from "./api";

const LAUNCH_DATA = "query_id=x&user=%7B%22id%22%3A1%7D&auth_date=1&hash=abc";

const QUEUE: QueueItem[] = [
  {
    id: 7,
    reason: "under_18",
    urgent: true,
    kind: "chat",
    created_at: "2026-11-21T12:00:00Z",
    has_details: true,
    reported_nickname: "Moon Child",
  },
];

const SPAM_REPORT: QueueItem = {
  id: 8,
  reason: "spam",
  urgent: false,
  kind: "profile",
  created_at: "2026-11-21T13:00:00Z",
  has_details: false,
  reported_nickname: "Sun Ray",
};

const person = (nickname: string) => ({
  public_id: "ABC123",
  nickname,
  age: 25,
  gender: "female",
  country_code: "IN",
  city: null,
  bio: null,
  has_photo: true,
  status: "active",
  deleted: false,
});

const REVIEW: Review = {
  id: 7,
  kind: "chat",
  reason: "under_18",
  urgent: true,
  details: "Says she is 15",
  status: "open",
  outcome: null,
  created_at: "2026-11-21T12:00:00Z",
  reporter: person("Night Owl"),
  reported: person("Moon Child"),
  record: {
    actions: [
      { kind: "suspend", rule: "threats", days: 3, ends_at: "2026-11-23T12:00:00Z", created_at: "2026-11-20T12:00:00Z" },
      { kind: "warn", rule: "spam", days: null, ends_at: null, created_at: "2026-11-18T12:00:00Z" },
    ],
    reports_received: 2,
    reports_open: 1,
  },
  messages: [
    {
      source: "chat",
      id: 1,
      sender: "reported",
      kind: "photo",
      text: "look",
      edited_text: null,
      edited_at: null,
      private: true,
      view_once: true,
      viewable: true,
      sent_at: "2026-11-21T11:00:00Z",
    },
    {
      source: "chat",
      id: 2,
      sender: "reporter",
      kind: "dice",
      text: null,
      edited_text: null,
      edited_at: null,
      private: false,
      view_once: false,
      viewable: false,
      sent_at: "2026-11-21T11:01:00Z",
    },
  ],
  requests: [],
};

type Handler = (path: string, init: RequestInit) => { status: number; body: unknown };

let calls: { path: string; init: RequestInit }[];
let confirmAnswer: boolean;

function serve(handler: Handler) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (path: string, init: RequestInit) => {
      // Every request, reads and writes alike, is signed and carries no cookies.
      expect(new Headers(init.headers).get("Authorization")).toBe(`tma ${LAUNCH_DATA}`);
      expect(init.credentials).toBe("omit");
      calls.push({ path, init });
      const { status, body } = handler(path, init);
      return new Response(JSON.stringify(body), { status });
    }),
  );
}

function defaultHandler(path: string, init: RequestInit): { status: number; body: unknown } {
  if (path === "/api/moderation/me") return { status: 200, body: { role: "moderator", nickname: "Mod Max" } };
  if (path === "/api/moderation/reports") return { status: 200, body: QUEUE };
  if (path === "/api/moderation/reports/7" && !init.method?.startsWith("P")) return { status: 200, body: REVIEW };
  if (path.endsWith("/media")) return { status: 200, body: { deleted_after_seconds: 600 } };
  if (path.endsWith("/action")) return { status: 200, body: { result: "applied" } };
  return { status: 404, body: { error: "not_found" } };
}

beforeEach(() => {
  calls = [];
  confirmAnswer = true;
  window.Telegram = {
    WebApp: {
      initData: LAUNCH_DATA,
      colorScheme: "light",
      ready: vi.fn(),
      expand: vi.fn(),
      showConfirm: vi.fn((_message: string, callback: (ok: boolean) => void) => callback(confirmAnswer)),
      BackButton: { show: vi.fn(), hide: vi.fn(), onClick: vi.fn(), offClick: vi.fn() },
    },
  };
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  delete window.Telegram;
});

async function openReport() {
  const user = userEvent.setup();
  render(<App />);
  await user.click(await screen.findByRole("button", { name: /Appears under 18/ }));
  await screen.findByRole("article", { name: "Report 7" });
  return user;
}

function bodyOf(call: { init: RequestInit } | undefined): unknown {
  const body = call?.init.body;
  return typeof body === "string" ? JSON.parse(body) : undefined;
}

function lastBody(): unknown {
  return bodyOf(calls.at(-1));
}

function actionCall() {
  return calls.find((call) => call.path.endsWith("/action"));
}

/** The form's submit button, after the action buttons with the same name. */
function submitButton(name: string): HTMLElement {
  const buttons = screen.getAllByRole("button", { name });
  const last = buttons[buttons.length - 1];
  if (last === undefined) throw new Error(`no ${name} button`);
  return last;
}

describe("signing in", () => {
  it("sends Telegram's launch data with every request, writes included", async () => {
    serve(defaultHandler);
    render(<App />);
    expect(await screen.findByText(/Mod Max · Moderator/)).toBeTruthy();
    // The shared fetch mock checks the header on each call; make writes too.
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: /Appears under 18/ }));
    await screen.findByRole("article", { name: "Report 7" });
    await user.click(screen.getByRole("button", { name: /profile photo/ }));
    await user.click(screen.getByRole("button", { name: "Close, no action" }));
    await screen.findByText("Report closed with no action.");
    expect(calls.map((call) => call.path)).toEqual([
      "/api/moderation/me",
      "/api/moderation/reports",
      "/api/moderation/reports/7",
      "/api/moderation/reports/7/media",
      "/api/moderation/reports/7/action",
      "/api/moderation/reports",
    ]);
  });

  it("asks to reopen from the bot without launch data", async () => {
    serve(defaultHandler);
    window.Telegram = undefined;
    render(<App />);
    expect((await screen.findByRole("alert")).textContent).toMatch(/open this again from 🛡 Moderation/);
    expect(calls).toEqual([]);
  });

  it("explains a refusal", async () => {
    serve(() => ({ status: 403, body: { error: "forbidden" } }));
    render(<App />);
    expect((await screen.findByRole("alert")).textContent).toMatch(/for RealChat moderators and admins/);
  });
});

describe("the queue", () => {
  it("lists open reports and says when there are none", async () => {
    serve(defaultHandler);
    render(<App />);
    const row = await screen.findByRole("button", { name: /Appears under 18/ });
    expect(row.textContent).toMatch(/Urgent/);
    expect(row.textContent).toMatch(/Moon Child/);
    cleanup();
    serve((path) =>
      path === "/api/moderation/reports"
        ? { status: 200, body: [] }
        : { status: 200, body: { role: "admin", nickname: null } },
    );
    render(<App />);
    expect(await screen.findByText("No open reports.")).toBeTruthy();
  });
});

describe("a report", () => {
  it("shows the people, the record, and the chat", async () => {
    serve(defaultHandler);
    await openReport();
    expect(screen.getByText("“Says she is 15”")).toBeTruthy();
    expect(screen.getByText(/Reports received: 2 \(1 open\)/)).toBeTruthy();
    expect(screen.getByText(/Warning · Spam, scams, or advertising/)).toBeTruthy();
    // A suspension shows when it ends.
    expect(screen.getByText(/Suspension \(3 days, until .*2026/)).toBeTruthy();
    const chat = screen.getByRole("region", { name: "Chat" });
    expect(within(chat).getByText(/Private mode · view once/)).toBeTruthy();
    expect(within(chat).getByText("Dice")).toBeTruthy();
    // Only the photo can be sent; the dice has no file.
    expect(within(chat).getAllByRole("button", { name: /View in Telegram/ })).toHaveLength(1);
    expect(window.Telegram?.WebApp?.BackButton?.show).toHaveBeenCalled();
  });

  it("sends media to the moderator's chat", async () => {
    serve(defaultHandler);
    const user = await openReport();
    const chat = screen.getByRole("region", { name: "Chat" });
    await user.click(within(chat).getByRole("button", { name: /View in Telegram/ }));
    expect(await screen.findByText(/queued for deletion after 10 minutes/)).toBeTruthy();
    expect(calls.at(-1)?.path).toBe("/api/moderation/reports/7/media");
    expect(lastBody()).toEqual({ source: "chat", item_id: 1 });
    await user.click(screen.getByRole("button", { name: /profile photo/ }));
    await waitFor(() => expect(lastBody()).toEqual({ source: "photo" }));
  });
});

describe("actions", () => {
  it("asks for a rule, confirms, then sends the action", async () => {
    serve(defaultHandler);
    const user = await openReport();
    await user.click(screen.getByRole("button", { name: "Suspend" }));
    await user.click(submitButton("Suspend"));
    expect((await screen.findByRole("alert")).textContent).toMatch(/Choose the rule/);
    expect(calls.some((call) => call.path.endsWith("/action"))).toBe(false);

    await user.selectOptions(screen.getByLabelText(/Rule broken/), "harassment");
    await user.selectOptions(screen.getByLabelText(/Days/), "7");
    await user.click(submitButton("Suspend"));

    expect(window.Telegram?.WebApp?.showConfirm).toHaveBeenCalledWith(
      "Suspend Moon Child for 7 days for: Harassment, hate, or bullying?",
      expect.any(Function),
    );
    expect(await screen.findByText("Account suspended. The report is closed.")).toBeTruthy();
    expect(actionCall()?.path).toBe("/api/moderation/reports/7/action");
    expect(bodyOf(actionCall())).toEqual({ kind: "suspend", rule: "harassment", days: 7 });
    // Back on the queue, reloaded.
    expect(calls.at(-1)?.path).toBe("/api/moderation/reports");
  });

  it("sends nothing when the confirmation is declined", async () => {
    serve(defaultHandler);
    confirmAnswer = false;
    const user = await openReport();
    await user.click(screen.getByRole("button", { name: "Close, no action" }));
    expect(window.Telegram?.WebApp?.showConfirm).toHaveBeenCalled();
    expect(calls.some((call) => call.path.endsWith("/action"))).toBe(false);
  });

  it("warns before a ban that only an admin can lift it", async () => {
    serve(defaultHandler);
    const user = await openReport();
    await user.click(screen.getByRole("button", { name: "Ban" }));
    await user.selectOptions(screen.getByLabelText(/Rule broken/), "under_18");
    await user.click(submitButton("Ban"));
    await waitFor(() => expect(bodyOf(actionCall())).toEqual({ kind: "ban", rule: "under_18" }));
    expect(window.Telegram?.WebApp?.showConfirm).toHaveBeenCalledWith(
      expect.stringMatching(/Only an admin can lift a ban/),
      expect.any(Function),
    );
  });

  it("says when someone else already handled the report", async () => {
    serve((path, init) =>
      path.endsWith("/action")
        ? { status: 409, body: { error: "report_closed" } }
        : defaultHandler(path, init),
    );
    const user = await openReport();
    await user.click(screen.getByRole("button", { name: "Close, no action" }));
    expect((await screen.findByRole("alert")).textContent).toBe("Someone already handled this report.");
  });
});

describe("resilience", () => {
  it("offers to retry when the queue can't be loaded", async () => {
    let failing = true;
    serve((path, init) =>
      path === "/api/moderation/reports" && failing ? { status: 500, body: {} } : defaultHandler(path, init),
    );
    const user = userEvent.setup();
    render(<App />);
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toMatch(/couldn't be loaded/);
    failing = false;
    await user.click(within(alert).getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("button", { name: /Appears under 18/ })).toBeTruthy();
    // Still signed in: the header is still there.
    expect(screen.getByText(/Mod Max · Moderator/)).toBeTruthy();
  });

  it("sends one copy however quickly the media button is tapped", async () => {
    let release: () => void = () => undefined;
    serve(defaultHandler);
    const respond = vi.mocked(fetch).getMockImplementation();
    vi.mocked(fetch).mockImplementation(async (input, init) => {
      if (String(input).endsWith("/media")) await new Promise<void>((resolve) => (release = resolve));
      return respond ? respond(input, init) : new Response("{}");
    });
    const user = await openReport();
    const button = within(screen.getByRole("region", { name: "Chat" })).getByRole("button", {
      name: /View in Telegram/,
    });
    await user.click(button);
    await user.click(button);
    await user.click(button);
    release();
    await screen.findByText(/queued for deletion after 10 minutes/);
    // Count every request made, including any still waiting for an answer.
    const sent = vi.mocked(fetch).mock.calls.filter(([input]) => String(input).endsWith("/media"));
    expect(sent).toHaveLength(1);
  });

  it("clears the missing-rule message once a rule is chosen", async () => {
    serve(defaultHandler);
    const user = await openReport();
    await user.click(screen.getByRole("button", { name: "Warn" }));
    await user.click(screen.getByRole("button", { name: "Send warning" }));
    expect((await screen.findByRole("alert")).textContent).toMatch(/Choose the rule/);
    await user.selectOptions(screen.getByLabelText(/Rule broken/), "spam");
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("ignores a report that answers after the moderator went back", async () => {
    let release: () => void = () => undefined;
    const queue: QueueItem[] = [...QUEUE, { ...SPAM_REPORT }];
    serve((path, init) => {
      if (path === "/api/moderation/reports") return { status: 200, body: queue };
      if (path === "/api/moderation/reports/8") return { status: 404, body: { error: "not_found" } };
      return defaultHandler(path, init);
    });
    const respond = vi.mocked(fetch).getMockImplementation();
    vi.mocked(fetch).mockImplementation(async (input, init) => {
      if (String(input) === "/api/moderation/reports/8") await new Promise<void>((resolve) => (release = resolve));
      return respond ? respond(input, init) : new Response("{}");
    });
    const user = userEvent.setup();
    render(<App />);
    await user.click(await screen.findByRole("button", { name: /Spam or scam/ }));
    // Back to the queue before report 8 answers, then open report 7.
    const back = vi.mocked(window.Telegram?.WebApp?.BackButton?.onClick ?? vi.fn()).mock.calls.at(-1)?.[0];
    act(() => back?.());
    await user.click(await screen.findByRole("button", { name: /Appears under 18/ }));
    await screen.findByRole("article", { name: "Report 7" });
    release(); // report 8's late "not found"
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(screen.getByRole("article", { name: "Report 7" })).toBeTruthy();
    expect(screen.queryByText("That report wasn't found.")).toBeNull();
  });
});
