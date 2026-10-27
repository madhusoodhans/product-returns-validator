/* "Your orders": the logged-in customer's recent orders as compact cards under the greeting, shown until the chat
 * knows its order. Clicking a card sends a normal user message "Order 4411", so the Claim step runs as usual. */
import { useState } from "react";
import type { LucideIcon } from "lucide-react";
import {
  ChevronDown, Coffee, Footprints, GlassWater, Headphones, Lamp, Laptop, Luggage, Package, Smartphone, Wine,
} from "lucide-react";
import { motion, useReducedMotion } from "motion/react";
import { Button } from "../components/arc/button/button";
import { motionTokens } from "../components/arc/lib/motion-tokens";
import type { CustomerOrder } from "../api";
import styles from "./OrderCards.module.css";

const ICON: Record<string, LucideIcon> = {
  phone: Smartphone, laptop: Laptop, mug: Coffee, glass: Wine, sneaker: Footprints, headphones: Headphones,
  suitcase: Luggage, lamp: Lamp, bottle: GlassWater,
};
const FIRST = 6;

const delivered = (d: number) => (d <= 0 ? "Delivered today" : d === 1 ? "Delivered yesterday" : `Delivered ${d} days ago`);

/** `compact` (the chat has moved on without an order) shows the first six with a "Show all" toggle; otherwise all of them. */
export function OrderCards({ orders, disabled, compact, onPick }: {
  orders: CustomerOrder[]; disabled: boolean; compact: boolean; onPick: (o: CustomerOrder) => void;
}) {
  const reduce = useReducedMotion();
  const [all, setAll] = useState(false);
  if (!orders.length) return null;
  const shown = all || !compact ? orders : orders.slice(0, FIRST);
  return (
    <motion.section className={styles.root} aria-labelledby="your-orders"
      initial={reduce ? { opacity: 0 } : { opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }}
      transition={reduce ? { duration: 0.12 } : motionTokens.spring.smooth}>
      <h3 id="your-orders" className={styles.title}>Your orders <span className={styles.count}>Pick one to start</span></h3>
      <ul className={styles.grid}>
        {shown.map((o) => {
          const Icon = ICON[o.product] ?? Package;
          const outside = o.window_days != null && o.delivered_days_ago > o.window_days;
          return (
            <li key={o.order}>
              <button type="button" className={styles.card} disabled={disabled} onClick={() => onPick(o)}
                aria-label={`Order ${o.order}, ${o.item}, ${delivered(o.delivered_days_ago).toLowerCase()}${outside ? `, outside the ${o.window_days}-day window` : ""}`}>
                <span className={styles.icon} aria-hidden="true"><Icon size={18} strokeWidth={1.75} /></span>
                <span className={styles.text}>
                  <span className={styles.item}>{o.item}</span>
                  <span className={styles.meta}>
                    <span>Order {o.order}</span><span aria-hidden="true">·</span><span>{delivered(o.delivered_days_ago)}</span>
                  </span>
                  {outside && <span className={styles.hint}>Outside the {o.window_days}-day window</span>}
                </span>
              </button>
            </li>
          );
        })}
      </ul>
      {compact && orders.length > FIRST && (
        <Button variant="ghost" size="sm" className={styles.more} onClick={() => setAll((a) => !a)} aria-expanded={all}>
          <ChevronDown size={14} strokeWidth={1.9} aria-hidden="true" className={styles.chev} data-open={all || undefined} />
          {all ? "Show fewer" : `Show all ${orders.length}`}
        </Button>
      )}
    </motion.section>
  );
}
