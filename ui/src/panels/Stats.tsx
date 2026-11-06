/* The current chat session's numbers (`stats` events / GET /api/stats?session=), at the top of the right pane: "This chat"
 * two columns side by side: "Model use" (time and tokens) and "Decisions" (D3, Router and guardrail counts), each a list of
 * label and value rows. Missing values render as a dash; a new chat
 * starts from empty boxes. */
import type { ReactNode } from "react";
import type { Stats as StatsT } from "../api";
import { DASH, fmtInt } from "../format";
import styles from "./Stats.module.css";

function Row({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <div className={styles.row} title={hint}>
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
      <section className={styles.col} aria-labelledby="stats-model">
        <h2 id="stats-model" className={styles.h}>Model use</h2>
        <dl className={styles.rows}>
          <Row label="Decision time" hint="D3 flash and Vela 3 flash calls in this chat"><Dur ms={m?.decision} /></Row>
          <Row label="LLM time" hint="Every vllm-sr/auto call in this chat (claim reading and replies)"><Dur ms={m?.llm} /></Row>
          <Row label="Total model time" hint="All model calls in this chat: Vela, D3 and the language model"><Dur ms={m?.total} /></Row>
          <Row label="LLM tokens" hint={`Tokens the language model wrote in this chat, reasoning tokens included. D3 and Vela write none; they read ${fmtInt(s.d3_tokens ?? 0)}, mostly image tokens.`}>
            {s.llm_tokens == null ? DASH : fmtInt(s.llm_tokens)}
          </Row>
        </dl>
      </section>
      <section className={styles.col} aria-labelledby="stats-decisions">
        <h2 id="stats-decisions" className={styles.h}>Decisions</h2>
        <dl className={styles.rows}>
          <Row label="D3 calls" hint="D3 calls in this chat">{fmtInt(s.d3_calls)}</Row>
          <Row label="D3 median" hint="Median time of one D3 call in this chat"><Dur ms={s.d3_ms_median} /></Row>
          <Row label="Reasoning on" hint="Replies where the Router switched reasoning on, of the replies the language model wrote in this chat">
            {s.llm_turns == null || !s.turns ? DASH : <>{fmtInt(s.reasoning_turns ?? 0)}<span className={styles.vs}>/</span>{fmtInt(s.llm_turns)}</>}
          </Row>
          <Row label="Blocked / off-topic" hint="Turns stopped by the guardrail, and off-topic turns, in this chat; both get a fixed reply with no model call. Upset customers handed to a person are not counted here">
            {s.blocked == null && s.off_topic == null ? DASH : <>{fmtInt(s.blocked ?? 0)}<span className={styles.vs}>/</span>{fmtInt(s.off_topic ?? 0)}</>}
          </Row>
          <Row label="To a person" hint={`Turns handed to a person in this chat: worn or repaired items, unclear photos, requests for a person, and upset customers (a hand-off, not a block)${s.reviews ? `; ${fmtInt(s.reviews)} upset, with a fixed reply` : ""}`}>
            {fmtInt(s.to_person)}
          </Row>
          <Row label="Image checks avoided" hint="1 when this chat was settled without a photo check (not eligible or human review)">
            {fmtInt(s.image_checks_avoided)}
          </Row>
        </dl>
      </section>
    </div>
  );
}
