import type {
  ConversationPage,
  InstagramAttempt,
  InstagramChooseResult,
  InstagramSettings,
  Status,
  Transcript,
} from "./types";

// Calls to the backend's /shopify/admin/* routes.
//
// Plain fetch() on purpose: inside Shopify admin, App Bridge (loaded first in
// index.html) adds the session token as the Authorization header to fetch
// requests to the app's own origin. The backend verifies it
// (shopify_session.verify_session_token).
//
// Paths are relative to the page (served at /shopify/app), so "admin/status"
// resolves to /shopify/admin/status.

/** 202: the backend is still installing this store — try again shortly. */
export class SetupInProgress extends Error {}

/** Any other non-2xx answer. The message is the backend's `error`, if any. */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

function resolve(path: string): string {
  return new URL(path, window.location.href).toString();
}

async function call<T>(path: string, method: "GET" | "POST", body?: unknown): Promise<T> {
  const res = await fetch(resolve(path), {
    method,
    headers:
      body === undefined
        ? { Accept: "application/json" }
        : { Accept: "application/json", "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (res.status === 202) throw new SetupInProgress("setup in progress");
  if (!res.ok) {
    let message = `HTTP ${res.status}`;
    try {
      const body = await res.json();
      if (body?.error) message = String(body.error);
    } catch {
      // not JSON — keep the HTTP status
    }
    throw new ApiError(res.status, message);
  }
  return (await res.json()) as T;
}

export const fetchStatus = () => call<Status>("admin/status", "GET");
export const retrySetup = () => call<Status>("admin/retry", "POST");
export const disconnectInstagram = () =>
  call<Status>("admin/instagram/disconnect", "POST");

/** Save Instagram switches; returns the fresh status. */
export const saveInstagramSettings = (changes: Partial<InstagramSettings>) =>
  call<Status>("admin/instagram/settings", "POST", changes);

/** Where a connect started from this page stands (the pop-up writes its outcome there). */
export const fetchInstagramAttempt = (id: string) =>
  call<InstagramAttempt>(`admin/instagram/attempt?id=${encodeURIComponent(id)}`, "GET");

/** Connect the account chosen from a "choose" outcome. */
export const chooseInstagramAccount = (attempt: string, account: string) =>
  call<InstagramChooseResult>("admin/instagram/choose", "POST", { attempt, account });

/** One page of recent conversations; `before` is the cursor of the last row shown. */
export const fetchConversations = (before?: string) => {
  const q = new URLSearchParams({ limit: "25" });
  if (before) q.set("before", before);
  return call<ConversationPage>(`admin/conversations?${q.toString()}`, "GET");
};

export const fetchTranscript = (id: string) =>
  call<Transcript>(`admin/conversations/${encodeURIComponent(id)}`, "GET");
