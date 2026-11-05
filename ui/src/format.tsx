/* Labels, tones and icons shared by the panels. */
import type { LucideIcon } from "lucide-react";
import {
  Ban, Camera, CloudOff, CircleAlert, CircleCheck, CircleDot, CircleHelp, CircleSlash, EyeOff, FileX, Hash, Hourglass, Info, Lock, MessageCircleQuestion, MessageSquareOff, RefreshCw, Shuffle, ShieldAlert, UserRound,
} from "lucide-react";
import type { BadgeTone } from "./components/arc/badge/badge";
import type { NodeKey, Verdict } from "./api";

export interface VerdictMeta { label: string; short: string; tone: BadgeTone; icon: LucideIcon }
export const VERDICT: Record<Verdict, VerdictMeta> = {
  accept: { label: "Accepted", short: "Accept", tone: "success", icon: CircleCheck },
  retake: { label: "Retake photo", short: "Retake", tone: "warning", icon: RefreshCw },
  mismatch: { label: "Wrong product", short: "Mismatch", tone: "warning", icon: Shuffle },
  no_damage: { label: "No damage visible", short: "No damage", tone: "neutral", icon: EyeOff },
  not_eligible: { label: "Not eligible", short: "Not eligible", tone: "danger", icon: Ban },
  human: { label: "Human review", short: "Human", tone: "info", icon: UserRound },
  clarify: { label: "Clarifying question", short: "Clarify", tone: "info", icon: CircleHelp },
  blocked: { label: "Blocked by guardrail", short: "Blocked", tone: "danger", icon: ShieldAlert },
  need_photo: { label: "Photo requested", short: "Need photo", tone: "neutral", icon: Camera },
  need_order: { label: "Order number requested", short: "Need order", tone: "neutral", icon: Hash },
  need_problem: { label: "Problem requested", short: "Need problem", tone: "neutral", icon: MessageCircleQuestion },
  closed: { label: "Already decided", short: "Closed", tone: "neutral", icon: Lock },
  off_topic: { label: "Off-topic", short: "Off-topic", tone: "info", icon: CircleSlash },
  info: { label: "Answered", short: "Answered", tone: "info", icon: Info },
  bad_file: { label: "Not a photo file", short: "Bad file", tone: "warning", icon: FileX },
  ended: { label: "Chat ended", short: "Ended", tone: "neutral", icon: MessageSquareOff },
  error: { label: "Error", short: "Error", tone: "danger", icon: CircleAlert },
  busy: { label: "Service busy", short: "Busy", tone: "warning", icon: Hourglass },
  rate_limited: { label: "Too many messages", short: "Slow down", tone: "warning", icon: Hourglass },
  unavailable: { label: "Assistant unavailable", short: "Unavailable", tone: "danger", icon: CloudOff },
};
/** Known verdicts get their entry; an unknown one gets a neutral chip with its own name, so a new server verdict never breaks the page. */
export const verdictMeta = (v: string | undefined | null): VerdictMeta | null =>
  !v ? null : Object.prototype.hasOwnProperty.call(VERDICT, v) ? VERDICT[v as Verdict]
    : { label: titleCase(String(v)), short: titleCase(String(v)), tone: "neutral", icon: CircleDot };

export const NODE_LABEL: Record<NodeKey, string> = {
  guardrail: "Guardrail", router: "Router", claim: "Claim", policy: "Policy rules",
  eligibility: "Eligibility", image_check: "Photo check", decision: "Decision", reply: "Reply",
};
/** What each node is, shown under the name while it waits. */
export const NODE_HINT: Record<NodeKey, string> = {
  guardrail: "Vela 3 screens for prompt attacks and harm; D3 checks the message is about orders",
  router: "vllm-sr/auto picks the model and the reasoning effort for the reply",
  claim: "Order, item, issue and part as JSON",
  policy: "Return window, delivery and price limit, in code",
  eligibility: "D3 text decision over the policy and complaint",
  image_check: "D3 image decision: quality, product, damage",
  decision: "Rules in code turn D3 answers into a verdict",
  reply: "The LLM words the verdict; it cannot change it",
};
export const D3_NODES: NodeKey[] = ["eligibility", "image_check"];

