/* The sample folder as a modal gallery (GET /api/gallery), grouped by product and case. Clicking a photo sends it. */
import { useEffect, useMemo, useState } from "react";
import { Dialog, DialogContent } from "../components/arc/dialog/dialog";
import { getGallery, errText, imageUrl, type GalleryItem, type Policies } from "../api";
import { titleCase } from "../format";
import styles from "./Gallery.module.css";

export function Gallery({ open, onOpenChange, policies, onPick }: {
  open: boolean; onOpenChange: (o: boolean) => void; policies: Policies; onPick: (path: string) => void;
}) {
  const [items, setItems] = useState<GalleryItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (!open || items) return;
    getGallery().then(setItems, (e) => setError(errText(e)));
  }, [open, items]);

  const groups = useMemo(() => {
    const by = new Map<string, GalleryItem[]>();
    for (const g of items ?? []) by.set(g.product, [...(by.get(g.product) ?? []), g]);
    return [...by.entries()];
  }, [items]);
  const nameOf = (k: string) => policies[k]?.name ?? titleCase(k);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className={styles.dialog} title="Sample photos" description="Pick a photo to send it in the chat.">
        {error && <p className={styles.error}>{error}</p>}
        {!items && !error && <p className={styles.muted}>Loading photos</p>}
        {items && items.length === 0 && <p className={styles.muted}>The sample folder is empty.</p>}
        <div className={styles.groups}>
          {groups.map(([product, list]) => (
            <section key={product} aria-labelledby={`g-${product}`}>
              <h3 id={`g-${product}`} className={styles.h3}>{nameOf(product)}</h3>
              <ul className={styles.grid}>
                {list.map((g) => (
                  <li key={g.path}>
                    <button type="button" className={styles.tile} onClick={() => { onPick(g.path); onOpenChange(false); }}
                      aria-label={`Send ${nameOf(product)}, ${titleCase(g.case).toLowerCase()}`}>
                      <img src={imageUrl(g.path)} alt="" loading="lazy" />
                      <span>{titleCase(g.case)}</span>
                    </button>
                  </li>
                ))}
              </ul>
            </section>
          ))}
        </div>
      </DialogContent>
    </Dialog>
  );
}
