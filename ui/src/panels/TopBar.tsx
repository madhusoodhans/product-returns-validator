/* Link back to the page of all demos, title, the stats of the current chat session (`stats` events / GET /api/stats?session=), the theme switch.
 * One row of stat blocks spread over the bar in two groups: "This chat" counts and "Model use" (time and tokens, each a
 * number with its unit).
 * Missing values render as a dash; a new chat starts from an empty bar. */
import type { ReactNode } from "react";
import { ArrowLeft } from "lucide-react";
import { ThemeSwitch, type Theme } from "../components/arc/theme-switch/theme-switch";
import type { Health, Stats } from "../api";
import { DASH, fmtInt, fmtPct } from "../format";
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

/** Which models are up, in plain words (GET /api/health): all working, a fallback in use, or unavailable. */
function HealthBadge({ health }: { health: Health | null }) {
  const tone = !health ? "unknown" : health.status === "ok" ? "ok" : health.status === "unavailable" ? "bad"
    : health.status === "checking" ? "unknown" : "warn";
  const text = health?.message ?? "Status unknown";
  const down = Object.entries(health?.models ?? {}).filter(([, m]) => m.state === "down").map(([k]) => k);
  return (
    <span className={styles.health} data-tone={tone} role="status" aria-live="polite"
      title={down.length ? `Unavailable right now: ${down.join(", ")}` : "D3, the routing preview and both Qwen models answer"}>
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

export function TopBar({ stats, health, theme, onTheme, landingUrl }: { stats: Stats | null; health: Health | null; theme: Theme; onTheme: (t: Theme) => void; landingUrl?: string }) {
  const s = stats ?? {};
  const share = s.share ?? null;
  const m = s.model_ms ?? null;
  return (
    <header className={styles.bar}>
      <div className={styles.brand}>
        {landingUrl && (
          <a className={styles.back} href={backHref(landingUrl, theme)} aria-label="Back to all demos" title="All demos">
            <ArrowLeft size={16} aria-hidden="true" /><span className={styles.backText}>All demos</span>
          </a>
        )}
        <h1 title="D3 decides, Qwen chats">Returns validator Demo</h1>
      </div>

      <div className={styles.stats} role="group" aria-label="Stats of this chat">
        <span className={styles.caption} data-group="chat">This chat</span>
        <span className={styles.caption} data-group="model">Model use</span>
        <span className={styles.divider} aria-hidden="true" />

        <Stat label="D3 calls" hint="D3 calls in this chat and their median time">
          {fmtInt(s.d3_calls)}
          {s.d3_ms_median != null && <span className={styles.unit}>{Math.round(s.d3_ms_median)} ms<span className={styles.long}> median</span></span>}
        </Stat>
        <Stat label="Qwen 9B / 27B" hint="Share of this chat's turns that Vela sent to Qwen 9B and to Qwen 27B">
          {share ? <>{fmtPct(share.qwen9b)}<span className={styles.vs}>/</span>{fmtPct(share.qwen27b)}</> : DASH}
        </Stat>
        <Stat label="Blocked / off-topic" hint="Turns stopped by the guardrail, and off-topic turns, in this chat; both get a fixed reply with no model call">
          {s.blocked == null && s.off_topic == null ? DASH : <>{fmtInt(s.blocked ?? 0)}<span className={styles.vs}>/</span>{fmtInt(s.off_topic ?? 0)}</>}
        </Stat>
        <Stat label="Image checks avoided" hint="1 when this chat was settled without a photo check (not eligible or human review)">
          {fmtInt(s.image_checks_avoided)}
        </Stat>

        <Stat label="Decision time" hint="Time of every D3 call in this chat" wide><Dur ms={m?.d3} /></Stat>
        <Stat label="LLM time" hint="Time of every Qwen call in this chat (claim reading and replies)" wide><Dur ms={m?.llm} /></Stat>
        <Stat label="Total model time" hint="All model calls in this chat: Vela routing, D3 and Qwen" wide><Dur ms={m?.total} /></Stat>
        <Stat label="LLM tokens generated" hint={`Tokens Qwen wrote in this chat. D3 writes none; it read ${fmtInt(s.d3_tokens ?? 0)}, mostly image tokens.`} wide>
          {s.llm_tokens == null ? DASH : <>{fmtInt(s.llm_tokens)}<span className={styles.unit}>tokens</span></>}
        </Stat>
      </div>

      <div className={styles.actions}>
        <HealthBadge health={health} />
        <ThemeSwitch theme={theme} iconOnly variant="rise" onThemeChange={(next) => onTheme(next)} />
      </div>
    </header>
  );
}
