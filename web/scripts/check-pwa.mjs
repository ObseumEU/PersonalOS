// After `vite build`: the installed app is complete and small (docs/MOBILE.md).
// - sw.js is built (version and precache list filled in) and precaches the /m shell;
// - the manifest has what Chrome needs to install it, and its icons exist;
// - the /m entry with everything it imports statically stays under the budget (gzip).
// Fails the build (exit 1) otherwise. Then removes dist/.vite (the chunk graph is not served).
import { existsSync, readFileSync, rmSync } from "node:fs";
import { gzipSync } from "node:zlib";

const BUDGET_KB = 150;
const dist = new URL("../dist/", import.meta.url);
const read = (p) => readFileSync(new URL(p, dist));
const fail = [];
const check = (ok, msg) => ok || fail.push(msg);

// The service worker
const sw = read("sw.js").toString();
check(!sw.includes("__VERSION__") && !sw.includes("__PRECACHE__"), "sw.js: placeholders not filled");
const precache = JSON.parse(/const PRECACHE = (\[.*?\]);/s.exec(sw)?.[1] ?? "[]");
check(precache.includes("/m.html"), "sw.js: /m.html is not precached");
check(precache.some((p) => /^\/assets\/m-.*\.js$/.test(p)), "sw.js: the /m entry script is not precached");
for (const p of precache) check(existsSync(new URL(`.${p}`, dist)), `sw.js precaches a missing file: ${p}`);
for (const ev of ["install", "activate", "fetch", "push", "notificationclick"]) check(sw.includes(`"${ev}"`), `sw.js: no ${ev} handler`);

// The manifest
const man = JSON.parse(read("manifest.webmanifest").toString());
check(man.name && man.short_name, "manifest: name and short_name");
check(man.start_url === "/m" && man.display === "standalone", "manifest: start_url /m, display standalone");
const sizes = (purpose) => man.icons.filter((i) => (i.purpose ?? "any").split(" ").includes(purpose)).map((i) => i.sizes);
check(sizes("any").includes("192x192") && sizes("any").includes("512x512"), "manifest: 192 and 512 icons");
check(sizes("maskable").includes("512x512"), "manifest: a maskable icon");
for (const i of man.icons) check(existsSync(new URL(`.${i.src}`, dist)), `manifest icon missing: ${i.src}`);

// The /m budget: its entry and static imports (lazy chunks are not counted), JS and CSS, gzip.
const graph = JSON.parse(read(".vite/manifest.json").toString());
const seen = new Set();
let bytes = 0;
const walk = (key) => {
  if (seen.has(key)) return;
  seen.add(key);
  const c = graph[key];
  bytes += gzipSync(read(c.file)).length;
  for (const css of c.css ?? []) if (!seen.has(css)) (seen.add(css), (bytes += gzipSync(read(css)).length));
  for (const k of c.imports ?? []) walk(k);
};
walk("m.html");
const kb = bytes / 1024;
check(kb < BUDGET_KB, `/m entry is ${kb.toFixed(1)} KB gzip, over the ${BUDGET_KB} KB budget`);

rmSync(new URL(".vite", dist), { recursive: true, force: true });
if (fail.length) {
  console.error(`check-pwa: ${fail.length} problem(s):\n- ${fail.join("\n- ")}`);
  process.exit(1);
}
console.log(`check-pwa: ok · sw.js precaches ${precache.length} files · /m entry ${kb.toFixed(1)} KB gzip (budget ${BUDGET_KB} KB)`);
