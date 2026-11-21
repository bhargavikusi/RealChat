// Calls to the moderation API. Every request carries Telegram's signed launch data,
// which the server checks each time (D-071).

export interface Me {
  role: "moderator" | "admin";
  nickname: string | null;
}

/** Why a request failed: "reopen" when the launch data is missing or too old, "denied"
 * when the account may not use the panel, "error" for anything else. */
export type Failure = "reopen" | "denied" | "error";

export class ApiError extends Error {
  readonly failure: Failure;

  constructor(failure: Failure) {
    super(failure);
    this.failure = failure;
  }
}

export async function getJson<T>(path: string, launchData: string): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, {
      headers: { Authorization: `tma ${launchData}` },
      credentials: "omit",
      cache: "no-store",
    });
  } catch {
    throw new ApiError("error");
  }
  if (response.status === 401) throw new ApiError("reopen");
  if (response.status === 403) throw new ApiError("denied");
  if (!response.ok) throw new ApiError("error");
  return (await response.json()) as T;
}
