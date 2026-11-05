/* A plain bar, as in the receipt demo: link back to the page of all demos, the title in the middle, then health, API key
 * and theme. The chat's stats sit at the top of the right pane (Stats). */
import { ArrowLeft } from "lucide-react";
import { ThemeSwitch, type Theme } from "../components/arc/theme-switch/theme-switch";
import type { Health } from "../api";
import { KeyMenu } from "./KeyMenu";
import styles from "./TopBar.module.css";

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

export function TopBar({ health, theme, onTheme, landingUrl, keyStatus, busy, onChangeKey, onForgetKey }: {
  health: Health | null; theme: Theme; onTheme: (t: Theme) => void; landingUrl?: string;
  /** The visitor's API key (KeyMenu): "Enter API key" before one works, then a small key icon with a menu. */
  keyStatus: "checking" | "ready" | "none"; busy?: boolean; onChangeKey: () => void; onForgetKey: () => void | Promise<void>;
}) {
  return (
    <header className={styles.bar}>
      <div className={styles.brand}>
        {landingUrl && (
          <a className={styles.back} href={backHref(landingUrl, theme)} aria-label="Back to all demos" title="All demos">
            <ArrowLeft size={16} aria-hidden="true" /><span className={styles.backText}>All demos</span>
          </a>
        )}
      </div>
      <div className={styles.title}>
        <h1 title="D3 decides; the language model picked by vllm-sr/auto words the reply">Returns validator Demo</h1>
      </div>

      <div className={styles.actions}>
        <HealthBadge health={health} />
        <KeyMenu status={keyStatus} busy={busy} onEnter={onChangeKey} onChange={onChangeKey} onForget={onForgetKey} />
        <ThemeSwitch theme={theme} iconOnly variant="rise" onThemeChange={(next) => onTheme(next)} />
      </div>
    </header>
  );
}
