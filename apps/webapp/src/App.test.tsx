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

const ACCOUNT = {
  id: 42,
  person: { ...person("Moon Child"), status: "banned" },
  suspended_until: null,
  record: { actions: [], reports_received: 1, reports_open: 1 },
  reports_received: [
    { id: 7, reason: "under_18", kind: "chat", status: "open", outcome: null, created_at: "2026-11-21T12:00:00Z" },
  ],
  reports_made: [],
};

const THREADS = [
  {
    kind: "chat",
    ref: 5,
    partner_nickname: "Night Owl",
    first_at: "2026-11-21T11:00:00Z",
    last_at: "2026-11-21T11:05:00Z",
    messages: 2,
  },
];

function lookupHandler(role: "moderator" | "admin") {
  return (path: string, init: RequestInit): { status: number; body: unknown } => {
    if (path === "/api/moderation/me") return { status: 200, body: { role, nickname: "Staff Sam" } };
    if (path.startsWith("/api/moderation/accounts?query=")) {
      const query = decodeURIComponent(path.split("=")[1] ?? "");
      return { status: 200, body: query === "abcdefg" ? [{ id: 42, public_id: "abcdefg", nickname: "Moon Child", status: "banned" }] : [] };
    }
    if (path === "/api/moderation/accounts/42") return { status: 200, body: ACCOUNT };
    if (path === "/api/moderation/accounts/42/action") return { status: 200, body: { result: "applied" } };
    if (path === "/api/moderation/accounts/42/threads") return { status: 200, body: THREADS };
    if (path === "/api/moderation/accounts/42/threads/open") {
      const [first, second] = REVIEW.messages;
      return { status: 200, body: [{ ...first, sender: "account" }, { ...second, sender: "partner" }] };
    }
    if (path === "/api/moderation/accounts/42/media") return { status: 200, body: { deleted_after_seconds: 600 } };
    return defaultHandler(path, init);
  };
}

async function openAccount(role: "moderator" | "admin") {
  serve(lookupHandler(role));
  const user = userEvent.setup();
  render(<App />);
  await user.click(await screen.findByRole("button", { name: "Lookup" }));
  await user.type(screen.getByLabelText(/RealChat ID/), "abcdefg");
  await user.click(screen.getByRole("button", { name: "Search" }));
  await screen.findByRole("article", { name: "Account 42" });
  return user;
}

