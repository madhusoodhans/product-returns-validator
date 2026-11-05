/* Page shell: stats bar on top; sample demos, chat and the decision flow side by side. */
import { useCallback, useEffect, useState } from "react";
import type { Theme } from "./components/arc/theme-switch/theme-switch";
import { errText, getConfig, getHealth, getSamples, type Health, type Samples as SamplesData } from "./api";
import { useDemo } from "./useDemo";
import { deleteKey, forgetStoredKey, getKeyState, sendKey, storeKey, storedKey, type KeyResult } from "./key";
import { KeyDialog } from "./panels/KeyDialog";
import { TopBar } from "./panels/TopBar";
import { Samples } from "./panels/Samples";
import { Chat } from "./panels/Chat";
import { Flow } from "./panels/Flow";
import { Gallery } from "./panels/Gallery";
import styles from "./App.module.css";

function useTheme(): [Theme, (t: Theme) => void] {
  const [theme, setThemeState] = useState<Theme>(() => (document.documentElement.dataset.theme === "dark" ? "dark" : "light"));
  const setTheme = (t: Theme) => {
    const apply = () => { document.documentElement.dataset.theme = t; setThemeState(t); };
    const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
    const doc = document as Document & { startViewTransition?: (cb: () => void) => void };
    if (doc.startViewTransition && !reduce) doc.startViewTransition(apply); else apply();
    try { localStorage.setItem("theme", t); } catch { /* private window */ }
  };
  return [theme, setTheme];
}

const HEALTH_POLL_MS = 15_000;

/** The server's view of which models are up: polled, and fetched again after every turn (a turn may have found one down). */
function useHealth(busy: boolean): Health | null {
  const [health, setHealth] = useState<Health | null>(null);
  useEffect(() => {
    const load = () => getHealth().then(setHealth, () => setHealth(null));
    load();
    const id = setInterval(load, HEALTH_POLL_MS);
    return () => clearInterval(id);
  }, []);
  useEffect(() => { if (!busy) getHealth().then(setHealth, () => { /* keep the last value */ }); }, [busy]);
  return health;
}

/** The visitor's key: "checking" (a stored or handed-over key is being sent), "ready" (the server holds a working key for
 * this tab, or has its own), "none" (the dialog asks for one). Demo actions are disabled until "ready". */
function useApiKey() {
  const [status, setStatus] = useState<"checking" | "ready" | "none">("checking");
  const [error, setError] = useState<string | null>(null);
  const [dialog, setDialog] = useState(false);

  useEffect(() => {
    const stored = storedKey();
    if (stored) {
      sendKey(stored).then((r) => {
        if (r.ok) { setStatus("ready"); return; }
        forgetStoredKey();
        setStatus("none"); setError(r.message ?? null); setDialog(true);
      });
      return;
    }
    getKeyState().then((k) => {
      if (k.set || k.server_key) setStatus("ready");
      else { setStatus("none"); setDialog(true); }
    }, () => { setStatus("none"); setDialog(true); });
  }, []);

  const submit = useCallback(async (key: string): Promise<KeyResult> => {
    const r = await sendKey(key);
    if (r.ok) { storeKey(key); setStatus("ready"); setError(null); }
    return r;
  }, []);
  /** The server has no key for this tab (restarted, or the key expired there): send the stored one again. */
  const recover = useCallback(async () => {
    const stored = storedKey();
    if (!stored) return false;
    const r = await sendKey(stored);
    if (!r.ok) forgetStoredKey();
    return r.ok;
  }, []);
  const failed = useCallback((_code: string, message: string) => {
    forgetStoredKey();
    setStatus("none");
    setError(message);
  }, []);
  /** "Forget key" in the top bar: DELETE /api/key and clear sessionStorage, then the no-key state (actions disabled, no dialog). */
  const forget = useCallback(async () => {
    await deleteKey();
    setStatus("none"); setError(null);
  }, []);
  return { status, ready: status === "ready", error, dialog, setDialog, submit, recover, failed, forget };
}

export function App() {
  const [theme, setTheme] = useTheme();
  const [samples, setSamples] = useState<SamplesData>({ groups: [], policies: {} });
  const [samplesError, setSamplesError] = useState<string | null>(null);
  const [galleryOpen, setGalleryOpen] = useState(false);
  const key = useApiKey();
  const demo = useDemo({ recover: key.recover, failed: key.failed });
  const keyHint = key.status === "checking" ? "Checking your API key" : "Enter your API key to start";
  const health = useHealth(demo.busy);

  const [playAll, setPlayAll] = useState(false);
  useEffect(() => { getSamples().then(setSamples, (e) => setSamplesError(errText(e))); }, []);
  const [landingUrl, setLandingUrl] = useState("");
  useEffect(() => {
    getConfig().then((c) => { setPlayAll(c.play_all !== false); setLandingUrl(c.landing_url ?? ""); }, () => setPlayAll(false));
  }, []);

  return (
    <div className={styles.shell}>
      <TopBar stats={demo.stats} health={health} theme={theme} onTheme={setTheme} landingUrl={landingUrl}
        keyStatus={key.status} busy={demo.busy || !!demo.playing} onChangeKey={() => key.setDialog(true)} onForgetKey={key.forget} />
      <div className={styles.body}>
        <Samples groups={samples.groups} error={samplesError} playing={demo.playing} outcomes={demo.outcomes} usedReasoning={demo.usedReasoning} playAll={playAll}
          disabled={!key.ready} hint={keyHint}
          onPlay={(g, sc) => demo.playScenario(g, sc)} onPlayAll={() => demo.playAll(samples.groups)} onStop={demo.stop} />
        <Chat messages={demo.messages} busy={demo.busy} playing={demo.playing} replayMsg={demo.replay?.msgId ?? null} liveTurn={demo.liveTurn}
          order={demo.order} policies={samples.policies}
          orders={demo.customer?.orders ?? []} showOrders={!demo.order && !demo.sampleChat && !demo.playing && !demo.ended}
          ended={demo.ended} onReview={() => demo.send({ action: "review" })}
          keyReady={key.ready} keyHint={keyHint} onChangeKey={() => key.setDialog(true)}
          onSend={(i) => demo.send(i)} onOpenTurn={demo.showTurn} onGallery={() => setGalleryOpen(true)} onReset={demo.reset} />
        <Flow flow={demo.flow} replay={demo.replay} hasTurn={demo.messages.some((m) => m.role === "user")} onBack={demo.closeReplay} />
      </div>
      <footer className={styles.footer}>Messages and photos are processed by the hosted vllm-sr API (research and evaluation use).</footer>
      <KeyDialog open={key.dialog} onOpenChange={key.setDialog} onSubmit={key.submit} error={key.error} canClose={key.ready} />
      <Gallery open={galleryOpen} onOpenChange={setGalleryOpen} policies={samples.policies} onPick={(path) => demo.send({ image: path })} />
    </div>
  );
}
