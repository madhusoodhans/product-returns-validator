/* The API key dialog (docs/API.md "API key"): a password field with show/hide, "Check key" with a spinner, the error for
 * each code under the field, and the success state. Opened at start without a key, from "Change key" in the top bar, and
 * from a turn whose key failed. */
import { useEffect, useRef, useState, type FormEvent } from "react";
import { CircleCheck, ExternalLink, Eye, EyeOff } from "lucide-react";
import { Dialog, DialogContent } from "../components/arc/dialog/dialog";
import { Button } from "../components/arc/button/button";
import { GET_KEY_URL, type KeyResult } from "../key";
import styles from "./KeyDialog.module.css";

export function KeyDialog({ open, onOpenChange, onSubmit, error, canClose }: {
  open: boolean; onOpenChange: (o: boolean) => void;
  /** Validates and stores the key (POST /api/key); resolves with the result. */
  onSubmit: (key: string) => Promise<KeyResult>;
  /** Why the dialog is open (a key that failed during a turn, or a hand-off key that did not work). */
  error?: string | null;
  /** A working key is set (the title says "Change API key"). Without one the dialog can still be closed: the page then
   * shows its disabled state with "Enter API key" hints. */
  canClose: boolean;
}) {
  const [value, setValue] = useState("");
  const [show, setShow] = useState(false);
  const [checking, setChecking] = useState(false);
  const [result, setResult] = useState<KeyResult | null>(null);
  const input = useRef<HTMLInputElement>(null);
  // Focus goes back to whatever opened the dialog (the top-bar key icon, or a "Change key" button), when it is still there.
  const returnTo = useRef<HTMLElement | null>(null);
  const wasOpen = useRef(false);
  if (open && !wasOpen.current) returnTo.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  wasOpen.current = open;

  useEffect(() => {
    if (open) { setResult(null); setValue(""); setShow(false); }
  }, [open]);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!value.trim() || checking) return;
    setChecking(true);
    setResult(null);
    const r = await onSubmit(value);
    setChecking(false);
    setResult(r);
    if (r.ok) { setValue(""); setTimeout(() => onOpenChange(false), 1100); }
    else input.current?.focus();
  };

  const shownError = result && !result.ok ? result.message : !result ? error : null;
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className={styles.dialog} onCloseAutoFocus={(e) => {
        const el = returnTo.current;
        if (el && el.isConnected && el !== document.body) { e.preventDefault(); el.focus(); }
      }} title={canClose ? "Change API key" : "Enter your API key"}
        description="Every model call in this demo runs on your vllm-sr key. It stays in this browser tab and in the demo server's memory, never on disk.">
        <form className={styles.form} onSubmit={submit} noValidate>
          <label className={styles.label} htmlFor="vsr-key">vllm-sr API key</label>
          <div className={styles.field} data-invalid={shownError ? true : undefined}>
            <input id="vsr-key" ref={input} className={styles.input} type={show ? "text" : "password"} value={value}
              onChange={(e) => { setValue(e.target.value); setResult(null); }} placeholder="sk-vsr-..." autoComplete="off"
              autoCapitalize="off" autoCorrect="off" spellCheck={false} autoFocus disabled={checking}
              aria-invalid={shownError ? true : undefined} aria-describedby="vsr-key-status" />
            <button type="button" className={styles.eye} onClick={() => setShow((s) => !s)} aria-label={show ? "Hide key" : "Show key"} aria-pressed={show}>
              {show ? <EyeOff size={16} strokeWidth={1.9} aria-hidden="true" /> : <Eye size={16} strokeWidth={1.9} aria-hidden="true" />}
            </button>
          </div>
          <p id="vsr-key-status" className={styles.status} role="status" aria-live="polite" data-tone={result?.ok ? "ok" : shownError ? "bad" : undefined}>
            {result?.ok ? <><CircleCheck size={15} strokeWidth={2} aria-hidden="true" /> Key works for decisions and chat</>
              : shownError ?? (checking ? "Checking the key with one small decision call and one chat call" : "")}
          </p>
          <div className={styles.row}>
            <a className={styles.get} href={GET_KEY_URL} target="_blank" rel="noreferrer">
              Get an API key <ExternalLink size={13} strokeWidth={1.9} aria-hidden="true" />
            </a>
            <Button type="submit" loading={checking} disabled={!value.trim() || checking}>Check key</Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}
