/* Right panel: how this turn was decided. One node per step in flow order; nodes move waiting -> running -> done,
 * skipped, blocked or failed as `step` events arrive. D3 nodes expand to their questions. Clicking an earlier user
 * message shows that turn's stored steps here instead of the live turn. */
import { useEffect, useRef, useState, type ReactNode } from "react";
import { Check, ChevronDown, CircleCheck, History, Info, MessageCircleQuestion, TriangleAlert, UserRound, X, type LucideIcon } from "lucide-react";
import { AnimatePresence, motion, useReducedMotion } from "motion/react";
import { Badge } from "../components/arc/badge/badge";
import { Button } from "../components/arc/button/button";
import { ScrollArea } from "../components/arc/scroll-area/scroll-area";
import { motionTokens } from "../components/arc/lib/motion-tokens";
import {
  NODES, type ChoiceQ, type ClaimDetail, type D3Detail, type DecisionDetail, type GuardrailDetail, type NodeKey, type OrderMatch, type PhotoDetail, type PolicyDetail,
  type ReplyDetail, type RouteDetail, type RouterDetail, type StepEvent,
} from "../api";
import { D3_NODES, NODE_HINT, NODE_LABEL, ROUTER_NOTE, fmtMs, modelName, photoRule, reasoningWords, routedName, titleCase, verdictMeta, type RuleTone } from "../format";
import type { Flow as FlowT, Replay } from "../useDemo";
import { D3Legend, D3Questions } from "./D3Questions";
import styles from "./Flow.module.css";

/** Live turn ids are "<session uuid>-t2"; show the "t2" part. */
const shortTurn = (t: string) => t.match(/-(t\d+)$/)?.[1] ?? t;

const STATUS_TEXT = { waiting: "Waiting", running: "Running", done: "Done", skipped: "Not needed", blocked: "Blocked", failed: "Failed" } as const;

/** "Fast decision, reasoning off" for one routed call. */
const routeLine = (r: RouteDetail) => `${titleCase(r.decision || "unknown")} decision, ${reasoningWords(r)}`;

const CALLOUT_ICON: Record<RuleTone, LucideIcon> = { success: CircleCheck, warning: TriangleAlert, info: UserRound, neutral: Info };

/** One line that says which rule decided, in the tone of its outcome. */
function Callout({ tone, icon, children }: { tone: RuleTone; icon?: LucideIcon; children: ReactNode }) {
  const Icon = icon ?? CALLOUT_ICON[tone];
  return <p className={styles.callout} data-tone={tone}><Icon size={14} strokeWidth={2} aria-hidden="true" /><span>{children}</span></p>;
}

/** What vllm-sr/auto picked for the reply: decision and reasoning, then the model it routed to, as given. The claim
 * extraction call is routed on its own and shown small when it went elsewhere. */
function RouterView({ d }: { d: RouterDetail }) {
  if (!d.decision && !d.model) return null;
  const ex = d.extraction;
  return (
    <div className={styles.router}>
      <p className={styles.routerPick}>
        {titleCase(d.decision || "unknown")} decision<span className={styles.routerWhy}>, {reasoningWords(d)}</span>
      </p>
      {d.model && <p className={styles.small}>Model <code className={styles.routerName} title={d.model}>{d.model}</code></p>}
      {d.signals && <p className={styles.small}>Signals <code className={styles.routerName}>{d.signals}</code></p>}
      {ex && (ex.decision || ex.model) && (
        <p className={styles.small}>Claim reading: {routeLine(ex)}{ex.model ? `, ${routedName(ex.model)}` : ""}</p>
      )}
      <p className={styles.note}>{ROUTER_NOTE}</p>
    </div>
  );
}

