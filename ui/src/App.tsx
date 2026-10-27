/* Page shell: stats bar on top; sample demos, chat and the decision flow side by side. */
import { useEffect, useState } from "react";
import type { Theme } from "./components/arc/theme-switch/theme-switch";
import { errText, getSamples, type Samples as SamplesData } from "./api";
import { useDemo } from "./useDemo";
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

export function App() {
  const [theme, setTheme] = useTheme();
  const [samples, setSamples] = useState<SamplesData>({ groups: [], policies: {} });
  const [samplesError, setSamplesError] = useState<string | null>(null);
  const [galleryOpen, setGalleryOpen] = useState(false);
  const demo = useDemo();

  useEffect(() => { getSamples().then(setSamples, (e) => setSamplesError(errText(e))); }, []);

  return (
    <div className={styles.shell}>
      <TopBar stats={demo.stats} theme={theme} onTheme={setTheme} />
      <div className={styles.body}>
        <Samples groups={samples.groups} error={samplesError} playing={demo.playing} outcomes={demo.outcomes} used27={demo.used27}
          onPlay={(g, sc) => demo.playScenario(g, sc)} onPlayAll={() => demo.playAll(samples.groups)} onStop={demo.stop} />
        <Chat messages={demo.messages} busy={demo.busy} playing={demo.playing} replayMsg={demo.replay?.msgId ?? null} liveTurn={demo.liveTurn}
          order={demo.order} policies={samples.policies}
          orders={demo.customer?.orders ?? []} showOrders={!demo.order && !demo.sampleChat && !demo.playing && !demo.ended}
          ended={demo.ended} onReview={() => demo.send({ action: "review" })}
          onSend={(i) => demo.send(i)} onOpenTurn={demo.showTurn} onGallery={() => setGalleryOpen(true)} onReset={demo.reset} />
        <Flow flow={demo.flow} replay={demo.replay} hasTurn={demo.messages.some((m) => m.role === "user")} onBack={demo.closeReplay} />
      </div>
      <Gallery open={galleryOpen} onOpenChange={setGalleryOpen} policies={samples.policies} onPick={(path) => demo.send({ image: path })} />
    </div>
  );
}
