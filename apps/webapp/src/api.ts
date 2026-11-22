// Calls to the moderation API. Every request carries Telegram's signed launch data,
// which the server checks each time (D-071).

export interface Me {
  role: "moderator" | "admin";
  nickname: string | null;
}

export interface QueueItem {
  id: number;
  reason: string;
  urgent: boolean;
  kind: "chat" | "profile";
  created_at: string;
  has_details: boolean;
  reported_nickname: string | null;
}

export interface Person {
  public_id: string | null;
  nickname: string | null;
  age: number | null;
  gender: string | null;
  country_code: string | null;
  city: string | null;
  bio: string | null;
  has_photo: boolean;
  status: string;
  deleted: boolean;
}

export interface PastAction {
  kind: string;
  rule: string | null;
  days: number | null;
  ends_at: string | null;
  created_at: string;
}

export interface Item {
  source: "chat" | "direct";
  id: number;
  /** "reporter" or "reported" in a report; "account" or "partner" in an account's
   * thread. */
  sender: "reporter" | "reported" | "account" | "partner";
  kind: string;
  text: string | null;
  edited_text: string | null;
  edited_at: string | null;
  private: boolean;
  view_once: boolean;
  viewable: boolean;
  sent_at: string;
}

export interface RequestItem {
  sender: "reporter" | "reported";
  status: string;
  created_at: string;
}

export interface Review {
  id: number;
  kind: "chat" | "profile";
  reason: string;
  urgent: boolean;
  details: string | null;
  status: string;
  outcome: string | null;
  created_at: string;
  reporter: Person;
  reported: Person;
  record: { actions: PastAction[]; reports_received: number; reports_open: number };
  messages: Item[];
  requests: RequestItem[];
}

/** Actions on a report: lifting isn't offered there. */
export type ReportActionKind = "close" | "warn" | "suspend" | "ban";
/** Actions on a looked-up account: closing needs a report, so it isn't offered. */
export type AccountActionKind = "warn" | "suspend" | "ban" | "lift";
export type ActionKind = ReportActionKind | AccountActionKind;

export interface ActionBody<K extends ActionKind = ActionKind> {
  kind: K;
  rule?: string;
  days?: number;
}

/** Media from a report. */
export interface MediaBody {
  source: "chat" | "direct" | "photo";
  item_id?: number;
}

/** Media from an account's threads, which an admin opens with a reason. */
export interface AccountMediaBody {
  source: "chat" | "direct";
  item_id: number;
  reason: string;
}

export interface Match {
  id: number;
  public_id: string | null;
  nickname: string | null;
  status: string;
}

export interface ReportSummary {
  id: number;
  reason: string;
  kind: "chat" | "profile";
  status: string;
  outcome: string | null;
  created_at: string;
}

export interface Account {
  id: number;
  person: Person;
  suspended_until: string | null;
  record: Review["record"];
  reports_received: ReportSummary[];
  reports_made: ReportSummary[];
}

export interface Thread {
  kind: "chat" | "direct";
  ref: number;
  partner_nickname: string | null;
  first_at: string;
  last_at: string;
  messages: number;
}

export interface ReasonBody {
  reason: string;
}

export interface ThreadBody extends ReasonBody {
  kind: "chat" | "direct";
  ref: number;
}

/** Why a request failed: "reopen" when the launch data is missing or too old, "denied"
 * when the account may not use the panel, "conflict" when the action no longer applies
 * (with the server's reason in `code`), "notFound", "invalid" when the server refused
 * the request's content (422, e.g. a missing rule or a too-short reason), "telegram"
 * when Telegram refused, and "error" for anything else. */
export type Failure = "reopen" | "denied" | "conflict" | "notFound" | "invalid" | "telegram" | "error";

export class ApiError extends Error {
  readonly failure: Failure;
  readonly code: string | undefined;

  constructor(failure: Failure, code?: string) {
    super(failure);
    this.failure = failure;
    this.code = code;
  }
}

type Body = ActionBody | MediaBody | AccountMediaBody | ReasonBody | ThreadBody;

async function request<T>(path: string, launchData: string, body?: Body): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, {
      method: body === undefined ? "GET" : "POST",
      headers: {
        Authorization: `tma ${launchData}`,
        ...(body === undefined ? {} : { "Content-Type": "application/json" }),
      },
      body: body === undefined ? undefined : JSON.stringify(body),
      credentials: "omit",
      cache: "no-store",
    });
  } catch {
    throw new ApiError("error");
  }
  if (response.ok) return (await response.json()) as T;
  if (response.status === 401) throw new ApiError("reopen");
  if (response.status === 403) throw new ApiError("denied");
  if (response.status === 404) throw new ApiError("notFound");
  if (response.status === 409) {
    const data = (await response.json().catch(() => ({}))) as { error?: string };
    throw new ApiError("conflict", data.error);
  }
  if (response.status === 422) throw new ApiError("invalid");
  if (response.status === 502) throw new ApiError("telegram");
  throw new ApiError("error");
}

export function getJson<T>(path: string, launchData: string): Promise<T> {
  return request<T>(path, launchData);
}

export function postJson<T>(path: string, launchData: string, body: Body): Promise<T> {
  return request<T>(path, launchData, body);
}
