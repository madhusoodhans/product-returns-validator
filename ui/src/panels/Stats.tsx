/* The current chat session's numbers (`stats` events / GET /api/stats?session=), at the top of the right pane: "This chat"
 * counts, then "Model use" (time and tokens, each a number with its unit). Missing values render as a dash; a new chat
 * starts from empty boxes. */
import type { ReactNode } from "react";
import type { Stats as StatsT } from "../api";
import { DASH, fmtInt } from "../format";
import styles from "./Stats.module.css";

function Tile({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <div className={styles.tile} title={hint}>
      <dt>{label}</dt>
      <dd className={styles.value}>{children}</dd>
    </div>
  );
}

/** A duration as a number and a smaller unit: ms under a second, else seconds. */
function Dur({ ms }: { ms: number | null | undefined }) {
  if (ms == null) return <>{DASH}</>;
  return ms < 1000 ? <>{Math.round(ms)}<span className={styles.unit}>ms</span></> : <>{(ms / 1000).toFixed(ms < 10000 ? 2 : 1)}<span className={styles.unit}>s</span></>;
}

export function Stats({ stats }: { stats: StatsT | null }) {
  const s = stats ?? {};
  const m = s.model_ms ?? null;
  return (
    <div className={styles.root}>
      <section className={styles.card} aria-labelledby="stats-chat">
        <h2 id="stats-chat" className={styles.h}>This chat</h2>
        <dl className={styles.tiles}>
          <Tile label="D3 calls" hint="D3 calls in this chat and their median time">
            {fmtInt(s.d3_calls)}
            {s.d3_ms_median != null && <span className={styles.unit}>{Math.round(s.d3_ms_median)} ms median</span>}
          </Tile>
          <Tile label="Reasoning on" hint="Replies where the Router switched reasoning on, of the replies the language model wrote in this chat">
            {s.llm_turns == null || !s.turns ? DASH : <>{fmtInt(s.reasoning_turns ?? 0)}<span className={styles.vs}>/</span>{fmtInt(s.llm_turns)}<span className={styles.unit}>{s.llm_turns === 1 ? "reply" : "replies"}</span></>}
          </Tile>
          <Tile label="Blocked / off-topic" hint="Turns stopped by the guardrail, and off-topic turns, in this chat; both get a fixed reply with no model call">
            {s.blocked == null && s.off_topic == null ? DASH : <>{fmtInt(s.blocked ?? 0)}<span className={styles.vs}>/</span>{fmtInt(s.off_topic ?? 0)}</>}
          </Tile>
          <Tile label="Image checks avoided" hint="1 when this chat was settled without a photo check (not eligible or human review)">
            {fmtInt(s.image_checks_avoided)}
          </Tile>
        </dl>
      </section>
      <section className={styles.card} aria-labelledby="stats-model">
        <h2 id="stats-model" className={styles.h}>Model use</h2>
        <dl className={styles.tiles}>
          <Tile label="Decision time" hint="D3 flash and Vela 3 flash calls in this chat"><Dur ms={m?.decision} /></Tile>
          <Tile label="LLM time" hint="Every vllm-sr/auto call in this chat (claim reading and replies)"><Dur ms={m?.llm} /></Tile>
          <Tile label="Total model time" hint="All model calls in this chat: Vela, D3 and the language model"><Dur ms={m?.total} /></Tile>
          <Tile label="LLM tokens generated" hint={`Tokens the language model wrote in this chat, reasoning tokens included. D3 and Vela write none; they read ${fmtInt(s.d3_tokens ?? 0)}, mostly image tokens.`}>
            {s.llm_tokens == null ? DASH : <>{fmtInt(s.llm_tokens)}<span className={styles.unit}>tokens</span></>}
          </Tile>
        </dl>
      </section>
    </div>
  );
}
