// Minimal stand-in for `npx shadcn add @uiarc/<id>` (the shadcn CLI needs Node 20; this machine has 18).
// Fetches https://uiarc.dev/r/<id>.json, follows registryDependencies, writes files under src/, prints npm deps.
// Usage: node scripts/arc-add.mjs button select ...
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";

const ROOT = new URL("..", import.meta.url).pathname;
const seen = new Set(), deps = new Set();
const urlOf = (x) => (x.startsWith("http") ? x : `https://uiarc.dev/r/${x.replace(/^@uiarc\//, "")}.json`);

function targetPath(f) {
  const t = f.target || f.path;
  if (t.startsWith("@components/")) return join(ROOT, "src/components", t.slice("@components/".length));
  if (t.startsWith("~/")) return join(ROOT, t.slice(2));
  return join(ROOT, "src", t.replace(/^registry\//, "components/arc/"));
}

async function add(ref) {
  const url = urlOf(ref);
  if (seen.has(url)) return;
  seen.add(url);
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${url}: ${r.status}`);
  const item = await r.json();
  for (const d of item.dependencies || []) deps.add(d);
  for (const f of item.files || []) {
    const p = targetPath(f);
    mkdirSync(dirname(p), { recursive: true });
    writeFileSync(p, f.content);
    console.log(`  ${item.name}: ${p.slice(ROOT.length)}`);
  }
  for (const d of item.registryDependencies || []) await add(d);
}

for (const id of process.argv.slice(2)) await add(id);
console.log("npm deps:", [...deps].join(" "));