/** D3 matched a message without an order number to one of the customer's orders. */
function OrderMatchView({ m }: { m: OrderMatch }) {
  const q = m.questions.find((x) => x.type === "choice") as ChoiceQ | undefined;
  if (!q) return null;
  const top = q.probs[q.choice] ?? 0;
  const th = m.threshold;
  const none = q.choice === "none" || !!m.no_match;
  const matched = q.choice !== "unclear" && !none && (th == null || top >= th);
  return (
    <div className={styles.match}>
      <div className={styles.matchHead}>
        <span className={styles.matchName}>Order match<span className={styles.d3}>D3</span></span>
        <span className={styles.matchResult} data-ok={matched}>
          {none ? `None of these orders at ${top.toFixed(2)}` : matched ? `Matched at ${top.toFixed(2)}` : `Best ${top.toFixed(2)}, not matched`}{th != null && !none ? `, needs ${th}` : ""}
        </span>
      </div>
      <D3Questions detail={m} threshold={th} />
      {none && <Callout tone="info" icon={MessageCircleQuestion}>The description fits none of the customer's orders, so the bot asks which order. It never guesses.</Callout>}
    </div>
  );
}

function ClaimView({ d }: { d: ClaimDetail }) {
  const rows = ([["Order", d.order], ["Product", d.product], ["Issue", d.issue], ["Part", d.part]] as const).filter(([, v]) => v);
  const match = d.order_match?.questions?.length ? d.order_match : null;
  const fit = d.order_fit && !d.order_fit.fits ? d.order_fit : null;
  const said = fit?.said ? Object.values(fit.said).flat().join(", ") : "";
  if (!rows.length && !match && !fit) return null;
  return (
    <>
      {rows.length > 0 && (
        <dl className={styles.kv}>
          {rows.map(([k, v]) => <div key={k}><dt>{k}</dt><dd>{titleCase(String(v)).replace(/_/g, " ")}</dd></div>)}
        </dl>
      )}
      {match && <OrderMatchView m={match} />}
      {fit && (
        <Callout tone="info" icon={MessageCircleQuestion}>
          {`The details given${said ? ` (${said})` : ""} do not fit order ${fit.order}, so the bot asks which order. Checked in code, no model call.`}
        </Callout>
      )}
    </>
  );
}

function PolicyView({ d }: { d: PolicyDetail }) {
  return (
    <ul className={styles.rules}>
      {d.rules.map((r) => (
        <li key={r.label} data-ok={r.ok}>
          {r.ok ? <Check size={14} strokeWidth={2.2} aria-label="Pass" /> : <X size={14} strokeWidth={2.2} aria-label="Fail" />}
          <span>{r.label}</span>
        </li>
      ))}
    </ul>
  );
}

function DecisionView({ d }: { d: DecisionDetail }) {
  const v = verdictMeta(d.verdict);
  const Icon = v?.icon;
  return (
    <div className={styles.decision}>
      {v && Icon && <Badge tone={v.tone} icon={<Icon size={13} strokeWidth={2} aria-hidden="true" />}>{v.label}</Badge>}
      {!!d.reasons?.length && <ul className={styles.reasons}>{d.reasons.map((r) => <li key={r}>{r}</li>)}</ul>}
      {d.rule && <code className={styles.rule}>{d.rule}</code>}
      {d.case === "review" && (
        <Callout tone="info">A hand-off, not a block: the message is about an order, so a person reviews the chat. The customer gets a fixed reply with no language model call, and nothing is approved.</Callout>
      )}
      {d.case === "vague" && (
        <Callout tone="info" icon={MessageCircleQuestion}>A vague request: no item, order or reason given, so the bot asks a follow-up question instead of answering from the Returns page.</Callout>
      )}
      {d.case === "no_match" && (
        <Callout tone="info" icon={MessageCircleQuestion}>Which order? The description fits none of the customer's orders, so the bot asks.</Callout>
      )}
    </div>
  );
}

/** The D3 scope check on the guardrail: what the message is (choice `scope`), commands or code, real product problem,
 * then what the customer wants (choice `intent`, top 3 options so the node stays short). */
function ScopeView({ m }: { m: D3Detail }) {
  const q = m.questions.find((x) => x.key === "scope" && x.type === "choice") as ChoiceQ | undefined;
  const intent = m.questions.find((x) => x.key === "intent" && x.type === "choice") as ChoiceQ | undefined;
  const name = q ? q.labels?.[q.choice] ?? titleCase(q.choice) : "";
  const tone = !q ? undefined : q.choice === "returns" ? "ok" : q.choice === "off_topic" ? "info" : "bad";
  const rest: D3Detail = { ...m, questions: m.questions.filter((x) => x !== intent) };
  return (
    <div className={styles.match}>
      <div className={styles.matchHead}>
        <span className={styles.matchName}>Scope check<span className={styles.d3}>D3</span></span>
        {q && <span className={styles.matchResult} data-tone={tone}>{name} at {(q.probs[q.choice] ?? 0).toFixed(2)}</span>}
      </div>
      <D3Questions detail={rest} compact />
      {intent && (
        <>
          <div className={styles.subHead}>
            <span>{intent.label || "Intent"}</span>
            <span className={styles.subResult}>{intent.labels?.[intent.choice] ?? titleCase(intent.choice)} at {(intent.probs[intent.choice] ?? 0).toFixed(2)}</span>
          </div>
          <D3Questions detail={{ questions: [intent] }} compact top={3} />
        </>
      )}
    </div>
  );
}

/** Vela 3 flash scores against their thresholds, then the D3 scope check. No scores: Vela was unavailable. */
function GuardView({ d }: { d: GuardrailDetail }) {
  const scope = d.scope_check?.questions?.length ? d.scope_check : null;
  const ta = d.thresholds?.attack ?? 0.5, th = d.thresholds?.harm ?? 0.5;
  const vela = d.attack != null || d.harm != null;
  return (
    <>
      <p className={styles.small}>
        {vela ? <>
          Vela prompt attack <span className={styles.num} data-bad={(d.attack ?? 0) >= ta || undefined}>{d.attack != null ? d.attack.toFixed(2) : "–"}</span>, needs &lt; {ta.toFixed(2)}
          {" · "}harm <span className={styles.num} data-bad={(d.harm ?? 0) >= th || undefined}>{d.harm != null ? d.harm.toFixed(2) : "–"}</span>, needs &lt; {th.toFixed(2)}
          {d.vela_flagged ? " · flagged" : ""}
        </> : "Vela unavailable, D3 checked alone"}
      </p>
      {scope && <ScopeView m={scope} />}
      {d.review && (
        <Callout tone="info">Vela flagged harm, often just swearing, but D3 reads the message as about an order. Not blocked: handed to a person for review.</Callout>
      )}
    </>
  );
}

/** Tokens, then the D3 reply check; a replaced reply shows what was caught. */
function ReplyView({ step, d }: { step: StepEvent; d: ReplyDetail }) {
  const c = d.reply_check;
  if (d.standard) return d.reason ? <p className={styles.small}>{d.reason}</p> : null;
  const tokens = !step.summary && d.tokens != null ? <p className={styles.small}>{d.tokens} tokens{d.model ? ` by ${d.model}` : ""}</p> : null;
  if (!c) return tokens;
  return (
    <>
      {tokens}
      <p className={styles.small}>
        Reply check (D3): claims approval <span className={styles.num}>{c.claims_approval.toFixed(2)}</span>, needs &lt; {c.threshold.toFixed(2)}
      </p>
      {c.replaced && (
        <div className={styles.caught} role="note">
          <p className={styles.caughtHead}><TriangleAlert size={14} strokeWidth={2} aria-hidden="true" />Replaced: the reply claimed an approval</p>
          {c.original && (
            <details className={styles.original}>
              <summary>Original reply</summary>
              <del>{c.original}</del>
            </details>
          )}
        </div>
      )}
    </>
  );
}

function Detail({ node, step, open }: { node: NodeKey; step: StepEvent; open: boolean }) {
  const d = step.detail as unknown;
  if (!d || (step.status !== "done" && step.status !== "blocked" && step.status !== "failed")) return null;
  switch (node) {
    case "guardrail": return <GuardView d={d as GuardrailDetail} />;
    case "router": return <RouterView d={d as RouterDetail} />;
    case "claim": return <ClaimView d={d as ClaimDetail} />;
    case "policy": return (d as PolicyDetail).rules ? <PolicyView d={d as PolicyDetail} /> : null;
    case "decision": return <DecisionView d={d as DecisionDetail} />;
    case "reply": return <ReplyView step={step} d={d as ReplyDetail} />;
    case "image_check": {
      const p = d as PhotoDetail;
      const r = photoRule(p.rule, p.damage_band);
      return <>
        {r && <Callout tone={r.tone}>{r.text}</Callout>}
        {open && p.questions ? <><D3Questions detail={p} /><D3Legend /></> : null}
      </>;
    }
    case "eligibility":
      return open && (d as D3Detail).questions ? <><D3Questions detail={d as D3Detail} /><D3Legend /></> : null;
  }
}

function Node({ node, step, last }: { node: NodeKey; step: StepEvent | undefined; last: boolean }) {
  const reduce = useReducedMotion();
  const status = step?.status ?? "waiting";
  const isD3 = D3_NODES.includes(node);
  const usesD3 = isD3 || (status !== "skipped" && (
    (node === "claim" && !!(step?.detail as ClaimDetail | undefined)?.order_match)
    || (node === "guardrail" && !!(step?.detail as GuardrailDetail | undefined)?.scope_check)
    || (node === "reply" && !!(step?.detail as ReplyDetail | undefined)?.reply_check)));
  const replaced = node === "reply" && !!(step?.detail as ReplyDetail | undefined)?.reply_check?.replaced;
  // a standard reply (no LLM could word it) gets the same warning border, with its summary
  const standard = node === "reply" && !!(step?.detail as ReplyDetail | undefined)?.standard;
  const hasQuestions = isD3 && !!(step?.detail as D3Detail | undefined)?.questions?.length && status !== "skipped";
  const [open, setOpen] = useState(true);
  const verdictShown = node === "decision" && !!(step?.detail as DecisionDetail | undefined)?.verdict && step?.status === "done";
  // A replaced reply says so in its warning block (with the original), so the summary line is not repeated.
  // A router that reports its pick shows it as its own lines, so the summary ("Fast decision, reasoning off") is not repeated.
  const route = node === "router" ? (step?.detail as RouterDetail | undefined) : undefined;
  const routerPick = node === "router" && status === "done" && !!(route?.decision || route?.model);
  const summary = verdictShown || replaced || routerPick ? "" : step?.summary || (status === "skipped" ? "Not needed" : status === "waiting" || !step ? NODE_HINT[node] : "");
  const hasDetail = !!step?.detail && (step.status === "done" || step.status === "blocked" || step.status === "failed") && (!isD3 || (hasQuestions && open)
    || (node === "image_check" && status !== "skipped" && !!(step?.detail as PhotoDetail | undefined)?.rule));
  const detail: ReactNode = hasDetail ? <Detail node={node} step={step!} open={open} /> : null;
  const model = step?.model && status !== "skipped" ? modelName(step.model) : "";
  const ref = useRef<HTMLLIElement>(null);
  // Keep the active step in view on a big screen; D3 nodes scroll their questions in when they land.
  useEffect(() => {
    if (status !== "running" && !(status === "done" && hasQuestions)) return;
    ref.current?.scrollIntoView({ block: "nearest", behavior: reduce ? "auto" : "smooth" });
  }, [status, hasQuestions, reduce]);
  const skipped = status === "skipped";
  return (
    <li ref={ref} className={styles.node} data-status={status} data-last={last || undefined} data-replaced={replaced || standard || undefined}>
      <span className={styles.rail} aria-hidden="true"><span className={styles.dot} /></span>
      <div className={styles.body}>
        <div className={styles.headRow}>
          <h3 className={styles.name} title={node === "router" ? ROUTER_NOTE : undefined}>{NODE_LABEL[node]}{usesD3 && <span className={styles.d3}>D3</span>}</h3>
          {model && <span className={styles.model} title={step?.model} data-big={(routerPick && route?.reasoning === "on") || undefined}>{model}</span>}
          {skipped && <span className={styles.inlineSkip}><span className={styles.skipTag}>Skipped</span>{summary}</span>}
          <span className={styles.ms}>{step?.ms != null && !skipped ? fmtMs(step.ms) : ""}</span>
          <span className={styles.srOnly}>{STATUS_TEXT[status]}</span>
        </div>
        {summary !== "" && !skipped && <p className={styles.summary}>
          {summary}
        </p>}
        {hasQuestions && (
          <button type="button" className={styles.expand} aria-expanded={open} onClick={() => setOpen((o) => !o)}>
            <motion.span animate={{ rotate: open ? 0 : -90 }} transition={reduce ? { duration: 0 } : motionTokens.spring.snappy} className={styles.expandIcon}>
              <ChevronDown size={14} strokeWidth={1.9} aria-hidden="true" />
            </motion.span>
            {open ? "Hide questions" : `Show ${(step!.detail as D3Detail).questions.length} questions`}
          </button>
        )}
        <AnimatePresence initial={false}>
          {detail && (
            <motion.div key="detail" className={styles.detailWrap}
              initial={reduce ? { opacity: 0 } : { height: 0, opacity: 0 }} animate={reduce ? { opacity: 1 } : { height: "auto", opacity: 1 }}
              exit={reduce ? { opacity: 0 } : { height: 0, opacity: 0 }} transition={reduce ? { duration: 0.12 } : motionTokens.spring.smooth}>
              <div className={styles.detail}>{detail}</div>
            </motion.div>
          )}
        </AnimatePresence>
      </div>
    </li>
  );
}

/** D3 time this turn (docs/UI.md "D3 time in the flow header"): steps run by D3 alone (model contains "d3", e.g. eligibility,
 * photo check; for the guardrail only its D3 scope check, `scope_check.ms`), the
 * order match inside the claim step when the server times it (`order_match.ms`), and the reply check (`reply_check.ms`).
 * The claim and reply steps themselves are language model time and are not counted. */
function d3Ms(steps: StepEvent[]): number {
  let ms = 0;
  for (const s of steps) {
    if (s.status === "skipped" || s.status === "waiting") continue;
    if (s.node === "claim") ms += (s.detail as ClaimDetail | undefined)?.order_match?.ms ?? 0;
    else if (s.node === "reply") ms += (s.detail as ReplyDetail | undefined)?.reply_check?.ms ?? 0;
    else if (s.node === "guardrail" && (s.detail as GuardrailDetail | undefined)?.scope_check?.ms != null)
      ms += (s.detail as GuardrailDetail).scope_check!.ms ?? 0;  // only D3's scope check, not the Vela screen
    else if (/d3/i.test(s.model ?? "") || D3_NODES.includes(s.node)) ms += s.ms ?? 0;
  }
  return ms;
}

export function Flow({ flow, replay, hasTurn, onBack }: { flow: FlowT; replay: Replay | null; hasTurn: boolean; onBack: () => void }) {
  const shown: FlowT = replay ? replay.steps ?? {} : flow;
  const steps = Object.values(shown).filter(Boolean) as StepEvent[];
  const total = steps.filter((s) => s.status !== "skipped" && s.ms != null).reduce((a, s) => a + (s.ms ?? 0), 0);
  const d3 = d3Ms(steps);
  const running = steps.some((s) => s.status === "running");
  return (
    <section className={styles.root} aria-label="How this was decided">
      <div className={styles.head}>
        <div className={styles.titleRow}>
          <h2>How this was decided</h2>
          {replay && <Button variant="ghost" size="sm" onClick={onBack}>Back to latest</Button>}
        </div>
        <p className={styles.sub}>
          {replay ? <><History size={13} strokeWidth={1.9} aria-hidden="true" /> Earlier turn {shortTurn(replay.turn)}{replay.error ? `: ${replay.error}` : ""}</>
            : !hasTurn && !steps.length ? "Send a message to see each step"
            : running ? "Running" : steps.length ? total > 0 ? `Turn total ${fmtMs(total)}${d3 ? `, D3 ${fmtMs(d3)}` : ""}` : "No model time this turn" : ""}
        </p>
      </div>
      <ScrollArea className={styles.scroll} label="Flow steps">
        <ol className={styles.list}>
          {NODES.map((n, i) => <Node key={replay ? `${replay.turn}-${n}` : n} node={n} step={shown[n]} last={i === NODES.length - 1} />)}
        </ol>
      </ScrollArea>
    </section>
  );
}
