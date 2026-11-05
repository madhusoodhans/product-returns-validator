/* The visitor's vllm-sr API key in the browser (docs/API.md "API key").
 * - Kept in sessionStorage only (gone when the tab closes); never in localStorage, cookies or a URL sent to a server.
 * - The landing page hands it over as #key=<key> (a fragment is never sent to a server): read on load, removed from the
 *   address bar at once, saved here, then POSTed to /api/key.
 * - Every request carries X-Visitor-Id (a per-tab UUID): the server keeps the key for this visitor only. */

const KEY = "vsr-key";
const VISITOR = "vsr-visitor";

const ss = {
  get(k: string): string | null { try { return sessionStorage.getItem(k); } catch { return null; } },
  set(k: string, v: string) { try { sessionStorage.setItem(k, v); } catch { /* storage blocked: the key lives on the server only */ } },
  del(k: string) { try { sessionStorage.removeItem(k); } catch { /* nothing stored */ } },
};

const uuid = () =>
  typeof crypto !== "undefined" && "randomUUID" in crypto ? crypto.randomUUID()
    : "10000000-1000-4000-8000-100000000000".replace(/[018]/g, (c) => (+c ^ (Math.random() * 16) >> (+c / 4)).toString(16));

let visitor = ss.get(VISITOR);
if (!visitor || !/^[0-9a-f-]{36}$/.test(visitor)) { visitor = uuid(); ss.set(VISITOR, visitor); }
export const visitorId = () => visitor as string;

/** Runs once at module load, before the first render: take #key= from the landing page and drop it from the URL. */
function takeFragmentKey(): string | null {
  const m = /(?:^#|&)key=([^&]*)/.exec(location.hash);
  if (!m) return null;
  let k = "";
  try { k = decodeURIComponent(m[1]).trim(); } catch { k = ""; }
  const rest = location.hash.slice(1).split("&").filter((p) => !p.startsWith("key=")).join("&");
  try { history.replaceState(history.state, "", location.pathname + location.search + (rest ? `#${rest}` : "")); } catch { location.hash = rest; }
  if (k) ss.set(KEY, k);
  return k || null;
}
export const handedOver = takeFragmentKey() !== null;

export const storedKey = () => ss.get(KEY);
export const storeKey = (k: string) => ss.set(KEY, k.trim());
export const forgetStoredKey = () => ss.del(KEY);

export type KeyCode = "format" | "wrong_kind" | "invalid" | "disabled" | "quota" | "rate_limited" | "unreachable" | "error" | "no_key";
export interface KeyResult { ok: boolean; code?: KeyCode | string; message?: string }
export interface KeyState { set: boolean; server_key?: boolean; failed?: string | null }

/** Messages for each code (the server sends its own; these cover a request that never reached it). */
export const KEY_TEXT: Record<string, string> = {
  format: "That does not look like a vllm-sr API key. Keys from app.vllm-sr.ai start with sk-vsr-.",
  wrong_kind: "This demo needs a key from app.vllm-sr.ai: sk-pt keys only work for chat.",
  invalid: "This API key was not accepted. Check that you copied all of it, or get a new one.",
  disabled: "This API key is disabled or cannot use these models.",
  quota: "This API key has used its daily quota. It resets at 00:00 UTC.",
  rate_limited: "Too many requests for this key right now. Try again in a minute.",
  unreachable: "The vllm-sr API cannot be reached right now. Try again in a minute.",
  error: "The key could not be checked. Try again in a minute.",
  no_key: "Enter your vllm-sr API key to use the demo.",
};
export const keyText = (r: { code?: string; message?: string }) => r.message || KEY_TEXT[r.code ?? "error"] || KEY_TEXT.error;
export const GET_KEY_URL = "https://app.vllm-sr.ai/#key";

const headers = () => ({ "Content-Type": "application/json", "X-Visitor-Id": visitorId() });

/** POST /api/key: validate and keep it for this visitor (server memory). */
export async function sendKey(key: string): Promise<KeyResult> {
  try {
    const r = await fetch("api/key", { method: "POST", headers: headers(), body: JSON.stringify({ key: key.trim() }), cache: "no-store", signal: AbortSignal.timeout(70_000) });
    const j = (await r.json().catch(() => ({}))) as KeyResult;
    return r.ok && j.ok ? { ok: true } : { ok: false, code: j.code ?? "error", message: keyText(j) };
  } catch {
    return { ok: false, code: "unreachable", message: "Cannot reach the demo server." };
  }
}

export async function getKeyState(): Promise<KeyState> {
  const r = await fetch("api/key", { headers: headers(), cache: "no-store", signal: AbortSignal.timeout(8000) });
  if (!r.ok) throw new Error(`${r.status}`);
  return (await r.json()) as KeyState;
}

export async function deleteKey(): Promise<void> {
  forgetStoredKey();
  await fetch("api/key", { method: "DELETE", headers: headers(), cache: "no-store" }).catch(() => undefined);
}
