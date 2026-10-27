/* Middle panel: the conversation. Replies stream in; each assistant reply carries its verdict chip. Clicking an earlier
 * user message shows that turn's flow on the right. The composer sends text, an uploaded photo (as a data URL), or both. */
import { Fragment, useEffect, useRef, useState } from "react";
import { CircleCheck, FileImage, FolderOpen, Info, MessageSquare, OctagonMinus, RotateCcw, ShieldX, UserRound, UserRoundCheck, UserRoundSearch, type LucideIcon } from "lucide-react";
import { motion, useReducedMotion } from "motion/react";
import { Alert, type AlertTone } from "../components/arc/alert/alert";
import { Badge } from "../components/arc/badge/badge";
import { Button } from "../components/arc/button/button";
import { ChatComposer, type ChatDraft } from "../components/arc/chat-thread/chat-thread";
import { EmptyState } from "../components/arc/empty-state/empty-state";
import { ScrollArea } from "../components/arc/scroll-area/scroll-area";
import { TextShimmer } from "../components/arc/text-shimmer/text-shimmer";
import { Tooltip } from "../components/arc/tooltip/tooltip";
import { motionTokens } from "../components/arc/lib/motion-tokens";
import { imageUrl, type CustomerOrder, type Ended, type OrderInfo, type Policies } from "../api";
import { OrderCards } from "./OrderCards";
import { OrderChip, PolicyButton } from "./Policy";
import { verdictMeta } from "../format";
import type { Msg, Playing } from "../useDemo";
import styles from "./Chat.module.css";

const ACCEPT = "image/jpeg,image/png,image/webp,image/heic,image/heif,.heic,.heif";

const readDataUrl = (f: File) => new Promise<string>((ok, fail) => {
  const r = new FileReader();
  r.onload = () => ok(String(r.result));
  r.onerror = () => fail(r.error);
  r.readAsDataURL(f);
});

function Thumb({ src }: { src: string }) {
  const [broken, setBroken] = useState(false);
  if (broken) return <span className={styles.noThumb}><FileImage size={15} strokeWidth={1.9} aria-hidden="true" />Attachment, no preview</span>;
  return <img className={styles.thumb} src={imageUrl(src)} alt="Uploaded photo" onError={() => setBroken(true)} />;
}

/** Tone and icon of the end banner per `ended.reason`; an unknown reason gets the neutral look. */
const END_LOOK: Record<string, { tone: AlertTone; icon: LucideIcon }> = {
  approved: { tone: "success", icon: CircleCheck },
  approved_and_handed_over: { tone: "success", icon: UserRoundCheck },
  handed_over: { tone: "info", icon: UserRound },
  not_covered: { tone: "neutral", icon: Info },
  blocked: { tone: "danger", icon: ShieldX },
  off_topic: { tone: "warning", icon: OctagonMinus },
};
const endLook = (reason: string) => END_LOOK[reason] ?? { tone: "neutral" as AlertTone, icon: Info };

/** Replaces the composer once the chat has ended: reason icon, title, text and the server's actions. */
function EndBanner({ ended, disabled, onNewChat, onReview }: { ended: Ended; disabled: boolean; onNewChat: () => void; onReview: () => void }) {
  const look = endLook(ended.reason);
  const Icon = look.icon;
  return (
    <div className={styles.endWrap}>
      <Alert className={styles.endBanner} tone={look.tone} role="status" title={ended.title} data-reason={ended.reason}
        icon={<Icon width={18} height={18} strokeWidth={1.9} aria-hidden="true" />} iconKey={ended.reason}
        actions={<>
          {ended.actions.includes("review") && (
            <Button variant="secondary" size="sm" onClick={onReview} disabled={disabled}>
              <UserRoundSearch size={14} strokeWidth={1.9} aria-hidden="true" /> Ask a colleague to review
            </Button>
          )}
          <Button size="sm" onClick={onNewChat} disabled={disabled}>
            <RotateCcw size={14} strokeWidth={1.9} aria-hidden="true" /> Start a new chat
          </Button>
        </>}>
        {ended.text}
      </Alert>
    </div>
  );
}

function Bubble({ m, selected, live, canOpen, onOpen, policies }: {
  m: Msg; selected: boolean; live: boolean; canOpen: boolean; onOpen: () => void; policies: Policies;
}) {
  const reduce = useReducedMotion();
  const v = m.role === "assistant" ? verdictMeta(m.verdict) : null;
  const enter = reduce ? { initial: { opacity: 0 }, animate: { opacity: 1 } }
    : { initial: { opacity: 0, y: 8, scale: 0.98 }, animate: { opacity: 1, y: 0, scale: 1 } };
  if (m.role === "user") {
    const body = <>
      {m.image && <Thumb src={m.image} />}
      {m.text != null && m.text !== "" && <span className={styles.text}>{m.text}{m.typing && <span className={styles.caret} aria-hidden="true" />}</span>}
    </>;
    return (
      <motion.li className={styles.row} data-role="user" {...enter} transition={reduce ? { duration: 0.12 } : motionTokens.spring.smooth}>
        {canOpen ? (
          <button type="button" className={styles.user} data-system={m.system || undefined} data-photo={(m.image && !m.text) || undefined} data-selected={selected || undefined} data-live={live || undefined} onClick={onOpen}
            aria-pressed={selected} aria-label={`${m.text || "Photo"}. Show how this turn was decided`}>
            {body}
          </button>
        ) : <div className={styles.user} data-system={m.system || undefined} data-photo={(m.image && !m.text) || undefined} data-pending>{body}</div>}
        {selected && <span className={styles.showing}>Flow shown on the right</span>}
      </motion.li>
    );
  }
  const Icon = v?.icon;
  return (
    <motion.li className={styles.row} data-role="assistant" {...enter} transition={reduce ? { duration: 0.12 } : motionTokens.spring.smooth}>
      <div className={styles.assistant} aria-busy={m.streaming || undefined}>
        {m.text ? <span className={styles.text}>{m.text}{m.streaming && <span className={styles.caret} aria-hidden="true" />}</span>
          : m.streaming ? <TextShimmer>Checking</TextShimmer> : null}
        {m.error && <span className={styles.err}>{m.error}</span>}
      </div>
      {v && Icon && (
        <div className={styles.meta}>
          <Badge tone={v.tone} size="sm" icon={<Icon size={12} strokeWidth={2} aria-hidden="true" />} className={styles.chip}>{v.label}</Badge>
          {m.policyRef && m.order && policies[m.order.product] && <PolicyButton policy={policies[m.order.product]} order={m.order} highlight={m.policyRef} />}
        </div>
      )}
    </motion.li>
  );
}