/** The router's honest description, for its tooltip and detail. */
export const ROUTER_NOTE = "The Router raises the reasoning effort for upset, urgent or complex turns; normal turns run with reasoning off. "
  + "It does not switch to a larger model: the model can differ per call, and this shows which one answered.";

const MODEL_NAME: Record<string, string> = {
  "vllm-sr/vela3-flash": "Vela 3 flash", "vllm-sr/d3-flash": "D3 flash", "vllm-sr/auto": "vllm-sr/auto",
};
/** A routed model id as given, lightly prettified: "qwen/qwen3.8-27b" -> "Qwen3.8 27B", "qwen/qwen3.8-flash-next" -> "Qwen3.8 Flash Next". */
export function routedName(id: string | null | undefined): string {
  if (!id) return "";
  const base = id.replace(/^[^/]+\//, "");
  const m = base.match(/^([a-z]+)(\d[\w.]*)-(.+)$/i);
  if (!m) return base;
  const fam = m[1].charAt(0).toUpperCase() + m[1].slice(1);
  const rest = m[3].split("-").map((w) => (/^\d+(\.\d+)?[bm]$/i.test(w) ? w.toUpperCase() : w.charAt(0).toUpperCase() + w.slice(1))).join(" ");
  return `${fam}${m[2]} ${rest}`;
}
const oneModel = (m: string) => MODEL_NAME[m] || (m.startsWith("vllm-sr/") ? m.replace(/^vllm-sr\//, "") : routedName(m));
/** "vllm-sr/auto + vllm-sr/d3-flash" -> "vllm-sr/auto + D3 flash". */
export const modelName = (m: string | undefined | null) => (m ? m.split(/\s*\+\s*/).map(oneModel).join(" + ") : "");

/** "effort_high" -> "high"; null when not reported or off. */
export const effortWord = (e: string | null | undefined) => {
  const w = (e ?? "").match(/effort_([a-z]+)/i)?.[1] ?? (/^[a-z]+$/i.test(e ?? "") ? e! : "");
  return w && w.toLowerCase() !== "off" ? w.toLowerCase() : null;
};
/** Reasoning in words: "reasoning off", "reasoning on (high)", "reasoning not reported". */
export const reasoningWords = (r: { reasoning?: string | null; effort?: string | null }) =>
  r.reasoning === "off" ? "reasoning off" : r.reasoning === "on" ? `reasoning on${effortWord(r.effort) ? ` (${effortWord(r.effort)})` : ""}` : "reasoning not reported";
/** A compact tag for a chat reply: "fast · reasoning off". */
export const routeTag = (r: { decision?: string | null; reasoning?: string | null; effort?: string | null }) =>
  `${r.decision || "routed"} · ${r.reasoning === "on" ? "reasoning on" : r.reasoning === "off" ? "reasoning off" : "reasoning not reported"}`;

export const DASH = "–";
export const fmtMs = (ms: number | undefined | null) => (ms == null ? DASH : ms >= 1000 ? `${(ms / 1000).toFixed(1)} s` : `${Math.round(ms)} ms`);
export const fmtInt = (n: number | undefined | null) => (n == null ? DASH : Math.round(n).toLocaleString("en-US"));
export const fmtPct = (x: number | undefined | null) => (x == null ? DASH : `${Math.round(x * 100)}%`);
export const fmtCompact = (n: number) => (n >= 10000 ? `${(n / 1000).toFixed(n >= 100000 ? 0 : 1)}k` : n.toLocaleString("en-US"));
export const titleCase = (s: string) => s.charAt(0).toUpperCase() + s.slice(1).replace(/_/g, " ");
