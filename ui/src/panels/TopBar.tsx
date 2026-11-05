/* Link back to the page of all demos, title, the stats of the current chat session (`stats` events / GET /api/stats?session=), the theme switch.
 * One row of stat blocks spread over the bar in two groups: "This chat" counts and "Model use" (time and tokens, each a
 * number with its unit).
 * Missing values render as a dash; a new chat starts from an empty bar. */
import type { ReactNode } from "react";
import { ArrowLeft, KeyRound } from "lucide-react";
import { ThemeSwitch, type Theme } from "../components/arc/theme-switch/theme-switch";
import type { Health, Stats } from "../api";
import { DASH, fmtInt } from "../format";
import styles from "./TopBar.module.css";

function Stat({ label, hint, wide, children }: { label: string; hint?: string; wide?: boolean; children: ReactNode }) {
  return (
    <dl className={styles.item} title={hint} data-wide={wide || undefined}>
      <dt>{label}</dt>
      <dd>{children}</dd>
    </dl>
  );
}

/** A duration as a number and a smaller unit: ms under a second, else seconds. */
function Dur({ ms }: { ms: number | null | undefined }) {
  if (ms == null) return <>{DASH}</>;
  return ms < 1000 ? <>{Math.round(ms)}<span className={styles.unit}>ms</span></> : <>{(ms / 1000).toFixed(ms < 10000 ? 2 : 1)}<span className={styles.unit}>s</span></>;
}

const HEALTH_MODEL: Record<string, string> = { d3: "D3 flash", guard: "Vela 3 flash", llm: "vllm-sr/auto (language model)" };

/** Which models are up, in plain words (GET /api/health): all working, D3 checking alone, standard replies, or unavailable. */
const HEALTH_TEXT: Record<string, string> = {
  ok: "All systems working", checking: "Checking systems", guard_down: "Vela guardrail unavailable: D3 checks messages alone",
  standard: "Standard replies: language model unavailable", unavailable: "Assistant unavailable",
};

function HealthBadge({ health }: { health: Health | null }) {
  const tone = !health ? "unknown" : health.status === "ok" ? "ok" : health.status === "unavailable" ? "bad"
    : health.status === "checking" ? "unknown" : "warn";
  const text = (health && HEALTH_TEXT[health.status]) ?? health?.message ?? "Status unknown";
  const down = Object.entries(health?.models ?? {}).filter(([, m]) => m.state === "down").map(([k]) => HEALTH_MODEL[k] ?? k);
  return (
    <span className={styles.health} data-tone={tone} role="status" aria-live="polite"
      title={down.length ? `Unavailable right now: ${down.join(", ")}` : "D3 flash, Vela 3 flash and the language model behind vllm-sr/auto answer"}>
      <span className={styles.healthDot} aria-hidden="true" />{text}
    </span>
  );
}

/** The landing page is another origin, so its localStorage theme is not shared: hand the theme over as ?theme=. */
function backHref(landingUrl: string, theme: Theme) {
  try {
    const u = new URL(landingUrl, location.href);
    u.searchParams.set("theme", theme);
    return u.toString();
  } catch { return landingUrl; }
}

export function TopBar({ stats, health, theme, onTheme, landingUrl, keyStatus, onChangeKey }: {
  stats: Stats | null; health: Health | null; theme: Theme; onTheme: (t: Theme) => void; landingUrl?: string;
  /** The visitor's API key: "Change key" once one works, "Enter API key" before. */
  keyStatus: "checking" | "ready" | "none"; onChangeKey: () => void;
}) {
  const s = stats ?? {};
  const m = s.model_ms ?? null;
  return (
    <header className={styles.bar}>
      <div className={styles.brand}>
        {landingUrl && (
          <a className={styles.back} href={backHref(landingUrl, theme)} aria-label="Back to all demos" title="All demos">
            <ArrowLeft size={16} aria-hidden="true" /><span className={styles.backText}>All demos</span>
          </a>
        )}
        <h1 title="D3 decides; the language model picked by vllm-sr/auto words the reply">Returns validator Demo</h1>
      </div>

      <div className={styles.stats} role="group" aria-label="Stats of this chat">
        <span className={styles.caption} data-group="chat">This chat</span>
        <span className={styles.caption} data-group="model">Model use</span>
        <span className={styles.divider} aria-hidden="true" />

        <Stat label="D3 calls" hint="D3 calls in this chat and their median time">
          {fmtInt(s.d3_calls)}
          {s.d3_ms_median != null && <span className={styles.unit}>{Math.round(s.d3_ms_median)} ms<span className={styles.long}> median</span></span>}
        </Stat>
        <Stat label="Reasoning on" hint="Replies where the Router switched reasoning on, of the replies the language model wrote in this chat">
          {s.llm_turns == null || !s.turns ? DASH : <>{fmtInt(s.reasoning_turns ?? 0)}<span className={styles.vs}>/</span>{fmtInt(s.llm_turns)}<span className={styles.unit}>{s.llm_turns === 1 ? "reply" : "replies"}</span></>}
        </Stat>
        <Stat label="Blocked / off-topic" hint="Turns stopped by the guardrail, and off-topic turns, in this chat; both get a fixed reply with no model call">
          {s.blocked == null && s.off_topic == null ? DASH : <>{fmtInt(s.blocked ?? 0)}<span className={styles.vs}>/</span>{fmtInt(s.off_topic ?? 0)}</>}
        </Stat>
        <Stat label="Image checks avoided" hint="1 when this chat was settled without a photo check (not eligible or human review)">
          {fmtInt(s.image_checks_avoided)}
        </Stat>

        <Stat label="Decision time" hint="D3 flash and Vela 3 flash calls in this chat" wide><Dur ms={m?.decision} /></Stat>
        <Stat label="LLM time" hint="Every vllm-sr/auto call in this chat (claim reading and replies)" wide><Dur ms={m?.llm} /></Stat>
        <Stat label="Total model time" hint="All model calls in this chat: Vela, D3 and the language model" wide><Dur ms={m?.total} /></Stat>
        <Stat label="LLM tokens generated" hint={`Tokens the language model wrote in this chat, reasoning tokens included. D3 and Vela write none; they read ${fmtInt(s.d3_tokens ?? 0)}, mostly image tokens.`} wide>
          {s.llm_tokens == null ? DASH : <>{fmtInt(s.llm_tokens)}<span className={styles.unit}>tokens</span></>}
        </Stat>
      </div>

      <div className={styles.actions}>
        <HealthBadge health={health} />
        <button type="button" className={styles.keyBtn} onClick={onChangeKey} data-missing={keyStatus === "none" || undefined}
          title={keyStatus === "ready" ? "Use another vllm-sr API key" : "Enter your vllm-sr API key"}>
          <KeyRound size={14} strokeWidth={1.9} aria-hidden="true" />{keyStatus === "ready" ? "Change key" : keyStatus === "checking" ? "Checking key" : "Enter API key"}
        </button>
        <ThemeSwitch theme={theme} iconOnly variant="rise" onThemeChange={(next) => onTheme(next)} />
      </div>
    </header>
  );
}