describe("account lookup", () => {
  it("says when nothing is found", async () => {
    serve(lookupHandler("moderator"));
    const user = userEvent.setup();
    render(<App />);
    await user.click(await screen.findByRole("button", { name: "Lookup" }));
    await user.type(screen.getByLabelText(/RealChat ID/), "zzzzzzz");
    await user.click(screen.getByRole("button", { name: "Search" }));
    expect(await screen.findByText("No account found.")).toBeTruthy();
  });

  it("opens the account; a moderator can't lift a ban", async () => {
    await openAccount("moderator");
    expect(screen.getByText(/Status: banned/)).toBeTruthy();
    const received = screen.getByRole("region", { name: "Reports received" });
    expect(within(received).getByRole("button", { name: /Appears under 18/ })).toBeTruthy();
    // Only admins lift bans, and moderators get no stored chats.
    expect(screen.queryByRole("button", { name: "Lift restriction" })).toBeNull();
    expect(screen.queryByRole("region", { name: "Stored chats" })).toBeNull();
  });

  it("lets an admin lift a ban", async () => {
    const user = await openAccount("admin");
    await user.click(screen.getByRole("button", { name: "Lift restriction" }));
    expect(window.Telegram?.WebApp?.showConfirm).toHaveBeenCalledWith(
      "Lift the restriction on Moon Child?",
      expect.any(Function),
    );
    await waitFor(() => expect(bodyOf(actionCall())).toEqual({ kind: "lift" }));
    expect(actionCall()?.path).toBe("/api/moderation/accounts/42/action");
    expect(await screen.findByText("Restriction lifted; they'll be notified.")).toBeTruthy();
  });

  it("shows the result of acting on a report opened from the account", async () => {
    const user = await openAccount("moderator");
    await user.click(within(screen.getByRole("region", { name: "Reports received" })).getByRole("button"));
    await screen.findByRole("article", { name: "Report 7" });
    await user.click(screen.getByRole("button", { name: "Close, no action" }));
    await screen.findByRole("article", { name: "Account 42" });
    expect(await screen.findByText("Report closed with no action.")).toBeTruthy();
  });

  it("opens a report from the account and goes back to it", async () => {
    const user = await openAccount("moderator");
    await user.click(within(screen.getByRole("region", { name: "Reports received" })).getByRole("button"));
    await screen.findByRole("article", { name: "Report 7" });
    const back = vi.mocked(window.Telegram?.WebApp?.BackButton?.onClick ?? vi.fn()).mock.calls.at(-1)?.[0];
    act(() => back?.());
    expect(await screen.findByRole("article", { name: "Account 42" })).toBeTruthy();
  });

  it("lets an admin open stored chats only with a reason", async () => {
    const user = await openAccount("admin");
    const stored = screen.getByRole("region", { name: "Stored chats" });
    await user.click(within(stored).getByRole("button", { name: "Open stored chats" }));
    expect((await within(stored).findByRole("alert")).textContent).toMatch(/at least 5 characters/);
    expect(calls.some((call) => call.path.endsWith("/threads"))).toBe(false);

    await user.type(within(stored).getByLabelText(/Reason/), "Appeal from support");
    await user.click(within(stored).getByRole("button", { name: "Open stored chats" }));
    const thread = await within(stored).findByRole("button", { name: /Chat with Night Owl/ });
    expect(bodyOf(calls.find((call) => call.path.endsWith("/threads")))).toEqual({ reason: "Appeal from support" });

    await user.click(thread);
    const messages = await within(stored).findByRole("list", { name: "Messages" });
    // Labelled from the account's side, not as a report.
    expect(within(messages).queryByText(/Reporter|Reported/)).toBeNull();
    expect(within(messages).getAllByText(/^(Account|Partner) ·/).length).toBe(2);
    expect(bodyOf(calls.find((call) => call.path.endsWith("/threads/open")))).toEqual({
      reason: "Appeal from support",
      kind: "chat",
      ref: 5,
    });
    await user.click(within(messages).getByRole("button", { name: /View in Telegram/ }));
    expect(await within(stored).findByText(/queued for deletion after 10 minutes/)).toBeTruthy();
    expect(bodyOf(calls.find((call) => call.path.endsWith("/accounts/42/media")))).toEqual({
      reason: "Appeal from support",
      source: "chat",
      item_id: 1,
    });
  });

  it("ignores a search that answers after switching to Reports", async () => {
    let release: () => void = () => undefined;
    serve(lookupHandler("moderator"));
    const respond = vi.mocked(fetch).getMockImplementation();
    vi.mocked(fetch).mockImplementation(async (input, init) => {
      if (String(input).startsWith("/api/moderation/accounts?query=")) {
        await new Promise<void>((resolve) => (release = resolve));
      }
      return respond ? respond(input, init) : new Response("{}");
    });
    const user = userEvent.setup();
    render(<App />);
    await user.click(await screen.findByRole("button", { name: "Lookup" }));
    await user.type(screen.getByLabelText(/RealChat ID/), "abcdefg");
    await user.click(screen.getByRole("button", { name: "Search" }));
    await user.click(screen.getByRole("button", { name: "Reports" }));
    await screen.findByRole("region", { name: "Open reports" });
    release();
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(screen.queryByRole("article", { name: "Account 42" })).toBeNull();
    expect(screen.getByRole("region", { name: "Open reports" })).toBeTruthy();
  });
});

describe("opening from an alert", () => {
  afterEach(() => window.history.replaceState(null, "", "/"));

  it("opens the report named in the address, and Back returns to the queue", async () => {
    window.history.replaceState(null, "", "/moderation/?report=7");
    serve(defaultHandler);
    render(<App />);
    expect(await screen.findByRole("article", { name: "Report 7" })).toBeTruthy();
    const back = vi.mocked(window.Telegram?.WebApp?.BackButton?.onClick ?? vi.fn()).mock.calls.at(-1)?.[0];
    act(() => back?.());
    expect(await screen.findByRole("region", { name: "Open reports" })).toBeTruthy();
  });

  it("ignores an address that isn't a report id", async () => {
    window.history.replaceState(null, "", "/moderation/?report=abc");
    serve(defaultHandler);
    render(<App />);
    expect(await screen.findByRole("region", { name: "Open reports" })).toBeTruthy();
  });
});

