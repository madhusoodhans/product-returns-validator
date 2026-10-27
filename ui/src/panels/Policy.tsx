/* The product return policy, so viewers can check a reply against it. Opened from the order chip in the chat header
 * (no highlight) or from the Policy button under a reply (the part of the policy that reply rests on is highlighted). */
import type { ReactNode } from "react";
import { Info } from "lucide-react";
import { Popover, PopoverContent, PopoverTrigger } from "../components/arc/popover/popover";
import type { OrderInfo, Policy, PolicyRef } from "../api";
import styles from "./Policy.module.css";

const money = (n: number) => `$${n.toLocaleString("en-US")}`;
const days = (n: number) => `${n} day${n === 1 ? "" : "s"}`;

function Row({ label, on, children }: { label: string; on: boolean; children: ReactNode }) {
  return (
    <div className={styles.row} data-highlight={on || undefined}>
      <dt>{label}{on && <span className={styles.here}>This reply</span>}</dt>
      <dd>{children}</dd>
    </div>
  );
}

export function PolicyView({ policy, order, highlight }: { policy: Policy; order: OrderInfo | null; highlight?: PolicyRef | null }) {
  const d = order?.delivered_days_ago;
  const inWindow = d == null ? null : d <= policy.window_days;
  const limit = policy.human_review_over;
  const overLimit = limit != null && order?.price != null ? order.price > limit : null;
  return (
    <div className={styles.view}>
      <div className={styles.top}>
        <h3 className={styles.title}>{policy.name} return policy</h3>
        {order && (
          <p className={styles.order}>
            Order {order.order}{order.item ? ` · ${order.item}` : ""}{order.price != null ? ` · ${money(order.price)}` : ""}
          </p>
        )}
      </div>
      <dl className={styles.list}>
        <Row label="Return window" on={highlight === "window"}>
          <span>{days(policy.window_days)} from delivery</span>
          {d != null && (
            <span className={styles.fact} data-ok={inWindow}>
              Delivered {d === 0 ? "today" : `${days(d)} ago`}, {inWindow ? "inside the window" : "outside the window"}
            </span>
          )}
        </Row>
        <Row label="Covered" on={highlight === "covered"}>{policy.covered}</Row>
        <Row label="Not covered" on={highlight === "not_covered"}>
          <ul className={styles.bullets}>{policy.not_covered.map((l) => <li key={l}>{l}</li>)}</ul>
        </Row>
        <Row label="Photo" on={highlight === "photo"}>{policy.photo}</Row>
        {(limit != null || highlight === "review_limit") && (
          <Row label="Review limit" on={highlight === "review_limit"}>
            <span>{limit != null ? `Orders over ${money(limit)} are checked by a person` : "No review limit for this product"}</span>
            {overLimit != null && order?.price != null && (
              <span className={styles.fact} data-ok={!overLimit}>This order is {money(order.price)}, {overLimit ? "over the limit" : "under the limit"}</span>
            )}
          </Row>
        )}
      </dl>
    </div>
  );
}

const REF_LABEL: Record<PolicyRef, string> = {
  covered: "what is covered", not_covered: "what is not covered", window: "the return window", review_limit: "the review limit", photo: "the photo rule",
};

/** Small "Policy" button under a reply; opens the policy with the referenced part highlighted. */
export function PolicyButton({ policy, order, highlight }: { policy: Policy; order: OrderInfo | null; highlight: PolicyRef }) {
  return (
    <Popover>
      <PopoverTrigger asChild>
        <button type="button" className={styles.btn} aria-label={`Policy: ${REF_LABEL[highlight]} for the ${policy.name.toLowerCase()}`}>
          <Info size={13} strokeWidth={2} aria-hidden="true" /> Policy
        </button>
      </PopoverTrigger>
      <PopoverContent className={styles.pop} side="bottom" align="start" aria-label={`${policy.name} return policy`}>
        <PolicyView policy={policy} order={order} highlight={highlight} />
      </PopoverContent>
    </Popover>
  );
}

/** "Order 4411 · Smartphone" with an info button, for the chat header. */
export function OrderChip({ policy, order }: { policy: Policy | undefined; order: OrderInfo }) {
  return (
    <span className={styles.chip}>
      <span className={styles.chipText}>Order {order.order} · {policy?.name ?? order.product}</span>
      {policy && (
        <Popover>
          <PopoverTrigger asChild>
            <button type="button" className={styles.chipInfo} aria-label={`Show the ${policy.name.toLowerCase()} return policy`}>
              <Info size={15} strokeWidth={1.9} aria-hidden="true" />
            </button>
          </PopoverTrigger>
          <PopoverContent className={styles.pop} side="bottom" align="end" aria-label={`${policy.name} return policy`}>
            <PolicyView policy={policy} order={order} />
          </PopoverContent>
        </Popover>
      )}
    </span>
  );
}
