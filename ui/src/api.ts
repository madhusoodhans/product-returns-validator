/* Types and I/O for the returns-check API (docs/API.md). All paths are relative: same origin as the page. */

export type Verdict =
  | "accept" | "retake" | "mismatch" | "no_damage" | "not_eligible" | "human" | "clarify" | "blocked" | "need_photo" | "need_order"
  | "need_problem" | "closed" | "off_topic" | "info" | "bad_file" | "error" | "ended" | "busy" | "rate_limited" | "unavailable";

/** What vllm-sr/auto picked for one call: the routed model, the Router decision, reasoning on/off and its effort. */
export interface RouteBrief { model?: string | null; decision?: string | null; reasoning?: "on" | "off" | string | null; effort?: string | null }
/** `route`: what the Router picked for this turn's reply in the last bench run (null for a fixed reply). */
export interface Turn { text?: string; image?: string; expect?: Verdict; route?: RouteBrief | null }
export interface Scenario {
  id: string; title: string; subtitle?: string; order?: string | null; product?: string | null; expected: Verdict; turns: Turn[];
  /** The Router switched reasoning on for a reply of this chat in the last bench run. */
  uses_reasoning?: boolean;
}
export interface Group { key: string; title: string; scenarios: Scenario[] }
export interface Policy {
  name: string; window_days: number; human_review_over?: number | null; covered: string; not_covered: string[]; photo: string;
}
export type Policies = Record<string, Policy>;
export interface Samples { groups: Group[]; policies: Policies }
/** message.order: the order the conversation is about, once its number is known. */
export interface OrderInfo { order: string; product: string; item?: string; price?: number; delivered_days_ago?: number }
export type PolicyRef = "covered" | "not_covered" | "window" | "review_limit" | "photo";
/** message.ended: null while the chat is open (docs/API.md "Chat ending"). */
export type EndReason = "approved" | "approved_and_handed_over" | "handed_over" | "not_covered" | "blocked" | "off_topic";
export interface Ended { reason: EndReason | string; title: string; text: string; order?: string | null; actions: ("review" | "new_chat" | string)[] }
export interface GalleryItem { path: string; product: string; case: string }
/** GET /api/customer: the one mock customer, logged in. Orders most recent first. */
export interface CustomerOrder {
  order: string; product: string; product_name?: string; item: string; price?: number; delivered_days_ago: number; window_days?: number;
}
export interface Customer { name: string; email?: string; greeting: string; orders: CustomerOrder[] }

export const NODES = ["guardrail", "router", "claim", "policy", "eligibility", "image_check", "decision", "reply"] as const;
export type NodeKey = (typeof NODES)[number];
export type NodeStatus = "waiting" | "running" | "done" | "skipped" | "blocked" | "failed";

export interface ChoiceQ {
  key: string; label: string; type: "choice"; choice: string; probs: Record<string, number>;
  /** Option id -> display name (order matching: order id -> item name). */
  labels?: Record<string, string>;
}
export interface YesNoQ {
  key: string; label: string; type: "yesno"; prob: number; threshold: number; borderline_from?: number;
  status: "ok" | "borderline" | "bad";
}
export type Question = ChoiceQ | YesNoQ;
export interface Escalation { key: string; model: string; prob: number; ms?: number }
export interface D3Detail { model?: string; questions: Question[]; escalated?: Escalation[]; ms?: number }

/** Guardrail: Vela 3 flash scores (prompt attack, harm; null when Vela was unavailable) plus a D3 scope check (choice
 * `scope` and `intent` with `labels`, yes/no `command`, `complaint`). */
export interface GuardrailDetail {
  attack?: number | null; harm?: number | null; vela_flagged?: boolean; vela_ms?: number | null;
  thresholds?: { attack?: number; harm?: number }; flagged?: boolean; complaint?: number; model?: string; scope_check?: D3Detail | null;
}
/** What vllm-sr/auto picked for the reply call (router step `done`); `extraction`: the same for the claim-extraction call. */
export interface RouteDetail extends RouteBrief { signals?: string | null; ranking?: string | null; routing_ms?: number | null }
export interface RouterDetail extends RouteDetail { call?: string; extraction?: RouteDetail | null }
/** D3 order matching on the claim step (no order number in the message): one choice question `order`. `ms` only if the server times it. */
export interface OrderMatch extends D3Detail { threshold?: number; ms?: number }
export interface ClaimDetail {
  order?: string | null; product?: string | null; issue?: string | null; part?: string | null; order_match?: OrderMatch | null;
  /** The Router's choice for the claim-extraction call. */
  route?: RouteDetail | null;
}
export interface PolicyDetail { rules: { label: string; ok: boolean }[] }
export interface DecisionDetail { verdict: Verdict; reasons?: string[]; rule?: string }
/** D3 check of the worded reply before it is streamed; a reply that claims an approval not given is replaced. */
export interface ReplyCheck { model?: string; claims_approval: number; threshold: number; replaced: boolean; ms?: number; original?: string }
/** `model`: the model the Router picked. `standard`: the language model was unavailable, so a fixed template words the
 * decision (`reason` says why). `tokens` include reasoning tokens. */
export interface ReplyDetail {
  model?: string | null; tokens?: number; route?: RouteDetail | null; reply_check?: ReplyCheck | null; standard?: boolean; reason?: string;
}

export interface StepEvent {
  type: "step"; node: NodeKey; status: NodeStatus; model?: string; ms?: number; summary?: string; detail?: unknown;
}

