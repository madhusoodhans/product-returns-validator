/* The API key control in the top bar (same component in the receipt and returns demos).
 * - No key: the accent "Enter API key" button opens the key form.
 * - Checking a stored or handed-over key: a quiet icon button, disabled, with a spinner and the tooltip "Checking key".
 * - Key set and working: the same icon button with a small success dot, tooltip and name "API key set"; it opens a
 *   menu (Arc dropdown menu) with "Change key" (the key form) and "Forget key" (DELETE /api/key, sessionStorage
 *   cleared, back to the no-key state). Escape closes the menu and focus returns to the icon; after "Forget key" focus
 *   moves to "Enter API key".
 * A key that fails during a run is not shown here: the error banner / message keeps its own "Change key" button. */
import { useEffect, useRef } from "react";
import { KeyRound, LoaderCircle, LogOut, PenLine } from "lucide-react";
import { DropdownMenu } from "../components/arc/dropdown-menu/dropdown-menu";
import { Tooltip } from "../components/arc/tooltip/tooltip";
import styles from "./KeyMenu.module.css";

export type KeyMenuStatus = "none" | "checking" | "ready";

export function KeyMenu({ status, busy, onEnter, onChange, onForget }: {
  status: KeyMenuStatus;
  /** A run is in progress: "Change key" and "Forget key" are disabled until it ends. */
  busy?: boolean;
  /** Opens the key form (no key yet). */
  onEnter: () => void;
  /** Opens the key form to replace a working key. */
  onChange: () => void;
  /** Drops the key on the server and in this tab. */
  onForget: () => void | Promise<void>;
}) {
  const enterRef = useRef<HTMLButtonElement>(null);
  const action = useRef<"change" | "forget" | null>(null);

  useEffect(() => {
    if (status !== "none" || action.current !== "forget") return;
    // After Radix has finished restoring focus from the closing menu (its own zero-delay timer), move it here.
    const t = setTimeout(() => { action.current = null; enterRef.current?.focus(); }, 0);
    return () => clearTimeout(t);
  }, [status]);

  if (status === "none") {
    return (
      <button ref={enterRef} type="button" className={styles.enter} onClick={onEnter} title="Enter your vllm-sr API key">
        <KeyRound size={14} strokeWidth={1.9} aria-hidden="true" />Enter API key
      </button>
    );
  }

  if (status === "checking") {
    return (
      <Tooltip content="Checking key" side="bottom">
        <button type="button" className={styles.icon} aria-disabled="true" aria-label="Checking key" data-checking=""
          onClick={(e) => e.preventDefault()}>
          <LoaderCircle className={styles.spinner} size={16} strokeWidth={1.9} aria-hidden="true" />
        </button>
      </Tooltip>
    );
  }

  return (
    <DropdownMenu
      label="API key set"
      tooltip="API key set"
      triggerClassName={styles.icon}
      trigger={<><KeyRound size={16} strokeWidth={1.9} aria-hidden="true" /><span className={styles.dot} aria-hidden="true" /></>}
      onCloseAutoFocus={(e) => {
        // "Forget key": the icon is about to go, focus moves to "Enter API key" instead. "Change key": focus returns to
        // the icon first, then the form opens (so closing the form brings focus back here).
        const a = action.current;
        if (a === "forget") e.preventDefault();
        if (a === "change") { action.current = null; setTimeout(onChange, 0); }
      }}
      items={[
        { label: "Change key", icon: <PenLine size={15} strokeWidth={1.9} />, disabled: busy, onSelect: () => { action.current = "change"; } },
        { label: "Forget key", icon: <LogOut size={15} strokeWidth={1.9} />, disabled: busy, onSelect: () => { action.current = "forget"; void onForget(); } },
      ]}
    />
  );
}