export function Chat({ messages, busy, playing, replayMsg, liveTurn, order, policies, orders, showOrders, ended, onSend, onReview, onOpenTurn, onGallery, onReset }: {
  messages: Msg[]; busy: boolean; playing: Playing | null; replayMsg: string | null; liveTurn: string | null;
  order: OrderInfo | null; policies: Policies;
  /** The customer's orders, shown as cards under the greeting while `showOrders` (no order known yet, not a sample). */
  orders: CustomerOrder[]; showOrders: boolean;
  /** message.ended of the latest reply: the chat is over, the composer gives way to the end banner. */
  ended: Ended | null; onReview: () => void;
  onSend: (input: { text?: string; image?: string }) => void; onOpenTurn: (m: Msg) => void; onGallery: () => void; onReset: () => void;
}) {
  const viewport = useRef<HTMLDivElement>(null);
  const [fileError, setFileError] = useState<string | null>(null);
  const last = messages[messages.length - 1];
  const tail = `${messages.length}:${last?.text?.length ?? 0}:${last?.streaming ? 1 : 0}`;
  useEffect(() => {
    const el = viewport.current;
    if (el) el.scrollTo({ top: el.scrollHeight, behavior: "auto" });
  }, [tail, ended]);

  const locked = busy || !!playing;
  const cards = showOrders && orders.length > 0;
  const hasUser = messages.some((m) => m.role === "user");
  const streaming = last?.role === "assistant" && !!last.streaming;
  const send = async (d: ChatDraft) => {
    setFileError(null);
    const f = d.files[0];
    if (f && !/^image\/(jpeg|png|webp|heic|heif)$/.test(f.type) && !/\.(heic|heif)$/i.test(f.name)) { setFileError("Send a JPG, PNG, WebP or HEIC photo."); return; }
    const image = f ? await readDataUrl(f) : undefined;
    if (d.text || image) onSend({ text: d.text || undefined, image });
  };

  return (
    <section className={styles.root} aria-label="Chat">
      <div className={styles.head}>
        <h2>Chat</h2>
        <span className={styles.now}>
          {playing ? <>Playing <strong>{playing.scenario.title}</strong>{playing.all ? " (Play all)" : ""}</> : ""}
        </span>
        {order && <OrderChip order={order} policy={policies[order.product]} />}
        <Button variant="ghost" size="sm" onClick={onReset} disabled={locked || !hasUser}>
          <RotateCcw size={14} strokeWidth={1.9} aria-hidden="true" /> New chat
        </Button>
      </div>
      <ScrollArea className={styles.scroll} viewportRef={viewport} label="Conversation">
        {messages.length === 0 ? (
          <div className={styles.empty}>
            <EmptyState icon={<MessageSquare size={24} />} title="Report a damaged delivery"
              description="Pick a sample on the left, or describe the problem below and attach a photo." />
          </div>
        ) : (
          <ol className={styles.list} role="log" aria-live="polite" aria-relevant="additions">
            {messages.map((m, i) => (
              <Fragment key={m.id}>
                <Bubble m={m} selected={replayMsg === m.id} live={!replayMsg && !!m.turn && m.turn === liveTurn}
                  canOpen={m.role === "user" && !!m.turn} onOpen={() => onOpenTurn(m)} policies={policies} />
                {cards && (m.greeting || (i === 0 && !messages.some((x) => x.greeting))) && (
                  <li className={styles.cardsRow}>
                    <OrderCards orders={orders} disabled={locked} compact={hasUser} onPick={(o) => onSend({ text: `Order ${o.order}` })} />
                  </li>
                )}
              </Fragment>
            ))}
            {ended && !streaming && (
              <li className={styles.endRow}>
                <span className={styles.endDivider}>Chat ended</span>
                <EndBanner ended={ended} disabled={locked} onNewChat={onReset} onReview={onReview} />
              </li>
            )}
          </ol>
        )}
      </ScrollArea>
      {ended ? null : <div className={styles.compose}>
        {fileError && <p className={styles.fileError} role="alert">{fileError}</p>}
        <div className={styles.composeRow}>
          <Tooltip content="Sample photos">
            <button type="button" className={styles.gallery} aria-label="Pick a sample photo" onClick={onGallery} disabled={locked}>
              <FolderOpen size={18} strokeWidth={1.75} />
            </button>
          </Tooltip>
          <ChatComposer className={styles.composer} onSend={send} accept={ACCEPT} disabled={locked}
            placeholder={playing ? "Playing a sample" : cards ? "Describe the problem, or pick an order above" : "Describe the problem, or attach a photo"}
            labels={{ attach: "Upload a photo" }} />
        </div>
      </div>}
    </section>
  );
}
