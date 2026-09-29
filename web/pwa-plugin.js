// The installed app (docs/MOBILE.md): /m is its own entry (m.html, src/mobile), and sw.js is
// built from sw/sw.js with this deploy's version and the /m shell's files to precache.
// In dev, /m and /m/* serve m.html and /sw.js serves an empty precache.
// Plain JS (no Node types in the web's tsconfig); pwa-plugin.d.ts gives vite.config.ts its type.
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";

const STATIC = ["/m.html", "/manifest.webmanifest", "/icons/icon-192.png", "/icons/badge-96.png", "/icons/icon.svg"];

export default function pwaPlugin() {
  const source = () => readFileSync(new URL("./sw/sw.js", import.meta.url), "utf8");
  const render = (version, files) =>
    source().replace('"__VERSION__"', JSON.stringify(version)).replace("__PRECACHE__", JSON.stringify(files));
  return {
    name: "pos-pwa",
    configureServer(server) {
      server.middlewares.use((req, res, next) => {
        const path = (req.url ?? "").split("?")[0];
        if (path === "/sw.js") {
          res.setHeader("Content-Type", "text/javascript");
          res.setHeader("Cache-Control", "no-cache");
          res.end(render("dev", []));
          return;
        }
        if (path === "/m" || path.startsWith("/m/")) req.url = "/m.html";
        next();
      });
    },
    configurePreviewServer(server) {
      // `vite preview` of the build: /m and /m/* are m.html, like nginx does.
      server.middlewares.use((req, _res, next) => {
        const path = (req.url ?? "").split("?")[0];
        if (path === "/m" || path.startsWith("/m/")) req.url = "/m.html";
        next();
      });
    },
    generateBundle(_, bundle) {
      // The /m entry and everything it imports statically: the shell that must work offline.
      const entry = Object.values(bundle).find((c) => c.type === "chunk" && c.isEntry && c.name === "m"); // input m: m.html
      if (!entry) this.error("pos-pwa: the /m entry (m.html) is missing");
      const files = new Set();
      const walk = (name) => {
        const c = bundle[name];
        if (!c || c.type !== "chunk" || files.has(`/${c.fileName}`)) return;
        files.add(`/${c.fileName}`);
        c.viteMetadata?.importedCss.forEach((css) => files.add(`/${css}`));
        c.imports.forEach(walk);
      };
      walk(entry.fileName);
      const list = [...STATIC, ...[...files].sort()];
      const version = createHash("sha256").update(list.join("\n")).update(source()).digest("hex").slice(0, 12);
      this.emitFile({ type: "asset", fileName: "sw.js", source: render(version, list) });
    },
  };
}
