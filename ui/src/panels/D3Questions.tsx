/* D3 answers: choice questions as a small distribution, yes/no questions as bars with the threshold, the borderline band
 * and a status colour; escalations to a bigger D3 sit under their question as an indented row. */
import { CornerDownRight } from "lucide-react";
import { motion, useReducedMotion } from "motion/react";
import { motionTokens } from "../components/arc/lib/motion-tokens";
import type { ChoiceQ, D3Detail, Escalation, YesNoQ } from "../api";
import { fmtMs, modelName } from "../format";
import styles from "./D3Questions.module.css";

const pct = (x: number) => `${Math.max(0, Math.min(1, x)) * 100}%`;
const optionName = (s: string, labels?: Record<string, string>) => labels?.[s] ?? s.replace(/_/g, " ");

function Fill({ value, tone }: { value: number; tone: string }) {
  const reduce = useReducedMotion();
  return (
    <motion.span className={styles.fill} data-tone={tone} initial={reduce ? false : { scaleX: 0 }} animate={{ scaleX: Math.max(0, Math.min(1, value)) }}
      transition={reduce ? { duration: 0 } : motionTokens.spring.smooth} />
  );
}

function Choice({ q }: { q: ChoiceQ }) {
  const opts = Object.entries(q.probs).sort((a, b) => b[1] - a[1]);
  return (
    <li className={styles.choice}>
      <span className={styles.label}>{q.label}</span>
      <ul className={styles.opts} style={{ ["--cols" as string]: opts.length === 4 ? 2 : Math.min(opts.length, 3) }} aria-label={`${q.label}: ${optionName(q.choice)}`}>
        {opts.map(([k, p]) => (
          <li key={k} className={styles.opt} data-chosen={k === q.choice || undefined}>
            <span className={styles.optText}><span>{optionName(k)}</span><span className={styles.num}>{p.toFixed(2)}</span></span>
            <span className={styles.optBar}><Fill value={p} tone={k === q.choice ? "accent" : "muted"} /></span>
          </li>
        ))}
      </ul>
    </li>
  );
}

/** A choice whose options carry names (`labels`, e.g. order id -> item): one row per option, with the threshold tick.
 * The top option is highlighted in accent when it clears the threshold, in warning when it does not. */
function LabelledChoice({ q, threshold, compact, top: limit }: { q: ChoiceQ; threshold?: number; compact?: boolean; top?: number }) {
  const opts = Object.entries(q.probs).sort((a, b) => b[1] - a[1]).slice(0, limit ?? undefined);
  const top = q.probs[q.choice] ?? 0;
  const passed = threshold == null || top >= threshold;
  return (
    <li className={styles.choice}>
      <ul className={styles.rows} data-compact={compact || undefined} aria-label={`${q.label}: ${optionName(q.choice, q.labels)}, ${top.toFixed(2)}${threshold != null ? `, needs ${threshold}` : ""}`}>
        {opts.map(([k, p]) => {
          const chosen = k === q.choice;
          const id = /^\d+$/.test(k) ? `Order ${k}` : "";
          return (
            <li key={k} className={styles.optRow} data-chosen={chosen || undefined} data-tone={chosen ? (passed ? "accent" : "borderline") : undefined}>
              <span className={styles.optName} title={optionName(k, q.labels)}>
                <span>{optionName(k, q.labels)}</span>{id && <span className={styles.optId}>{id}</span>}
              </span>
              <span className={styles.track} aria-hidden="true">
                <Fill value={p} tone={chosen ? (passed ? "accent" : "borderline") : "muted"} />
                {threshold != null && <span className={styles.tick} style={{ left: pct(threshold) }} />}
              </span>
              <span className={styles.num}>{p.toFixed(2)}</span>
            </li>
          );
        })}
      </ul>
    </li>
  );
}

function Track({ prob, threshold, from, tone }: { prob: number; threshold: number; from?: number; tone: string }) {
  const lo = from == null ? threshold : Math.min(from, threshold), hi = from == null ? threshold : Math.max(from, threshold);
  return (
    <span className={styles.track} aria-hidden="true">
      {from != null && <span className={styles.band} style={{ left: pct(lo), width: pct(hi - lo) }} />}
      <Fill value={prob} tone={tone} />
      <span className={styles.tick} style={{ left: pct(threshold) }} />
    </span>
  );
}

const STATUS_WORD = { ok: "ok", borderline: "borderline", bad: "bad" } as const;

function YesNo({ q, esc }: { q: YesNoQ; esc: Escalation[] }) {
  return (
    <li className={styles.yesno} data-status={q.status}>
      <span className={styles.label} title={q.label}>{q.label}</span>
      <Track prob={q.prob} threshold={q.threshold} from={q.borderline_from} tone={q.status} />
      <span className={styles.num}>{q.prob.toFixed(2)}</span>
      <span className={styles.srOnly}>{`${q.label}: ${q.prob.toFixed(2)}, threshold ${q.threshold}, ${STATUS_WORD[q.status]}`}</span>
      {esc.map((e) => {
        const tone = e.prob >= Math.max(q.threshold, q.borderline_from ?? 0) ? "ok" : e.prob < Math.min(q.threshold, q.borderline_from ?? 1) ? "bad" : "borderline";
        return (
          <div key={e.model} className={styles.esc}>
            <span className={styles.escLabel}><CornerDownRight size={13} strokeWidth={1.9} aria-hidden="true" />{modelName(e.model)}{e.ms != null ? `, ${fmtMs(e.ms)}` : ""}</span>
            <Track prob={e.prob} threshold={q.threshold} from={q.borderline_from} tone={tone} />
            <span className={styles.num}>{e.prob.toFixed(2)}</span>
          </div>
        );
      })}
    </li>
  );
}

/** `compact`: labelled options on one line each (name, bar, value), lined up with the yes/no rows (guardrail scope check).
 * `top`: show only the N most likely options of a labelled choice (the guardrail's intent). */
export function D3Questions({ detail, threshold, compact, top }: { detail: D3Detail; threshold?: number; compact?: boolean; top?: number }) {
  const esc = detail.escalated ?? [];
  return (
    <ul className={styles.root}>
      {detail.questions.map((q) => (q.type === "choice"
        ? q.labels ? <LabelledChoice key={q.key} q={q} threshold={threshold} compact={compact} top={top} /> : <Choice key={q.key} q={q} />
        : <YesNo key={q.key} q={q} esc={esc.filter((e) => e.key === q.key)} />))}
    </ul>
  );
}

/** Small key under the D3 questions. */
export function D3Legend() {
  return (
    <p className={styles.legend} aria-hidden="true">
      <span className={styles.legendTick} /> threshold <span className={styles.legendBand} /> borderline band
      <span className={styles.dot} data-tone="ok" /> ok <span className={styles.dot} data-tone="borderline" /> borderline <span className={styles.dot} data-tone="bad" /> bad
    </p>
  );
}