const USER_ROW = {
  id: 42,
  public_id: "abcdefg",
  nickname: "Moon Child",
  age: 25,
  gender: "female",
  country_code: "IN",
  status: "active",
  role: "user",
  vip_until: "2026-12-30T00:00:00Z",
  coins: 20,
  last_active_at: "2026-11-21T12:00:00Z",
  chats_30d: 5,
  joined_at: "2026-10-01T12:00:00Z",
  reports_received: 2,
};

function usersHandler(total: number) {
  return (path: string, init: RequestInit): { status: number; body: unknown } => {
    if (path.startsWith("/api/moderation/users?")) {
      const page = Number(new URLSearchParams(path.split("?")[1]).get("page"));
      return {
        status: 200,
        body: { rows: [{ ...USER_ROW, id: 42 + page, nickname: `User ${page}` }], total, page, page_size: 1 },
      };
    }
    return lookupHandler("admin")(path, init);
  };
}

describe("the user directory", () => {
  it("is offered to admins only", async () => {
    serve(lookupHandler("moderator"));
    render(<App />);
    await screen.findByRole("button", { name: "Lookup" });
    expect(screen.queryByRole("button", { name: "Users" })).toBeNull();
  });

  it("sends only the filters that are set, and loads more pages", async () => {
    serve(usersHandler(2));
    const user = userEvent.setup();
    render(<App />);
    await user.click(await screen.findByRole("button", { name: "Users" }));
    expect(await screen.findByText(/2 users/)).toBeTruthy();
    expect(calls.at(-1)?.path).toBe("/api/moderation/users?sort=newest&descending=true&page=0");

    await user.click(screen.getByText("Filters"));
    await user.selectOptions(screen.getByLabelText("VIP"), "true");
    await user.type(screen.getByLabelText(/Country/), "in");
    await user.selectOptions(screen.getByLabelText("Sort by"), "chats");
    await user.click(screen.getByRole("button", { name: "Apply" }));
    await waitFor(() =>
      expect(calls.at(-1)?.path).toBe(
        "/api/moderation/users?vip=true&country=IN&sort=chats&descending=true&page=0",
      ),
    );

    await user.click(await screen.findByRole("button", { name: "Load more" }));
    expect(await screen.findByRole("button", { name: /User 1/ })).toBeTruthy();
    expect(screen.getByRole("button", { name: /User 0/ })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Load more" })).toBeNull();
  });

  it("opens an account and goes back with the filters kept", async () => {
    serve(usersHandler(1));
    const user = userEvent.setup();
    render(<App />);
    await user.click(await screen.findByRole("button", { name: "Users" }));
    await user.type(screen.getByLabelText(/Search by RealChat ID or nickname/), "moon");
    await user.click(screen.getByRole("button", { name: "Apply" }));
    await user.click(await screen.findByRole("button", { name: /User 0/ }));
    await screen.findByRole("article", { name: "Account 42" });
    const back = vi.mocked(window.Telegram?.WebApp?.BackButton?.onClick ?? vi.fn()).mock.calls.at(-1)?.[0];
    act(() => back?.());
    expect((await screen.findByLabelText(/Search by RealChat ID or nickname/) as HTMLInputElement).value).toBe("moon");
    await waitFor(() => expect(calls.at(-1)?.path).toContain("query=moon"));
  });

  it("clearing reloads even when nothing was set", async () => {
    serve(usersHandler(1));
    const user = userEvent.setup();
    render(<App />);
    await user.click(await screen.findByRole("button", { name: "Users" }));
    await screen.findByText(/1 user/);
    const before = calls.length;
    await user.click(screen.getByRole("button", { name: "Clear" }));
    await waitFor(() => expect(calls.length).toBe(before + 1));
    expect(await screen.findByRole("button", { name: /User 0/ })).toBeTruthy();
  });

  it("retries a page that failed instead of skipping it", async () => {
    let failPage1 = true;
    serve((path, init) => {
      if (path.startsWith("/api/moderation/users?") && path.endsWith("page=1") && failPage1) {
        return { status: 500, body: {} };
      }
      return usersHandler(3)(path, init);
    });
    const user = userEvent.setup();
    render(<App />);
    await user.click(await screen.findByRole("button", { name: "Users" }));
    await user.click(await screen.findByRole("button", { name: "Load more" }));
    const alert = await screen.findByRole("alert");
    // No Load more while a page has failed, so page 1 can't be skipped.
    expect(screen.queryByRole("button", { name: "Load more" })).toBeNull();
    failPage1 = false;
    await user.click(within(alert).getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("button", { name: /User 1/ })).toBeTruthy();
    const pages = calls.filter((call) => call.path.startsWith("/api/moderation/users?")).map((call) => call.path.split("page=")[1]);
    expect(pages).toEqual(["0", "1", "1"]);
  });

  it("clears the old count when new filters are applied", async () => {
    let release: () => void = () => undefined;
    serve(usersHandler(1));
    const respond = vi.mocked(fetch).getMockImplementation();
    const user = userEvent.setup();
    render(<App />);
    await user.click(await screen.findByRole("button", { name: "Users" }));
    await screen.findByText(/1 user/);
    vi.mocked(fetch).mockImplementation(async (input, init) => {
      await new Promise<void>((resolve) => (release = resolve));
      return respond ? respond(input, init) : new Response("{}");
    });
    await user.click(screen.getByRole("button", { name: "Apply" }));
    expect(screen.queryByText(/1 user/)).toBeNull();
    release();
    expect(await screen.findByText(/1 user/)).toBeTruthy();
  });
});

const GROWTH = {
  period: "30d",
  bucket_days: 1,
  buckets: [
    { start: "2026-11-21", signups: 2, onboarded: 1, vip_purchases: 0, active: 3, active_7d: 4, active_30d: 5 },
    { start: "2026-11-22", signups: 7, onboarded: 6, vip_purchases: 1, active: 8, active_7d: 9, active_30d: 10 },
  ],
  totals: {
    by_status: { active: 12, banned: 1 },
    onboarded: 11,
    vip_now: 2,
    active_today: 8,
    active_7d: 9,
    active_30d: 10,
  },
};

const YEARLY = {
  ...GROWTH,
  period: "12m",
  bucket_days: 7,
  buckets: [
    { start: "2025-12-29", signups: 4, onboarded: 3, vip_purchases: 2, active: 20, active_7d: 20, active_30d: 40 },
    { start: "2026-01-05", signups: 5, onboarded: 4, vip_purchases: 1, active: 21, active_7d: 21, active_30d: 41 },
  ],
};

const NO_REPORTS = { spam: 0, harassment: 0, sexual: 0, under_18: 0, threats: 0, other: 0 };
const NO_ACTIONS = { close: 0, warn: 0, suspend: 0, ban: 0, lift: 0 };

const MODERATION = {
  period: "30d",
  bucket_days: 1,
  buckets: [
    { start: "2026-11-21", reports: { ...NO_REPORTS, spam: 2 } },
    { start: "2026-11-22", reports: { ...NO_REPORTS, harassment: 1, other: 3 } },
  ],
  backlog: { open: 4, urgent: 1, oldest_open_at: "2026-11-12T06:30:00Z" },
  handling: { closed: 2, action_taken: 1, no_action: 1, median_seconds: 738000, longest_seconds: 1468800 },
  moderators: [
    { nickname: "Mod Max", public_id: "mmmmmmm", actions: { ...NO_ACTIONS, warn: 2, ban: 1 }, total: 3 },
    { nickname: null, public_id: null, actions: { ...NO_ACTIONS, close: 1 }, total: 1 },
  ],
  most_reported: [
    { id: 42, nickname: "Moon Child", public_id: "abcdefg", status: "banned", reports: 3, open: 1, findable: true },
    { id: 43, nickname: null, public_id: null, status: "deleted", reports: 1, open: 1, findable: false },
  ],
};

const NO_REFUNDS = { account_deleted: 0, admin: 0, telegram: 0 };

const REVENUE = {
  period: "30d",
  bucket_days: 1,
  buckets: [
    {
      start: "2026-11-21",
      coin_stars: 100,
      vip_stars: 780,
      coin_packs: 1,
      vip_passes: 1,
      first_vip: 1,
      refunds: NO_REFUNDS,
      refunded_stars: 0,
    },
    {
      start: "2026-11-22",
      coin_stars: 250,
      vip_stars: 0,
      coin_packs: 2,
      vip_passes: 0,
      first_vip: 0,
      refunds: { ...NO_REFUNDS, admin: 1, telegram: 2 },
      refunded_stars: 300,
    },
  ],
  totals: { stars: 5000, refunds_due: 1, refunds_due_stars: 780, vip_buyers: 3, repeat_vip_buyers: 1, onboarded: 40 },
};

function analyticsHandler(role: "moderator" | "admin") {
  return (path: string, init: RequestInit): { status: number; body: unknown } => {
    if (path.startsWith("/api/moderation/analytics/growth?")) {
      return { status: 200, body: path.endsWith("period=12m") ? YEARLY : GROWTH };
    }
    if (path.startsWith("/api/moderation/analytics/moderation?")) return { status: 200, body: MODERATION };
    if (path.startsWith("/api/moderation/analytics/revenue?")) return { status: 200, body: REVENUE };
    return lookupHandler(role)(path, init);
  };
}

function tableCells(region: string): (string | null)[] {
  const chart = screen.getByRole("region", { name: region });
  return within(within(chart).getByRole("table")).getAllByRole("cell").map((cell) => cell.textContent);
}

describe("analytics", () => {
  beforeEach(() => {
    // jsdom has no ResizeObserver; the chart container only needs it to exist.
    vi.stubGlobal(
      "ResizeObserver",
      class {
        observe() {}
        unobserve() {}
        disconnect() {}
      },
    );
  });

  it("is offered to admins only", async () => {
    serve(analyticsHandler("moderator"));
    render(<App />);
    await screen.findByRole("button", { name: "Lookup" });
    expect(screen.queryByRole("button", { name: "Analytics" })).toBeNull();
  });

  it("shows the figures and each chart's data, and changes the period", async () => {
    serve(analyticsHandler("admin"));
    const user = userEvent.setup();
    render(<App />);
    await user.click(await screen.findByRole("button", { name: "Analytics" }));
    const now = await screen.findByRole("list", { name: "Now" });
    const figures = within(now).getAllByRole("listitem").map((item) => item.textContent);
    expect(figures).toContain("8 active today");
    expect(figures).toContain("1 banned");
    expect(calls.at(-1)?.path).toBe("/api/moderation/analytics/growth?period=30d");

    expect(tableCells("Sign-ups and finished onboardings")).toEqual(["2", "1", "7", "6"]);
    expect(tableCells("Active users")).toEqual(["3", "4", "5", "8", "9", "10"]);
    expect(tableCells("VIP passes bought")).toEqual(["0", "1"]);
    expect(figures).toContain("9 active in the last 7 days");

    await user.selectOptions(screen.getByLabelText("Period"), "12m");
    await waitFor(() => expect(calls.at(-1)?.path).toBe("/api/moderation/analytics/growth?period=12m"));
    // Weeks, with their year, across New Year.
    const signups = await screen.findByRole("region", { name: "Sign-ups and finished onboardings" });
    const weeks = within(within(signups).getByRole("table")).getAllByRole("rowheader").map((cell) => cell.textContent);
    expect(weeks[0]).toMatch(/^Week of .*2025/);
    expect(weeks[1]).toMatch(/^Week of .*2026/);
    expect(tableCells("Active users")).toEqual(["20", "20", "40", "21", "21", "41"]);
  });

  it("shows moderation figures, and opens a most-reported account and comes back", async () => {
    serve(analyticsHandler("admin"));
    const user = userEvent.setup();
    render(<App />);
    await user.click(await screen.findByRole("button", { name: "Analytics" }));
    await screen.findByRole("list", { name: "Now" });
    await user.click(screen.getByRole("button", { name: "Moderation" }));
    expect(screen.getByRole("button", { name: "Moderation" }).getAttribute("aria-pressed")).toBe("true");
    await waitFor(() => expect(calls.at(-1)?.path).toBe("/api/moderation/analytics/moderation?period=30d"));

    const now = await screen.findByRole("list", { name: "Now" });
    expect(within(now).getAllByRole("listitem").map((item) => item.textContent)).toContain("4 open reports");
    const decisions = within(screen.getByRole("list", { name: "Decisions in the period" }))
      .getAllByRole("listitem")
      .map((item) => item.textContent);
    expect(decisions).toContain("8 d 13 h median time to a decision");
    expect(decisions).toContain("17 d longest time to a decision");
    // Reasons in order: spam, harassment, sexual, under 18, threats, other.
    expect(tableCells("Reports by reason")).toEqual(["2", "0", "0", "0", "0", "0", "0", "1", "0", "0", "0", "3"]);
    // Warnings, suspensions, bans, lifted, closed, total.
    expect(tableCells("Actions by moderator")).toEqual(["2", "0", "1", "0", "0", "3", "0", "0", "0", "0", "1", "1"]);
    const moderators = screen.getByRole("region", { name: "Actions by moderator" });
    expect(within(moderators).getAllByRole("rowheader").map((cell) => cell.textContent)).toEqual([
      "Mod Max · mmmmmmm",
      "Deleted account",
    ]);

    const reported = screen.getByRole("region", { name: "Most-reported accounts" });
    // A deleted account is listed but can't be opened.
    expect(within(reported).getByText("Deleted account")).toBeTruthy();
    expect(within(reported).getAllByRole("button")).toHaveLength(1);
    await user.click(within(reported).getByRole("button", { name: /Moon Child · abcdefg/ }));
    await waitFor(() => expect(calls.at(-1)?.path).toBe("/api/moderation/accounts/42"));
    expect(screen.getByRole("button", { name: "Analytics" }).getAttribute("aria-current")).toBe("page");

    const back = vi.mocked(window.Telegram?.WebApp?.BackButton?.onClick ?? vi.fn()).mock.calls.at(-1)?.[0];
    act(() => back?.());
    await screen.findByRole("region", { name: "Most-reported accounts" });
    expect(screen.getByRole("button", { name: "Moderation" }).getAttribute("aria-pressed")).toBe("true");
  });

  it("shows revenue, refunds, and VIP conversion", async () => {
    serve(analyticsHandler("admin"));
    const user = userEvent.setup();
    render(<App />);
    await user.click(await screen.findByRole("button", { name: "Analytics" }));
    await screen.findByRole("list", { name: "Now" });
    await user.selectOptions(screen.getByLabelText("Period"), "7d");
    await user.click(screen.getByRole("button", { name: "Revenue" }));
    await waitFor(() => expect(calls.at(-1)?.path).toBe("/api/moderation/analytics/revenue?period=7d"));

    const period = await screen.findByRole("list", { name: "In the period" });
    expect(within(period).getAllByRole("listitem").map((item) => item.textContent)).toEqual([
      "⭐ 1130 Stars paid and kept",
      "⭐ 300 Stars refunded",
    ]);
    const allTime = within(screen.getByRole("list", { name: "All time" }))
      .getAllByRole("listitem")
      .map((item) => item.textContent);
    expect(allTime).toContain("7.5% of onboarded accounts have bought VIP (3 of 40)");
    expect(allTime).toContain("1 refunds due (⭐ 780)");
    expect(tableCells("Stars paid")).toEqual(["100", "780", "250", "0"]);
    expect(tableCells("Purchases")).toEqual(["1", "1", "2", "0"]);
    expect(tableCells("First-time VIP buyers")).toEqual(["1", "0"]);
    // By an admin, by Telegram, account deleted.
    expect(tableCells("Refunds")).toEqual(["0", "0", "0", "1", "2", "0"]);
    expect(tableCells("Stars refunded")).toEqual(["0", "300"]);
  });

  it("offers to try again when a dashboard can't be loaded", async () => {
    let failing = true;
    serve((path, init) =>
      path.startsWith("/api/moderation/analytics/revenue?") && failing
        ? { status: 500, body: { error: "error" } }
        : analyticsHandler("admin")(path, init),
    );
    const user = userEvent.setup();
    render(<App />);
    await user.click(await screen.findByRole("button", { name: "Analytics" }));
    await screen.findByRole("list", { name: "Now" });
    await user.click(screen.getByRole("button", { name: "Revenue" }));
    expect(await screen.findByText("The analytics couldn't be loaded.")).toBeTruthy();
    // The growth figures loaded before aren't shown for revenue.
    expect(screen.queryByRole("list", { name: "Now" })).toBeNull();
    failing = false;
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("list", { name: "All time" })).toBeTruthy();
  });
});