/** Stats of one chat session (GET /api/stats?session=<id> and the `stats` SSE event). */
export interface Stats {
  session?: string; turns?: number; d3_calls?: number; d3_ms_median?: number | null;
  /** Replies the language model wrote, by Router decision, and how many of them ran with reasoning on. */
  llm_turns?: number; routes?: Record<string, number>; reasoning_turns?: number;
  blocked?: number; off_topic?: number; image_checks_avoided?: number;
  /** This chat's model time in ms (decision: D3 and Vela); null before its first turn. */
  model_ms?: { decision: number; llm: number; total: number } | null;
  /** Completion tokens the language model wrote in this chat, reasoning tokens included; null before its first turn. */
  llm_tokens?: number | null;
  d3_tokens?: number;
}

export type ChatEvent =
  | StepEvent
  | { type: "token"; text: string }
  | { type: "message"; role: "assistant"; text: string; verdict?: Verdict; order?: OrderInfo | null; policy_ref?: PolicyRef | null; ended?: Ended | null }
  | ({ type: "stats" } & Stats)
  | { type: "done"; turn: string }
  /** The server no longer knows this chat (restarted or dropped it): start a new one. */
  | { type: "expired"; message: string }
  | { type: "error"; message: string };

const JSON_TIMEOUT_MS = 8000;

async function getJson<T>(path: string): Promise<T> {
  let r: Response;
  try {
    r = await fetch(path, { cache: "no-store", signal: AbortSignal.timeout(JSON_TIMEOUT_MS) });
  } catch (e) {
    if ((e as Error)?.name === "TimeoutError") throw new Error(`The demo server did not answer within ${JSON_TIMEOUT_MS / 1000} s`);
    if (e instanceof TypeError) throw new Error("Cannot reach the demo server");
    throw e;
  }
  const text = await r.text();
  let data: unknown = text;
  try { data = JSON.parse(text); } catch { /* not JSON */ }
  if (!r.ok) throw new Error((data as { error?: string; detail?: string })?.error || (data as { detail?: string })?.detail || `${r.status} ${r.statusText}`);
  return data as T;
}

export const getSamples = () =>
  getJson<Partial<Samples>>("api/samples").then((d): Samples => ({ groups: d.groups ?? [], policies: d.policies ?? {} }));
export const getCustomer = () => getJson<Customer>("api/customer");
/** GET /api/health: which models are up, and the top bar's plain-words status (docs/API.md "Model availability"). */
export type HealthStatus = "ok" | "checking" | "guard_down" | "standard" | "unavailable";
export interface Health {
  ok: boolean; status: HealthStatus | string; message: string;
  d3?: boolean; guard?: boolean; llm?: boolean;
  models?: Record<string, { state: "up" | "down" | "unknown"; reason?: string | null; retry_in_s?: number | null }>;
}
export const getHealth = () => getJson<Health>("api/health");
/** GET /api/config: `play_all` false hides Play all (hosted copy); `landing_url` is the page of all demos ("" hides the link). */
export type Config = { play_all?: boolean; landing_url?: string };
export const getConfig = () => getJson<Config>("api/config");
export const getGallery = () => getJson<GalleryItem[]>("api/gallery");
export const getStats = (session: string) => getJson<Stats>(`api/stats?session=${encodeURIComponent(session)}`);
/** The contract says "the stored step events of a past turn"; accept `{steps: [...]}`, `{events: [...]}` or a bare array. */
export const getTurn = (turn: string) =>
  getJson<StepEvent[] | { steps?: StepEvent[]; events?: StepEvent[] }>(`api/turn/${encodeURIComponent(turn)}`).then((d) =>
    (Array.isArray(d) ? d : d.steps ?? d.events ?? []).filter((e) => e && e.type === "step"),
  );

/** POST /api/chat and feed each SSE event to onEvent. Resolves when the stream ends. */
export async function streamChat(
  /** `has_turns`: this chat already had a turn, so an unknown session is an expired chat, not a new one. */
  body: { session: string; text?: string; image?: string; action?: "review"; has_turns?: boolean },
  onEvent: (e: ChatEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const r = await fetch("api/chat", {
    method: "POST", body: JSON.stringify(body), headers: { "Content-Type": "application/json" }, signal, cache: "no-store",
  });
  if (!r.ok || !r.body) {
    const text = await r.text().catch(() => "");
    let detail = text;
    try { const j = JSON.parse(text); detail = j.error || j.detail || text; } catch { /* plain text */ }
    throw new Error(detail || `${r.status} ${r.statusText}`);
  }
  const reader = r.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  const flush = (block: string) => {
    let data = "";
    for (const line of block.split(/\r?\n/)) if (line.startsWith("data:")) data += (data ? "\n" : "") + line.slice(5).replace(/^ /, "");
    if (!data) return;
    try { onEvent(JSON.parse(data) as ChatEvent); } catch { /* keep-alive or partial */ }
  };
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let cut: RegExpExecArray | null;
    while ((cut = /\r?\n\r?\n/.exec(buffer))) {
      flush(buffer.slice(0, cut.index));
      buffer = buffer.slice(cut.index + cut[0].length);
    }
  }
  if (buffer.trim()) flush(buffer);
}

export const errText = (e: unknown) => String((e as Error)?.message || e);
/** A random UUID (the server accepts only UUID-shaped session ids); randomUUID needs a secure context. */
export const newSession = () =>
  typeof crypto !== "undefined" && "randomUUID" in crypto ? crypto.randomUUID()
    : "10000000-1000-4000-8000-100000000000".replace(/[018]/g, (c) => (+c ^ (Math.random() * 16) >> (+c / 4)).toString(16));
/** Sample paths are relative ("samples/phone/x.jpg"); data URLs pass through. */
export const imageUrl = (p: string) => (p.startsWith("data:") || p.startsWith("blob:") || /^https?:/.test(p) ? p : p.replace(/^\/+/, ""));
